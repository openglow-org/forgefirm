# SPDX-License-Identifier: MIT

# forgeext pin. Bump deliberately after pushing forgeext changes; keep
# only SRCREV and PV here - the image manifest leaves *-pin.inc out of the
# layer content hash because the component entry already identifies the
# pinned source (forgefirm-image-manifest.bbclass).
SRCREV = "711f09955c0120f30e142d54b52e47163617fedc"
PV = "0.1.0"
