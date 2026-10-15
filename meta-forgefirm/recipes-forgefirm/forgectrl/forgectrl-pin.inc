# SPDX-License-Identifier: MIT

# forgectrl pin. Bump deliberately after pushing forgectrl changes; keep
# only SRCREV and PV here - the image manifest leaves *-pin.inc out of the
# layer content hash because the component entry already identifies the
# pinned source (forgefirm-image-manifest.bbclass).
SRCREV = "c9e3347bdcc6535056e5de5e1d4bd38e93de9494"
PV = "0.1.31"
