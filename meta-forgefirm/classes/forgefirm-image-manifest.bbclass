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
# version), the device tree blob hashes, and the layers. Layers named in
# FORGEFIRM_MANIFEST_CONTENT_LAYERS are identified by content (tracked and
# untracked-unignored files under the layer directory, git blob ids), so
# a build from a dirty working tree and a later build from the identical
# committed tree agree; the other layers are identified by revision.
# Revisions and dirty flags of the content-hashed layers are informational
# and live in the "build" section, outside the identity. Nothing
# host-specific (paths, user, time) is recorded: the manifest travels in
# a public release artifact.
#
# Two kinds of file are left out of a layer's content: documentation
# (*.md) and component pin files (*FORGEFIRM_MANIFEST_PIN_SUFFIX,
# "<recipe>-pin.inc"). A pin file holds nothing but the SRCREV (and the PV
# that rides with it) of a component that has its own manifest entry; the
# entry already identifies that source file by file, so hashing the pin as
# layer content would turn every component update into a platform change
# and invalidate every acceptance result instead of the tests that cover
# the component. Everything else a recipe carries - build flags, patches,
# config fragments, init scripts, the pins of third-party sources that
# have no manifest entry - is layer content and stays in the hash. A pin
# written into a recipe body instead of its pin file still hashes: the
# safe direction (a full campaign, not a missed one).
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

do_rootfs[depends] += "virtual/kernel:do_deploy kernel-module-glowforge:do_deploy"

# No semicolon after the function name: image.bbclass makes the value of
# ROOTFS_POSTPROCESS_COMMAND the vardeps of do_rootfs, split on whitespace,
# so "name;" names nothing and a change to the function body would not
# make the rootfs again (execute_pre_post_process itself accepts both).
ROOTFS_POSTPROCESS_COMMAND += "forgefirm_manifest_assemble "
forgefirm_manifest_assemble[vardepsexclude] += "DATETIME"

def forgefirm_manifest_layer_content(path, skip_suffixes=('.md',)):
    """sha256 over (path, git blob id) of every file under the layer
    directory except those whose name ends in one of skip_suffixes."""
    import hashlib, os, subprocess
    out = subprocess.run(['git', 'ls-files', '-z', '--cached', '--others', '--exclude-standard', '--', '.'],
                         cwd=path, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=True).stdout
    paths = sorted(set(p.decode('utf-8', 'replace') for p in out.split(b'\0') if p))
    paths = [p for p in paths
             if os.path.isfile(os.path.join(path, p)) and not p.endswith(tuple(skip_suffixes))]
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

def forgefirm_manifest_layers(d):
    """Returns (identity, build): identity[name] = {"content_sha256"} for
    content-hashed layers or {"rev"} for the rest; build[name] = revision
    and dirty flag of every layer checkout (informational)."""
    import os, subprocess
    content_layers = (d.getVar('FORGEFIRM_MANIFEST_CONTENT_LAYERS') or '').split()
    skip = (('.md',) + tuple((d.getVar('FORGEFIRM_MANIFEST_PIN_SUFFIX') or '').split())
            + tuple((d.getVar('FORGEFIRM_MANIFEST_VERSION_SUFFIX') or '').split()))
    identity, build = {}, {}
    for layer in (d.getVar('BBLAYERS') or '').split():
        name = os.path.basename(layer.rstrip('/'))
        try:
            rev = subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=layer,
                                          stderr=subprocess.DEVNULL).decode().strip()
            status = subprocess.check_output(['git', 'status', '--porcelain', '--', '.'], cwd=layer,
                                             stderr=subprocess.DEVNULL).decode()
            build[name] = {'rev': rev, 'dirty': bool(status.strip())}
        except (subprocess.CalledProcessError, OSError):
            rev = None
            build[name] = {'rev': None, 'dirty': None}
        if name in content_layers:
            if rev is None:
                bb.fatal("forgefirm-image-manifest: layer %s must be a git checkout to be content-hashed" % name)
            identity[name] = {'content_sha256': forgefirm_manifest_layer_content(layer, skip)}
        else:
            identity[name] = {'rev': rev}
    return identity, build

def forgefirm_manifest_sha256_file(path):
    import hashlib
    h = hashlib.sha256()
    with open(path, 'rb') as f:
        for chunk in iter(lambda: f.read(1 << 20), b''):
            h.update(chunk)
    return h.hexdigest()

python forgefirm_manifest_assemble() {
    import glob, hashlib, json, os, re

    rootfs = d.getVar('IMAGE_ROOTFS')
    components = {}

    def add(path):
        with open(path) as f:
            e = json.load(f)
        name = e['component']
        recipe = e.pop('recipe', None)
        prev = components.get(name)
        if prev is None:
            e['recipes'] = [recipe] if recipe else []
            components[name] = e
            return
        if prev.get('srcrev') != e.get('srcrev') or prev.get('files') != e.get('files'):
            bb.fatal("forgefirm-image-manifest: component '%s' is claimed by %s and %s with different sources"
                     % (name, prev['recipes'], recipe))
        if recipe and recipe not in prev['recipes']:
            prev['recipes'].append(recipe)
            prev['recipes'].sort()

    for p in sorted(glob.glob(rootfs + d.getVar('FORGEFIRM_MANIFEST_DIR') + '/*.json')):
        add(p)
    for p in sorted(glob.glob(os.path.join(d.getVar('DEPLOY_DIR_IMAGE'), 'forgefirm-manifest.d', '*.json'))):
        add(p)
    if not components:
        bb.fatal("forgefirm-image-manifest: no component entries found")

    layer_identity, layer_build = forgefirm_manifest_layers(d)
    platform = {'machine': d.getVar('MACHINE'), 'layers': layer_identity}
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

    canonical = json.dumps({'components': components, 'platform': platform},
                           sort_keys=True, separators=(',', ':'))
    manifest = {
        'format': 1,
        'image': {'name': d.getVar('PN'), 'version': d.getVar('FORGEFIRM_VERSION_STRING')},
        'build': {'layers': layer_build},
        'content_sha256': hashlib.sha256(canonical.encode('utf-8')).hexdigest(),
        'components': components,
        'platform': platform,
    }
    text = json.dumps(manifest, sort_keys=True, indent=1) + '\n'

    target = os.path.join(rootfs, 'etc', 'forgefirm-manifest.json')
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
