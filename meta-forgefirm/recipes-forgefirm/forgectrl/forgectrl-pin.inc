# SPDX-License-Identifier: MIT

# forgectrl pin. Bump deliberately after pushing forgectrl changes; keep
# only SRCREV and PV here - the image manifest leaves *-pin.inc out of the
# layer content hash because the component entry already identifies the
# pinned source (forgefirm-image-manifest.bbclass).
SRCREV = "55bfae9005ecf4578f8778bb3dc7f406bffed762"
PV = "0.1.28"
