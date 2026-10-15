# SPDX-License-Identifier: MIT

# forgectrl pin. Bump deliberately after pushing forgectrl changes; keep
# only SRCREV and PV here - the image manifest leaves *-pin.inc out of the
# layer content hash because the component entry already identifies the
# pinned source (forgefirm-image-manifest.bbclass).
SRCREV = "22a2cfc04242ddc50ed0a54e967a59634f3f4d6d"
PV = "0.1.32"
