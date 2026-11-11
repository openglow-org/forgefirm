#!/usr/bin/env python3
# Copyright 2026 514 LLC d/b/a OpenGlow
# Written by Scott Wiederhold
# https://community.openglow.org
# SPDX-License-Identifier:    MIT
#
# image-check.py --ext4 FILE --max-kib N --inode-ratio R [--tmp DIR]
#                [--card WIC --card-rootfs-kib N --data-label L
#                 --data-kib N --account LINE]
#
# The built image, read back from the files the build deploys: a failure
# here fails the build. forgefirm-image.bb's image_check runs it in
# do_image_complete on both images; forgetest's test_image.py runs it on
# small images it makes. It reads ext4 and the partition table itself, so
# it needs nothing from the build host but Python. Every rule is checked
# and every failure printed, one line each, before it exits 1. The rules:
#
#   size        the raw ext4 (the rootfs the .fw carries) fits the slot:
#               the file and the filesystem are at most --max-kib.
#   ext4        it is ext4 (extents) and keeps its journal.
#   reserved    no blocks are reserved for root.
#   inodes      one inode per --inode-ratio bytes, within a tenth (mkfs
#               rounds to whole inode table blocks).
# With --card, the dev image's SD card image (.wic, or .wic.gz):
#   partitions  two partitions: p1 the rootfs, exactly --card-rootfs-kib
#               (the slot's size, so a later raw ext4 fits it), p2 the
#               card's data, exactly --data-kib.
#   label       p1 is labeled root, p2 --data-label.
#   ext4, reserved, inodes  as above, for both partitions.
#   fstab       p1's /etc/fstab is the raw ext4's: nothing mounts the
#               card's data from the fstab (the image's boot step does).
#   seed        p2 is seeded as a finished setup, as forgefirm-data and
#               forgectrl read it: forgefirm/ (0711) and log/ (0755),
#               the import marker, forgefirm/users holding exactly
#               --account, forgefirm/forgefirm.conf setting
#               cool_intake_fans = 2, forgefirm/setup.seed; the three
#               files 0600, and every entry root's.

import argparse
import gzip
import os
import struct
import sys
import tempfile

FAILED = []


def fail(rule, msg):
    print("image-check: %s: %s" % (rule, msg), file=sys.stderr)
    FAILED.append(rule)


class Ext4Error(Exception):
    pass


class Ext4:
    """An ext4 filesystem at byte offset `base` of an open file: the
    superblock, and files and directories by path (extent-mapped)."""

    S_IFMT, S_IFDIR, S_IFREG = 0o170000, 0o040000, 0o100000

    def __init__(self, f, base=0):
        self.f, self.base = f, base
        sb = self.read(1024, 1024)
        if len(sb) < 1024 or struct.unpack_from("<H", sb, 56)[0] != 0xEF53:
            raise Ext4Error("no ext4 superblock")
        (self.inodes_count, blocks_lo, r_lo) = struct.unpack_from("<III", sb, 0)
        self.first_data_block, log_bs = struct.unpack_from("<II", sb, 20)
        self.block_size = 1024 << log_bs
        self.inodes_per_group = struct.unpack_from("<I", sb, 40)[0]
        self.inode_size = struct.unpack_from("<H", sb, 88)[0]
        self.compat, self.incompat, self.ro_compat = struct.unpack_from("<III", sb, 92)
        self.label = sb[120:136].split(b"\0", 1)[0].decode("utf-8", "replace")
        is64 = bool(self.incompat & 0x80)
        self.desc_size = (struct.unpack_from("<H", sb, 254)[0] or 32) if is64 else 32
        blocks_hi, r_hi = struct.unpack_from("<II", sb, 0x150) if is64 else (0, 0)
        self.blocks_count = blocks_lo | blocks_hi << 32
        self.r_blocks_count = r_lo | r_hi << 32

    def read(self, offset, n):
        self.f.seek(self.base + offset)
        return self.f.read(n)

    def inode(self, ino):
        group, index = divmod(ino - 1, self.inodes_per_group)
        gdt = (self.first_data_block + 1) * self.block_size
        desc = self.read(gdt + group * self.desc_size, self.desc_size)
        table = struct.unpack_from("<I", desc, 8)[0]
        if self.desc_size >= 64:
            table |= struct.unpack_from("<I", desc, 40)[0] << 32
        raw = self.read(table * self.block_size + index * self.inode_size, self.inode_size)
        mode, uid, size_lo = struct.unpack_from("<HHI", raw, 0)
        gid = struct.unpack_from("<H", raw, 24)[0]
        flags = struct.unpack_from("<I", raw, 32)[0]
        size_hi = struct.unpack_from("<I", raw, 108)[0]
        uid_hi, gid_hi = struct.unpack_from("<HH", raw, 120)
        return {"mode": mode, "uid": uid | uid_hi << 16, "gid": gid | gid_hi << 16,
                "size": size_lo | size_hi << 32, "flags": flags, "block": raw[40:100]}

    def _extents(self, node, out):
        magic, entries, _, depth = struct.unpack_from("<HHHH", node, 0)
        if magic != 0xF30A:
            raise Ext4Error("a bad extent header")
        for i in range(entries):
            e = node[12 + 12 * i: 24 + 12 * i]
            if depth == 0:
                lblock, length, hi, lo = struct.unpack("<IHHI", e)
                out.append((lblock, length - 32768 if length > 32768 else length, lo | hi << 32, length > 32768))
            else:
                _, lo, hi = struct.unpack_from("<IIH", e, 0)
                self._extents(self.read((lo | hi << 32) * self.block_size, self.block_size), out)
        return out

    def data(self, ino):
        if ino["flags"] & 0x10000000:
            raise Ext4Error("inline data is not read here")
        if not ino["flags"] & 0x80000:
            raise Ext4Error("a block-mapped file is not read here")
        buf = bytearray(ino["size"])
        for lblock, length, pblock, unwritten in self._extents(ino["block"], []):
            start = lblock * self.block_size
            if start >= len(buf) or unwritten:
                continue
            n = min(length * self.block_size, len(buf) - start)
            buf[start:start + n] = self.read(pblock * self.block_size, n)
        return bytes(buf)

    def entries(self, ino):
        """{name: inode number} of a directory inode."""
        data, out, pos = self.data(ino), {}, 0
        while pos + 8 <= len(data):
            child, rec_len, name_len = struct.unpack_from("<IHB", data, pos)
            if rec_len < 8:
                break
            if child:
                name = data[pos + 8: pos + 8 + name_len].decode("utf-8", "replace")
                if name not in (".", ".."):
                    out[name] = child
            pos += rec_len
        return out

    def lookup(self, path):
        """The inode at path, or None."""
        ino = self.inode(2)
        for part in [p for p in path.split("/") if p]:
            if ino["mode"] & self.S_IFMT != self.S_IFDIR:
                return None
            child = self.entries(ino).get(part)
            if child is None:
                return None
            ino = self.inode(child)
        return ino

    def walk(self, path="/", ino=None):
        """(path, inode) of everything under a directory."""
        ino = ino or self.lookup(path)
        for name, child in sorted(self.entries(ino).items()):
            sub = self.inode(child)
            p = path.rstrip("/") + "/" + name
            yield p, sub
            if sub["mode"] & self.S_IFMT == self.S_IFDIR:
                yield from self.walk(p, sub)


def check_fs(fs, what, ratio):
    """The slot's mkfs options, read back: ext4 with a journal, nothing
    reserved, one inode per `ratio` bytes."""
    if not fs.incompat & 0x40:
        fail("ext4", "%s does not use extents: it is not ext4" % what)
    if not fs.compat & 0x4:
        fail("ext4", "%s has no journal" % what)
    if fs.r_blocks_count:
        fail("reserved", "%s reserves %d blocks for root" % (what, fs.r_blocks_count))
    got = fs.blocks_count * fs.block_size / max(fs.inodes_count, 1)
    if not 0.9 * ratio <= got <= 1.1 * ratio:
        fail("inodes", "%s has one inode per %.0f bytes, not %d" % (what, got, ratio))


def check_rootfs(path, max_kib, ratio):
    size = os.path.getsize(path)
    if size > max_kib * 1024:
        fail("size", "%s is %d KiB, past the slot's %d KiB" % (os.path.basename(path), size // 1024, max_kib))
    with open(path, "rb") as f:
        try:
            fs = Ext4(f)
        except Ext4Error as e:
            fail("ext4", "%s: %s" % (os.path.basename(path), e))
            return None
        if fs.blocks_count * fs.block_size > max_kib * 1024:
            fail("size", "the filesystem is %d KiB, past the slot's %d KiB"
                 % (fs.blocks_count * fs.block_size // 1024, max_kib))
        check_fs(fs, "the rootfs", ratio)
        fstab = fs.lookup("/etc/fstab")
        return fs.data(fstab) if fstab else None


def partitions(head):
    """[(start byte, size in bytes)] of the MBR's partitions."""
    if head[510:512] != b"\x55\xaa":
        return None
    out = []
    for i in range(4):
        e = head[446 + 16 * i: 462 + 16 * i]
        if e[4]:
            start, count = struct.unpack_from("<II", e, 8)
            out.append((start * 512, count * 512))
    return out


def extract(path, tmp):
    """The card image's partitions as files to read: the image itself when
    it is not compressed, else each partition written out (sparse) in one
    pass over the stream. Returns [(file path, base offset, size)]."""
    with open(path, "rb") as f:
        gz = f.read(2) == b"\x1f\x8b"
    opener = gzip.open if gz else open
    with opener(path, "rb") as f:
        parts = partitions(f.read(512))
        if parts is None or not gz:
            return [(path, start, size) for start, size in parts or []]
        outs = []
        for i, (start, size) in enumerate(parts):
            out = os.path.join(tmp, "p%d.img" % (i + 1))
            with open(out, "wb") as o:
                o.truncate(size)
            outs.append(open(out, "r+b"))
        pos, chunk = 512, 1 << 20
        while True:
            buf = f.read(chunk)
            if not buf:
                break
            for (start, size), o in zip(parts, outs):
                lo, hi = max(pos, start), min(pos + len(buf), start + size)
                if lo < hi:
                    piece = buf[lo - pos: hi - pos]
                    if piece.count(0) != len(piece):
                        o.seek(lo - start)
                        o.write(piece)
            pos += len(buf)
        for o in outs:
            o.close()
        return [(os.path.join(tmp, "p%d.img" % (i + 1)), 0, size) for i, (_, size) in enumerate(parts)]


def check_seed(fs, account):
    def node(p):
        ino = fs.lookup(p)
        if ino is None:
            fail("seed", "p2 has no %s" % p)
        return ino

    for p, kind, mode in (("/forgefirm", Ext4.S_IFDIR, 0o711), ("/log", Ext4.S_IFDIR, 0o755),
                          ("/import", Ext4.S_IFREG, None), ("/forgefirm/users", Ext4.S_IFREG, 0o600),
                          ("/forgefirm/forgefirm.conf", Ext4.S_IFREG, 0o600),
                          ("/forgefirm/setup.seed", Ext4.S_IFREG, 0o600)):
        ino = node(p)
        if ino is None:
            continue
        if ino["mode"] & Ext4.S_IFMT != kind:
            fail("seed", "p2's %s is not a %s" % (p, "directory" if kind == Ext4.S_IFDIR else "file"))
        elif mode is not None and ino["mode"] & 0o7777 != mode:
            fail("seed", "p2's %s is mode %o, not %o" % (p, ino["mode"] & 0o7777, mode))
    users = fs.lookup("/forgefirm/users")
    if users is not None and fs.data(users).decode("utf-8", "replace") != account + "\n":
        fail("seed", "p2's forgefirm/users does not hold exactly the card's account")
    conf = fs.lookup("/forgefirm/forgefirm.conf")
    if conf is not None:
        lines = [ln.split("#", 1)[0].split() for ln in fs.data(conf).decode("utf-8", "replace").splitlines()]
        if ["cool_intake_fans", "=", "2"] not in lines:
            fail("seed", "p2's forgefirm/forgefirm.conf does not set cool_intake_fans = 2")
    for p, ino in fs.walk("/"):
        if p == "/lost+found" or p.startswith("/lost+found/"):
            continue
        if (ino["uid"], ino["gid"]) != (0, 0):
            fail("seed", "p2's %s is %d:%d, not root's" % (p, ino["uid"], ino["gid"]))


def check_card(path, args, rootfs_fstab):
    if not os.path.exists(path) and os.path.exists(path + ".gz"):
        path += ".gz"
    with tempfile.TemporaryDirectory(prefix="image-check.", dir=args.tmp) as tmp:
        parts = extract(path, tmp)
        want = (args.card_rootfs_kib * 1024, args.data_kib * 1024)
        if len(parts) != 2:
            fail("partitions", "the card image has %d partition(s), not 2" % len(parts))
            return
        for i, ((_, _, size), size_want) in enumerate(zip(parts, want)):
            if size != size_want:
                fail("partitions", "p%d is %d KiB, not %d" % (i + 1, size // 1024, size_want // 1024))
        handles = [open(p, "rb") for p, _, _ in parts]
        try:
            fss = []
            for i, ((_, base, _), f) in enumerate(zip(parts, handles)):
                try:
                    fss.append(Ext4(f, base))
                except Ext4Error as e:
                    fail("ext4", "p%d: %s" % (i + 1, e))
                    return
            p1, p2 = fss
            for fs, what, label in ((p1, "p1", "root"), (p2, "p2", args.data_label)):
                if fs.label != label:
                    fail("label", "%s is labeled %r, not %r" % (what, fs.label, label))
                check_fs(fs, what, args.inode_ratio)
            fstab = p1.lookup("/etc/fstab")
            if fstab is None or p1.data(fstab) != rootfs_fstab:
                fail("fstab", "p1's /etc/fstab is not the rootfs's: the card image adds a mount to it")
            check_seed(p2, args.account)
        finally:
            for f in handles:
                f.close()


def main():
    ap = argparse.ArgumentParser(description="The built image, read back.")
    ap.add_argument("--ext4", required=True)
    ap.add_argument("--max-kib", type=int, required=True)
    ap.add_argument("--inode-ratio", type=int, required=True)
    ap.add_argument("--tmp")
    ap.add_argument("--card")
    ap.add_argument("--card-rootfs-kib", type=int)
    ap.add_argument("--data-label")
    ap.add_argument("--data-kib", type=int)
    ap.add_argument("--account")
    args = ap.parse_args()
    if args.card and None in (args.card_rootfs_kib, args.data_label, args.data_kib, args.account):
        ap.error("--card needs --card-rootfs-kib, --data-label, --data-kib and --account")
    try:
        fstab = check_rootfs(args.ext4, args.max_kib, args.inode_ratio)
        if args.card:
            check_card(args.card, args, fstab)
    except (OSError, Ext4Error, struct.error) as e:
        fail("read", "the image could not be read: %s" % e)
    sys.exit(1 if FAILED else 0)


if __name__ == "__main__":
    main()
