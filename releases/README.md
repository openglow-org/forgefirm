# Release acceptance artifacts

One directory per release, `v<version>/`, holding the `acceptance.json` and
`acceptance.md` that forgetest exported on the bench for that release.
`scripts/release.sh` refuses a release whose artifact does not authorize the
built rootfs; see https://docs.forgefirm.org/developers/acceptance/.

`allowed_signers` holds, in `ssh-keygen`'s allowed-signers format, the public
halves of the bench keys that sign the artifacts (principal `forgetest-bench`)
and of the key that signs each release's `sha256sums.txt` (principal
`forgefirm-sums`); see https://docs.forgefirm.org/developers/release-flow/.
