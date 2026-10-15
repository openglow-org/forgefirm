# SPDX-License-Identifier: MIT

# forgeext pin. Bump deliberately after pushing forgeext changes; keep
# only SRCREV and PV here - the image manifest leaves *-pin.inc out of the
# layer content hash because the component entry already identifies the
# pinned source (forgefirm-image-manifest.bbclass).
SRCREV = "e09f7b8060af995b0ea5d2c5bfa1d23f00636261"
PV = "0.1.3"
