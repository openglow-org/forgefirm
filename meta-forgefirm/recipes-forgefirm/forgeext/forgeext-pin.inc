# SPDX-License-Identifier: MIT

# forgeext pin. Bump deliberately after pushing forgeext changes; keep
# only SRCREV and PV here - the image manifest leaves *-pin.inc out of the
# layer content hash because the component entry already identifies the
# pinned source (forgefirm-image-manifest.bbclass).
SRCREV = "ebd959256d3da9f648d6975ba740bee86ad69c7b"
PV = "0.1.5"
