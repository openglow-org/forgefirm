# Copyright 2026 514 LLC d/b/a OpenGlow
# Written by Scott Wiederhold
# SPDX-License-Identifier: MIT

# forgefirm-manifest.bbclass - source fingerprint of one ForgeFIRM component
#
# A component recipe that inherits this class records what source it was
# built from: the component name, the revision of the checkout, and one
# [path, blob-id] pair per source file (git blob ids, so equal content
# means equal ids regardless of the commit that carried it). The image
# collects the entries into /etc/forgefirm-manifest.json
# (forgefirm-image-manifest.bbclass); the acceptance tool (forgetest) and
# the release gate (scripts/acceptance-gate.py) read that file to decide
# which acceptance results still apply to a build.
#
# Modes (FORGEFIRM_MANIFEST_MODE):
#   rootfs  (default) the entry is installed as
#           /etc/forgefirm-manifest.d/<PN>.json in the recipe's main package
#   deploy  the recipe writes the entry into ${DEPLOYDIR} itself (see the
#           kernel-module bbappend: its .ko is packaged by kernel-module-split
#           into a versioned package, so a rootfs file from the recipe would
#           not reach the image); this class then only provides the helpers.
#
# Sources: a git checkout in ${S} is fingerprinted as it is compiled: every
# tracked file as it is in the working tree, and every untracked file that
# the checkout's own .gitignore files do not ignore (a local exclude file
# and the build user's global excludes do not count, so the list does not
# depend on the build host). A submodule is listed as the commit it has
# checked out and recursed. A clean checkout lists exactly the blob ids of
# its commit (`git ls-tree`); a working tree that differs from its commit
# marks the entry "dirty", and a build from a local checkout (externalsrc)
# marks it "local_source". Non-git sources (file:// recipes) set
# FORGEFIRM_MANIFEST_SRC to the directory to fingerprint; the ids are
# computed with `git hash-object`, so they compare with tree ids. Such a
# recipe whose files come from a checkout outside the layers (forgetest
# takes them from the forgefirm checkout's forgetest/ and scripts/bench/)
# names those paths in FORGEFIRM_MANIFEST_DIRTY_TREES: the entry is marked
# "dirty" when one of them differs from its commit, by the rule the image
# class applies to a layer (forgefirm_manifest_checkout_dirty).
#
# The entry also lists the files the recipe installs ("installed", paths in
# the image). The image class hashes them in the root filesystem and keeps
# the hashes outside the identity, for forgetest to compare with the files
# that run on the machine.

FORGEFIRM_MANIFEST_NAME ?= "${PN}"
FORGEFIRM_MANIFEST_MODE ?= "rootfs"
FORGEFIRM_MANIFEST_SRC ?= ""
FORGEFIRM_MANIFEST_DIRTY_TREES ?= ""
FORGEFIRM_MANIFEST_DIR = "${sysconfdir}/forgefirm-manifest.d"

def forgefirm_manifest_git(args, cwd):
    import subprocess
    return subprocess.check_output(['git'] + args, cwd=cwd,
                                   stderr=subprocess.STDOUT).decode('utf-8', 'replace')

def forgefirm_manifest_tree(files, repo, prefix):
    """[path, id] of every file of the checkout's commit (HEAD), submodules
    recursed at the commit each has checked out."""
    import os
    out = forgefirm_manifest_git(['ls-tree', '-r', '--full-tree', 'HEAD'], repo)
    for line in out.splitlines():
        if not line.strip():
            continue
        meta, path = line.split('\t', 1)
        fields = meta.split()
        typ, obj = fields[1], fields[2]
        files.append([prefix + path, obj])
        if typ == 'commit':
            sub = os.path.join(repo, path)
            if os.path.exists(os.path.join(sub, '.git')):
                forgefirm_manifest_tree(files, sub, prefix + path + '/')

def forgefirm_manifest_blob_id(data):
    """The git blob id of bytes (what git stores for a symbolic link: its
    target)."""
    import hashlib
    return hashlib.sha1(b'blob %d\0' % len(data) + data).hexdigest()

def forgefirm_manifest_worktree(files, repo, prefix):
    """[path, id] of what a build of the checkout compiles: every tracked
    file as it is in the working tree (a deleted one is not listed), and
    every untracked file that the checkout's own .gitignore files do not
    ignore. A submodule, or an untracked repository inside the checkout, is
    listed as the commit it has checked out and recursed."""
    import os, subprocess
    tracked = {}
    out = subprocess.run(['git', 'ls-files', '-z', '-s'], cwd=repo,
                         stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=True).stdout
    for rec in out.split(b'\0'):
        if rec:
            meta, path = rec.split(b'\t', 1)
            mode, obj = meta.split()[:2]
            tracked[path.decode('utf-8', 'replace')] = (mode.decode(), obj.decode())
    out = subprocess.run(['git', '-c', 'core.excludesFile=/dev/null', 'ls-files', '-z', '--others',
                          '--exclude-per-directory=.gitignore'], cwd=repo,
                         stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=True).stdout
    others = set(p.decode('utf-8', 'replace').rstrip('/') for p in out.split(b'\0') if p)
    regular = []
    for path in sorted(set(tracked) | others):
        full = os.path.join(repo, path)
        mode, obj = tracked.get(path, (None, None))
        if mode == '160000' or (mode is None and os.path.isdir(full)):
            if os.path.exists(os.path.join(full, '.git')):
                try:
                    head = forgefirm_manifest_git(['rev-parse', 'HEAD'], full).strip()
                except subprocess.CalledProcessError:
                    head = 'no-commit'
                files.append([prefix + path, head])
                forgefirm_manifest_worktree(files, full, prefix + path + '/')
            elif mode == '160000':
                files.append([prefix + path, obj])
        elif os.path.islink(full):
            files.append([prefix + path, forgefirm_manifest_blob_id(os.fsencode(os.readlink(full)))])
        elif os.path.isfile(full):
            regular.append(path)
    if regular:
        out = subprocess.run(['git', 'hash-object', '--stdin-paths'], cwd=repo,
                             input=('\n'.join(regular) + '\n').encode(),
                             stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=True).stdout
        ids = out.decode().split()
        if len(ids) != len(regular):
            bb.fatal("forgefirm-manifest: hash-object returned %d ids for %d paths"
                     % (len(ids), len(regular)))
        files.extend([prefix + p, i] for p, i in zip(regular, ids))

def forgefirm_manifest_git_prefix(cwd):
    """`git hash-object --stdin-paths` resolves paths against the repository
    top level, not the cwd; this is the cwd's prefix inside the enclosing
    repository ('' when there is none)."""
    import subprocess
    try:
        return subprocess.check_output(['git', 'rev-parse', '--show-prefix'], cwd=cwd,
                                       stderr=subprocess.DEVNULL).decode().strip()
    except (subprocess.CalledProcessError, OSError):
        return ''

def forgefirm_manifest_hash_dir(src):
    import os, subprocess
    paths = []
    for root, dirs, fns in os.walk(src):
        dirs[:] = sorted(x for x in dirs if x not in ('.git', '__pycache__'))
        for fn in fns:
            if fn.endswith(('.pyc', '.pyo')):
                continue
            p = os.path.join(root, fn)
            if os.path.isfile(p) and not os.path.islink(p):
                paths.append(os.path.relpath(p, src))
    paths.sort()
    if not paths:
        return []
    prefix = forgefirm_manifest_git_prefix(src)
    out = subprocess.run(['git', 'hash-object', '--stdin-paths'], cwd=src,
                         input=('\n'.join(prefix + p for p in paths) + '\n').encode(),
                         stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=True)
    ids = out.stdout.decode().split()
    if len(ids) != len(paths):
        bb.fatal("forgefirm-manifest: hash-object returned %d ids for %d paths"
                 % (len(ids), len(paths)))
    return [[p, i] for p, i in zip(paths, ids)]

def forgefirm_manifest_checkout_dirty(path):
    """Whether a file or a directory of a git checkout differs from its
    commit: a tracked file changed or told to the index as unchanged
    (skip-worktree, assume-unchanged), or an untracked file that the
    checkout's own .gitignore files do not ignore. A path in no checkout
    cannot show its commit, so it counts as dirty."""
    import os, subprocess
    cwd, spec = (path, '.') if os.path.isdir(path) else (os.path.dirname(path), os.path.basename(path))
    try:
        status = subprocess.check_output(['git', 'status', '--porcelain', '--untracked-files=no', '--', spec],
                                         cwd=cwd, stderr=subprocess.DEVNULL)
        others = subprocess.check_output(['git', '-c', 'core.excludesFile=/dev/null', 'ls-files', '-z',
                                          '--others', '--exclude-per-directory=.gitignore', '--', spec],
                                         cwd=cwd, stderr=subprocess.DEVNULL)
        tags = subprocess.check_output(['git', 'ls-files', '-z', '-v', '--', spec], cwd=cwd,
                                       stderr=subprocess.DEVNULL)
    except (subprocess.CalledProcessError, OSError):
        return True
    hidden = any(t[:1] == b'S' or t[:1].islower() for t in tags.split(b'\0') if t)
    return bool(status.strip()) or bool(others.strip(b'\0')) or hidden

def forgefirm_manifest_installed(dest, skip_dir):
    """The regular files the recipe installed under `dest` (${D}), as paths
    in the image, its own manifest entries (`skip_dir`) left out."""
    import os
    out = []
    if not dest or not os.path.isdir(dest):
        return out
    skip = skip_dir.rstrip('/') + '/'
    for root, dirs, fns in os.walk(dest):
        dirs.sort()
        for fn in fns:
            p = os.path.join(root, fn)
            if os.path.isfile(p) and not os.path.islink(p):
                rel = '/' + os.path.relpath(p, dest)
                if not rel.startswith(skip):
                    out.append(rel)
    return sorted(out)

def forgefirm_manifest_entry(d):
    import os
    pn = d.getVar('PN')
    entry = {'component': d.getVar('FORGEFIRM_MANIFEST_NAME'),
             'recipe': pn, 'pv': d.getVar('PV'), 'files': []}
    src = (d.getVar('FORGEFIRM_MANIFEST_SRC') or '').strip()
    s = d.getVar('S')
    if src:
        if not os.path.isdir(src):
            bb.fatal("forgefirm-manifest: %s: FORGEFIRM_MANIFEST_SRC '%s' is not a directory" % (pn, src))
        entry['srcrev'] = None
        entry['source'] = 'files'
        entry['files'] = forgefirm_manifest_hash_dir(src)
        if any(forgefirm_manifest_checkout_dirty(t) for t in (d.getVar('FORGEFIRM_MANIFEST_DIRTY_TREES') or '').split()):
            entry['dirty'] = True
    elif os.path.exists(os.path.join(s, '.git')):
        entry['srcrev'] = forgefirm_manifest_git(['rev-parse', 'HEAD'], s).strip()
        uri = (d.getVar('SRC_URI') or '').split()
        entry['source'] = uri[0].split(';')[0] if uri else None
        forgefirm_manifest_worktree(entry['files'], s, '')
        committed = []
        forgefirm_manifest_tree(committed, s, '')
        if sorted(entry['files']) != sorted(committed):
            entry['dirty'] = True
    else:
        bb.fatal("forgefirm-manifest: %s: ${S} is not a git checkout and FORGEFIRM_MANIFEST_SRC is unset" % pn)
    if (d.getVar('EXTERNALSRC') or '').strip():
        entry['local_source'] = True
    entry['files'].sort()
    entry['installed'] = forgefirm_manifest_installed(d.getVar('D'), d.getVar('FORGEFIRM_MANIFEST_DIR'))
    return entry

def forgefirm_manifest_write(entry, path):
    import json, os
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, 'w') as f:
        json.dump(entry, f, sort_keys=True, separators=(',', ':'))
        f.write('\n')
    os.chmod(path, 0o644)

fakeroot python do_forgefirm_manifest() {
    import os
    entry = forgefirm_manifest_entry(d)
    forgefirm_manifest_write(entry, os.path.join(d.getVar('D') + d.getVar('FORGEFIRM_MANIFEST_DIR'),
                                                 d.getVar('PN') + '.json'))
}
do_forgefirm_manifest[depends] += "virtual/fakeroot-native:do_populate_sysroot"
do_forgefirm_manifest[vardeps] += "FORGEFIRM_MANIFEST_NAME FORGEFIRM_MANIFEST_SRC FORGEFIRM_MANIFEST_DIRTY_TREES \
    SRCREV SRC_URI EXTERNALSRC \
    forgefirm_manifest_entry forgefirm_manifest_tree forgefirm_manifest_worktree forgefirm_manifest_blob_id \
    forgefirm_manifest_git forgefirm_manifest_hash_dir forgefirm_manifest_git_prefix \
    forgefirm_manifest_checkout_dirty forgefirm_manifest_installed forgefirm_manifest_write"

python __anonymous() {
    if d.getVar('FORGEFIRM_MANIFEST_MODE') == 'rootfs':
        bb.build.addtask('do_forgefirm_manifest', 'do_package do_populate_sysroot', 'do_install', d)
}

FILES:${PN}:append = " ${FORGEFIRM_MANIFEST_DIR}"
