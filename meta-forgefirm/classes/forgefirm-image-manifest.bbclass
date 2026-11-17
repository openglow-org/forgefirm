# Copyright 2026 514 LLC d/b/a OpenGlow
# Written by Scott Wiederhold
# SPDX-License-Identifier: MIT

# forgefirm-image-manifest.bbclass - assemble /etc/forgefirm-manifest.json
#
# Inherited by the ForgeFIRM images. At rootfs postprocess it collects the
# per-component entries (forgefirm-manifest.bbclass) from the rootfs
# (/etc/forgefirm-manifest.d/*.json) and from DEPLOY_DIR_IMAGE
# (forgefirm-manifest.d/*.json: the kernel and the kernel module, which
# cannot ship a rootfs file), adds the platform identity, and writes
#
#   /etc/forgefirm-manifest.json          (in the image)
#   ${IMAGE_NAME}.forgefirm-manifest.json (deployed next to the image,
#                                          with the usual link name)
#
# The platform section records what the components' fingerprints do not:
# the machine, the kernel modules directory (carries the kernel's local
# version), the device tree blob hashes, the layers, the kas configuration,
# and, on the debug-kernel build only, "kernel_debug". Layers named in
# FORGEFIRM_MANIFEST_CONTENT_LAYERS are identified by content (tracked
# files, and untracked files that the checkout's own .gitignore files do
# not ignore, under the layer directory, as git blob ids; a local exclude
# file and the build user's global excludes do not count), so a build from
# a dirty working tree and a later build from the identical committed tree
# agree; the other layers are identified by revision. The kas configuration
# is identified the same way, by the text of its files in the tree (every
# file under kas/ but the lock file, whose commits the layers carry): a
# local_conf_header line (an IMAGE_INSTALL, a PACKAGECONFIG, a flag) is a
# platform change. Nothing host-specific (paths, user, time) is recorded:
# the manifest travels in a public release artifact.
#
# Three kinds of file are left out of a layer's content: documentation
# (*.md), component pin files (*FORGEFIRM_MANIFEST_PIN_SUFFIX,
# "<recipe>-pin.inc"), and the release version file
# (FORGEFIRM_MANIFEST_VERSION_SUFFIX, forgefirm-release.inc, which holds the
# version and nothing else). A pin file holds nothing but the SRCREV (and the PV
# that rides with it) of a component that has its own manifest entry; the
# entry already identifies that source file by file, so hashing the pin as
# layer content would turn every component update into a platform change
# and invalidate every acceptance result instead of the tests that cover
# the component. The build stops on a pin file (or the release version
# file) that assigns anything else. Everything else a recipe carries -
# build flags, patches, config fragments, init scripts, the pins of
# third-party sources that have no manifest entry - is layer content and
# stays in the hash. A pin written into a recipe body instead of its pin
# file still hashes: the safe direction (a full campaign, not a missed one).
#
# The "build" section is metadata outside the identity: the revision and
# dirty flag of every layer checkout and of the kas configuration; the
# sha256 of every file each component installed ("installed") and of every
# file under /boot ("boot"), as the root filesystem holds them; the entries
# of each directory that holds one of those files, and of the top, the
# Python library and the module loader's configuration ("dirs"); the path
# of every program and shared object ("code"); and the kernel's release,
# its build ID and the build ID of every module the image carries
# ("kernel"). forgetest compares them with the machine and
# refuses to let a result recorded on a build that is not its pins as built
# be inherited; release.sh refuses a release whose manifest records a dirty
# or local-source build.
#
# content_sha256 is the identity of the build's inputs: sha256 over the
# canonical JSON (sorted keys, no whitespace) of {"components", "platform"}.
# The image name, version string and the build section are metadata
# outside that hash, so the release image and the dev image of one build
# share the same identity, and so does a rebuild from an unchanged tree.

FORGEFIRM_MANIFEST_DIR ?= "${sysconfdir}/forgefirm-manifest.d"
FORGEFIRM_MANIFEST_CONTENT_LAYERS ?= "meta-forgefirm meta-glowforge-bsp meta-openglow-core"
FORGEFIRM_MANIFEST_PIN_SUFFIX ?= "-pin.inc"
# The release version file. Like a pin file it is metadata, not platform
# content: it carries FORGEFIRM_RELEASE and nothing else, the version
# string is already outside the identity hash (below), and the release
# gate proves the number against the rootfs stamp, the .fw meta-version
# and the tag. Hashing it would make every version bump a platform
# change and invalidate the campaign that authorizes the release.
FORGEFIRM_MANIFEST_VERSION_SUFFIX ?= "forgefirm-release.inc"
# The kas lock file: the platform already carries what its commits pin (the
# upstream layers' revisions, meta-openglow's content).
FORGEFIRM_MANIFEST_KAS_SKIP ?= ".lock.yml"

do_rootfs[depends] += "virtual/kernel:do_deploy kernel-module-glowforge:do_deploy"

# The manifest is assembled once the root filesystem is complete, so its
# hashes and directory listings are of the rootfs as imaged. do_rootfs
# (oe.rootfs create()) runs ROOTFS_POSTPROCESS_COMMAND and only then
# removes packages, writes /etc/ld.so.cache (ldconfig) and the modules'
# dependency files (depmod); do_image then runs IMAGE_PREPROCESS_COMMAND,
# where the BSP image's create_dirs (a +=) makes /data at the top. An
# append to IMAGE_PREPROCESS_COMMAND runs after every += and after
# image.bbclass's own append (reproducible_final_image_task, which changes
# only times): the last write to the rootfs before the image types are
# made. image_check reads the built ext4 back against what this records.
# No semicolon after the function name: image.bbclass makes the value of
# the command variables the vardeps of do_rootfs, split on whitespace, so
# "name;" names nothing and a change to the function body would not make
# the rootfs again (execute_pre_post_process itself accepts both).
IMAGE_PREPROCESS_COMMAND:append = " forgefirm_manifest_assemble"
forgefirm_manifest_assemble[vardepsexclude] += "DATETIME"

def forgefirm_manifest_listing(path):
    """The files under `path` that a build reads: tracked ones, and
    untracked ones that the checkout's own .gitignore files do not ignore.
    A local exclude file and the build user's global excludes hide nothing
    from a build, so they hide nothing here."""
    import os, subprocess
    out = subprocess.run(['git', '-c', 'core.excludesFile=/dev/null', 'ls-files', '-z', '--cached',
                          '--others', '--exclude-per-directory=.gitignore', '--', '.'],
                         cwd=path, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=True).stdout
    paths = sorted(set(p.decode('utf-8', 'replace') for p in out.split(b'\0') if p))
    return [p for p in paths if os.path.isfile(os.path.join(path, p))]

def forgefirm_manifest_metadata_problem(name, text, allowed):
    """Why a file left out of the layer content holds more than its rule
    allows, or None. `allowed` names the variables it may assign (regular
    expressions); a comment or a blank line is allowed too, and nothing
    else: no other variable, no include, no override, no continuation."""
    import re
    rx = re.compile(r'^(?:%s)\s*(?:\?\?=|\?=|:=|=)\s*"[^"\\]*"$' % '|'.join(allowed))
    for n, line in enumerate(text.splitlines(), 1):
        s = line.strip()
        if not s or s.startswith('#'):
            continue
        if not rx.match(s):
            return "%s:%d: %s (it may assign only %s)" % (name, n, s, ', '.join(allowed))
    return None

def forgefirm_manifest_layer_content(path, skip_suffixes=('.md',), metadata=()):
    """sha256 over (path, git blob id) of every file under the directory
    (forgefirm_manifest_listing) except those whose name ends in one of
    skip_suffixes. metadata: (suffix, allowed variables) pairs; a file left
    out under such a suffix that assigns anything else stops the build,
    since a change to it would change the build and move no fingerprint."""
    import hashlib, os, subprocess
    paths = []
    for p in forgefirm_manifest_listing(path):
        if not p.endswith(tuple(skip_suffixes)):
            paths.append(p)
            continue
        for suffix, allowed in metadata:
            if p.endswith(suffix):
                with open(os.path.join(path, p), encoding='utf-8', errors='replace') as f:
                    why = forgefirm_manifest_metadata_problem(p, f.read(), allowed)
                if why:
                    bb.fatal("forgefirm-image-manifest: %s is left out of the layer content, and %s"
                             % (os.path.join(path, p), why))
    if not paths:
        return None
    # hash-object --stdin-paths resolves against the repository top level
    prefix = subprocess.check_output(['git', 'rev-parse', '--show-prefix'], cwd=path,
                                     stderr=subprocess.DEVNULL).decode().strip()
    ids = subprocess.run(['git', 'hash-object', '--stdin-paths'], cwd=path,
                         input=('\n'.join(prefix + p for p in paths) + '\n').encode(),
                         stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=True).stdout.decode().split()
    if len(ids) != len(paths):
        bb.fatal("forgefirm-image-manifest: hash-object returned %d ids for %d paths under %s"
                 % (len(ids), len(paths), path))
    h = hashlib.sha256()
    for p, i in zip(paths, ids):
        h.update(p.encode('utf-8'))
        h.update(b'\0')
        h.update(i.encode('ascii'))
        h.update(b'\n')
    return h.hexdigest()

def forgefirm_manifest_dirty(path):
    """Whether the checkout under `path` differs from its commit: a tracked
    file changed, a tracked file whose changes the index is told to ignore
    (skip-worktree or assume-unchanged, which git status does not report),
    or an untracked file that the checkout's own .gitignore files do not
    ignore (the same rule as forgefirm_manifest_listing)."""
    import subprocess
    status = subprocess.check_output(['git', 'status', '--porcelain', '--untracked-files=no', '--', '.'],
                                     cwd=path, stderr=subprocess.DEVNULL)
    others = subprocess.check_output(['git', '-c', 'core.excludesFile=/dev/null', 'ls-files', '-z',
                                      '--others', '--exclude-per-directory=.gitignore', '--', '.'],
                                     cwd=path, stderr=subprocess.DEVNULL)
    tags = subprocess.check_output(['git', 'ls-files', '-z', '-v', '--', '.'], cwd=path,
                                   stderr=subprocess.DEVNULL)
    hidden = any(t[:1] == b'S' or t[:1].islower() for t in tags.split(b'\0') if t)
    return bool(status.strip()) or bool(others.strip(b'\0')) or hidden

def forgefirm_manifest_metadata_rules(d):
    """The suffixes left out of the layer content, and what each file of
    them may assign: a pin file its component's SRCREV and PV, the release
    version file FORGEFIRM_RELEASE."""
    pin = tuple((d.getVar('FORGEFIRM_MANIFEST_PIN_SUFFIX') or '').split())
    version = tuple((d.getVar('FORGEFIRM_MANIFEST_VERSION_SUFFIX') or '').split())
    skip = ('.md',) + pin + version
    metadata = tuple((s, ('SRCREV', 'SRCREV_[A-Za-z0-9_-]+', 'PV')) for s in pin) \
        + tuple((s, ('FORGEFIRM_RELEASE',)) for s in version)
    return skip, metadata

def forgefirm_manifest_layers(d):
    """Returns (identity, build): identity[name] = {"content_sha256"} for
    content-hashed layers or {"rev"} for the rest; build[name] = revision
    and dirty flag of every layer checkout."""
    import os, subprocess
    content_layers = (d.getVar('FORGEFIRM_MANIFEST_CONTENT_LAYERS') or '').split()
    skip, metadata = forgefirm_manifest_metadata_rules(d)
    identity, build = {}, {}
    for layer in (d.getVar('BBLAYERS') or '').split():
        name = os.path.basename(layer.rstrip('/'))
        try:
            rev = subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=layer,
                                          stderr=subprocess.DEVNULL).decode().strip()
            build[name] = {'rev': rev, 'dirty': forgefirm_manifest_dirty(layer)}
        except (subprocess.CalledProcessError, OSError):
            rev = None
            build[name] = {'rev': None, 'dirty': None}
        if name in content_layers:
            if rev is None:
                bb.fatal("forgefirm-image-manifest: layer %s must be a git checkout to be content-hashed" % name)
            identity[name] = {'content_sha256': forgefirm_manifest_layer_content(layer, skip, metadata)}
        else:
            identity[name] = {'rev': rev}
    return identity, build

def forgefirm_manifest_kas(d):
    """Returns (identity, build) of the kas configuration: the kas/
    directory beside the meta-forgefirm layer, in the forgefirm checkout.
    identity is the content hash of its files' text as the tree holds them,
    the lock file left out; build is its dirty flag."""
    import os
    kas = None
    for layer in (d.getVar('BBLAYERS') or '').split():
        if os.path.basename(layer.rstrip('/')) == 'meta-forgefirm':
            kas = os.path.join(os.path.dirname(layer.rstrip('/')), 'kas')
    if kas is None or not os.path.isdir(kas):
        bb.fatal("forgefirm-image-manifest: no kas configuration beside the meta-forgefirm layer")
    skip = ('.md',) + tuple((d.getVar('FORGEFIRM_MANIFEST_KAS_SKIP') or '').split())
    sha = forgefirm_manifest_layer_content(kas, skip)
    if sha is None:
        bb.fatal("forgefirm-image-manifest: no kas configuration file under %s" % kas)
    return {'content_sha256': sha}, {'dirty': forgefirm_manifest_dirty(kas)}

def forgefirm_manifest_sha256_file(path):
    import hashlib
    h = hashlib.sha256()
    with open(path, 'rb') as f:
        for chunk in iter(lambda: f.read(1 << 20), b''):
            h.update(chunk)
    return h.hexdigest()

def forgefirm_manifest_platform(d, rootfs):
    """Returns (platform, build): the platform identity of the image whose
    root filesystem is `rootfs`, and the build section's layer and kas
    state."""
    import glob, os, re
    layer_identity, layer_build = forgefirm_manifest_layers(d)
    kas_identity, kas_build = forgefirm_manifest_kas(d)
    platform = {'machine': d.getVar('MACHINE'), 'layers': layer_identity, 'kas': kas_identity}
    # The modules directory carries the kernel's LOCALVERSION_AUTO hash,
    # which does not reproduce across a re-patch of the same source; the
    # kernel's identity is its pinned SRCREV and config (the @srcrev and
    # @config entries above), so the hash is stripped from the name here.
    platform['kernel_modules'] = sorted(re.sub(r'[+-]g[0-9a-f]{7,}$', '', n)
                                        for n in os.listdir(os.path.join(rootfs, 'lib', 'modules'))) \
        if os.path.isdir(os.path.join(rootfs, 'lib', 'modules')) else []
    dtbs = {}
    for p in sorted(glob.glob(os.path.join(rootfs, 'boot', '**', '*.dtb'), recursive=True)):
        dtbs[os.path.relpath(p, os.path.join(rootfs, 'boot'))] = forgefirm_manifest_sha256_file(p)
    platform['dtb'] = dtbs
    # The debug kernel (kas/forgefirm-glowforge-debug.yml) differs from the
    # normal one only in its configuration, which only the tests that cover
    # the kernel fingerprint. In the platform, the marker moves every
    # fingerprint of the debug image and of no other.
    if (d.getVar('FORGEFIRM_KERNEL_DEBUG') or '').strip() == '1':
        platform['kernel_debug'] = True
    return platform, {'layers': layer_build, 'kas': kas_build}

def forgefirm_manifest_components(entries):
    """Returns (components, installed, kernel_build_id) from the component
    entries: one component per name (the entries of several recipes of one
    component must name the same sources, and a dirty or local-source mark
    of any of them marks the component), {component: [installed path]},
    and the kernel's build ID (the kernel entry's kernel_build_id). Both
    leave the components and go to the build section: they name binaries,
    and the identity names sources."""
    components, installed, kernel_build_id = {}, {}, None
    for e in entries:
        e = dict(e)
        name = e['component']
        recipe = e.pop('recipe', None)
        installed.setdefault(name, set()).update(e.pop('installed', None) or [])
        kernel_build_id = e.pop('kernel_build_id', None) or kernel_build_id
        prev = components.get(name)
        if prev is None:
            e['recipes'] = [recipe] if recipe else []
            components[name] = e
            continue
        if prev.get('srcrev') != e.get('srcrev') or prev.get('files') != e.get('files'):
            bb.fatal("forgefirm-image-manifest: component '%s' is claimed by %s and %s with different sources"
                     % (name, prev['recipes'], recipe))
        for flag in ('dirty', 'local_source'):
            if e.get(flag):
                prev[flag] = True
        if recipe and recipe not in prev['recipes']:
            prev['recipes'].append(recipe)
            prev['recipes'].sort()
    return components, {k: sorted(v) for k, v in installed.items()}, kernel_build_id

def forgefirm_manifest_installed_hashes(rootfs, installed):
    """{component: {path: sha256}} of the files the components installed,
    as the root filesystem holds them (regular files; a path the image
    does not carry is left out)."""
    import os
    out = {}
    for name in sorted(installed):
        hashes = {}
        for p in installed[name]:
            full = os.path.join(rootfs, p.lstrip('/'))
            if os.path.isfile(full) and not os.path.islink(full):
                hashes[p] = forgefirm_manifest_sha256_file(full)
        if hashes:
            out[name] = hashes
    return out

def forgefirm_manifest_boot_hashes(rootfs):
    """{path: sha256} of every regular file under /boot: the kernel, the
    device trees and the boot environment the boot loader reads from the
    root filesystem."""
    import os
    out = {}
    top = os.path.join(rootfs, 'boot')
    for root, dirs, fns in os.walk(top):
        dirs.sort()
        for fn in fns:
            full = os.path.join(root, fn)
            if os.path.isfile(full) and not os.path.islink(full):
                out['/' + os.path.relpath(full, rootfs)] = forgefirm_manifest_sha256_file(full)
    return out

def forgefirm_manifest_code(rootfs):
    """The path of every program and shared object the root filesystem
    holds: each regular file with an execute bit, and each .so file. A
    process may run, or map as code, these and nothing else."""
    import os, re
    so = re.compile(r'\.so(\.[0-9]+)*$')
    out = []
    for root, dirs, fns in os.walk(rootfs):
        dirs.sort()
        for fn in fns:
            full = os.path.join(root, fn)
            if os.path.islink(full) or not os.path.isfile(full):
                continue
            if os.stat(full).st_mode & 0o111 or so.search(fn):
                out.append('/' + os.path.relpath(full, rootfs))
    return sorted(out)

def forgefirm_manifest_watched_dirs(rootfs):
    """Directories listed whether or not a component installs into them:
    the top (a user site, /.local, would start there), the Python standard
    library and its site-packages (a sitecustomize.py, a .pth), and the
    module loader's configuration (options for a module)."""
    import glob, os
    out = ['/', '/etc/modprobe.d', '/etc/modules-load.d']
    for d in sorted(glob.glob(os.path.join(rootfs, 'usr', 'lib', 'python3*'))):
        rel = '/' + os.path.relpath(d, rootfs)
        out += [rel, rel + '/site-packages']
    return [d for d in out if os.path.isdir(os.path.join(rootfs, d.lstrip('/')))]

def forgefirm_manifest_dir_entries(rootfs, paths, also=(), dirs=()):
    """{directory: [entry]} of every directory that holds one of `paths`,
    and of each of `dirs`, as the root filesystem holds it, with the names
    of `also` (what is made after the listing: the manifest itself, the
    ext4's /lost+found) added to theirs: a file the image does not carry
    beside an installed one shows on the machine as an entry no listing
    names."""
    import os
    out = {}
    for d in sorted(set(os.path.dirname(p) for p in paths) | set(dirs)):
        try:
            out[d] = sorted(os.listdir(os.path.join(rootfs, d.lstrip('/'))))
        except OSError:
            continue
    for p in also:
        d = os.path.dirname(p)
        if d in out and os.path.basename(p) not in out[d]:
            out[d] = sorted(out[d] + [os.path.basename(p)])
    return out

def forgefirm_manifest_note_build_id(data):
    """The GNU build ID (hex) in ELF note bytes, or None: namesz 4, descsz,
    type 3 (NT_GNU_BUILD_ID), "GNU" and a NUL, then the ID; little-endian, as the
    machine is (forgetest's provenance.note_build_id reads it the same
    way from the running kernel and modules)."""
    i = data.find(b'GNU\x00')
    while i != -1:
        if i >= 12 and data[i - 12:i - 8] == b'\x04\x00\x00\x00' and data[i - 4:i] == b'\x03\x00\x00\x00':
            n = int.from_bytes(data[i - 8:i - 4], 'little')
            if 0 < n <= 64 and len(data) >= i + 4 + n:
                return data[i + 4:i + 4 + n].hex()
        i = data.find(b'GNU\x00', i + 1)
    return None

def forgefirm_manifest_kernel(rootfs, build_id):
    """The kernel the image carries: its release (each /lib/modules
    directory's name, hash and all), its build ID, and {module: build ID}
    of every module under /lib/modules, named as the kernel lists it
    (dashes as underscores)."""
    import gzip, lzma, os
    top = os.path.join(rootfs, 'lib', 'modules')
    releases = sorted(os.listdir(top)) if os.path.isdir(top) else []
    modules = {}
    for rel in releases:
        for root, dirs, fns in os.walk(os.path.join(top, rel)):
            dirs.sort()
            for fn in sorted(fns):
                for ext, opener in (('.ko', open), ('.ko.gz', gzip.open), ('.ko.xz', lzma.open)):
                    if fn.endswith(ext):
                        with opener(os.path.join(root, fn), 'rb') as f:
                            bid = forgefirm_manifest_note_build_id(f.read())
                        modules[fn[:-len(ext)].replace('-', '_')] = bid
                        break
    return {'release': releases, 'build_id': build_id or None, 'modules': modules}

def forgefirm_manifest_document(name, version, components, platform, build):
    """The manifest: content_sha256 over the components and the platform
    only, so nothing in the build section moves the identity."""
    import hashlib, json
    canonical = json.dumps({'components': components, 'platform': platform},
                           sort_keys=True, separators=(',', ':'))
    return {
        'format': 1,
        'image': {'name': name, 'version': version},
        'build': build,
        'content_sha256': hashlib.sha256(canonical.encode('utf-8')).hexdigest(),
        'components': components,
        'platform': platform,
    }

python forgefirm_manifest_assemble() {
    import glob, json, os

    rootfs = d.getVar('IMAGE_ROOTFS')
    entries = []
    for p in sorted(glob.glob(rootfs + d.getVar('FORGEFIRM_MANIFEST_DIR') + '/*.json')) \
            + sorted(glob.glob(os.path.join(d.getVar('DEPLOY_DIR_IMAGE'), 'forgefirm-manifest.d', '*.json'))):
        with open(p) as f:
            entries.append(json.load(f))
    components, installed, kernel_build_id = forgefirm_manifest_components(entries)
    if not components:
        bb.fatal("forgefirm-image-manifest: no component entries found")

    platform, build = forgefirm_manifest_platform(d, rootfs)
    build['installed'] = forgefirm_manifest_installed_hashes(rootfs, installed)
    build['boot'] = forgefirm_manifest_boot_hashes(rootfs)
    target = os.path.join(rootfs, 'etc', 'forgefirm-manifest.json')
    listed = [p for files in build['installed'].values() for p in files] + list(build['boot'])
    build['dirs'] = forgefirm_manifest_dir_entries(rootfs, listed,
                                                   also=['/etc/forgefirm-manifest.json', '/lost+found'],
                                                   dirs=forgefirm_manifest_watched_dirs(rootfs))
    build['code'] = forgefirm_manifest_code(rootfs)
    build['kernel'] = forgefirm_manifest_kernel(rootfs, kernel_build_id)
    manifest = forgefirm_manifest_document(d.getVar('PN'), d.getVar('FORGEFIRM_VERSION_STRING'),
                                           components, platform, build)
    text = json.dumps(manifest, sort_keys=True, indent=1) + '\n'

    with open(target, 'w') as f:
        f.write(text)
    os.chmod(target, 0o644)

    deploy = d.getVar('IMGDEPLOYDIR')
    if deploy:
        name = d.getVar('IMAGE_NAME') + '.forgefirm-manifest.json'
        with open(os.path.join(deploy, name), 'w') as f:
            f.write(text)
        link = os.path.join(deploy, d.getVar('IMAGE_LINK_NAME') + '.forgefirm-manifest.json')
        if os.path.lexists(link):
            os.remove(link)
        os.symlink(name, link)
    bb.note("forgefirm-image-manifest: %d components, content_sha256 %s"
            % (len(components), manifest['content_sha256']))
}
