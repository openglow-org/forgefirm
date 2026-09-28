# SPDX-License-Identifier: MIT

# forgeext pin. Bump deliberately after pushing forgeext changes; keep
# only SRCREV and PV here - the image manifest leaves *-pin.inc out of the
# layer content hash because the component entry already identifies the
# pinned source (forgefirm-image-manifest.bbclass).
SRCREV = "2d0391e7f51922848bc2ac30240ac9f5dd65af20"
PV = "0.1.1"
