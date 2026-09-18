# SPDX-License-Identifier: MIT

# forgefirm-manifest.bbclass - source fingerprint of one ForgeFIRM component
#
# A component recipe that inherits this class records what source it was
# built from: the component name, the pinned revision, and one
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
# Sources: a git checkout in ${S} is fingerprinted with `git ls-tree`
# (submodules recursed, the gitlink kept). Non-git sources (file:// recipes)
# set FORGEFIRM_MANIFEST_SRC to the directory to fingerprint; the ids are
# computed with `git hash-object`, so they compare with tree ids.

FORGEFIRM_MANIFEST_NAME ?= "${PN}"
FORGEFIRM_MANIFEST_MODE ?= "rootfs"
FORGEFIRM_MANIFEST_SRC ?= ""
FORGEFIRM_MANIFEST_DIR = "${sysconfdir}/forgefirm-manifest.d"

def forgefirm_manifest_git(args, cwd):
    import subprocess
    return subprocess.check_output(['git'] + args, cwd=cwd,
                                   stderr=subprocess.STDOUT).decode('utf-8', 'replace')

def forgefirm_manifest_tree(files, repo, prefix):
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
    elif os.path.exists(os.path.join(s, '.git')):
        entry['srcrev'] = forgefirm_manifest_git(['rev-parse', 'HEAD'], s).strip()
        uri = (d.getVar('SRC_URI') or '').split()
        entry['source'] = uri[0].split(';')[0] if uri else None
        forgefirm_manifest_tree(entry['files'], s, '')
    else:
        bb.fatal("forgefirm-manifest: %s: ${S} is not a git checkout and FORGEFIRM_MANIFEST_SRC is unset" % pn)
    entry['files'].sort()
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
do_forgefirm_manifest[vardeps] += "FORGEFIRM_MANIFEST_NAME FORGEFIRM_MANIFEST_SRC SRCREV SRC_URI \
    forgefirm_manifest_entry forgefirm_manifest_tree forgefirm_manifest_git \
    forgefirm_manifest_hash_dir forgefirm_manifest_git_prefix forgefirm_manifest_write"

python __anonymous() {
    if d.getVar('FORGEFIRM_MANIFEST_MODE') == 'rootfs':
        bb.build.addtask('do_forgefirm_manifest', 'do_package do_populate_sysroot', 'do_install', d)
}

FILES:${PN}:append = " ${FORGEFIRM_MANIFEST_DIR}"
