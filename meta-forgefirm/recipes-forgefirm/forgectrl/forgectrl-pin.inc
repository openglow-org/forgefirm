# SPDX-License-Identifier: MIT

# forgectrl pin. Bump deliberately after pushing forgectrl changes; keep
# only SRCREV and PV here - the image manifest leaves *-pin.inc out of the
# layer content hash because the component entry already identifies the
# pinned source (forgefirm-image-manifest.bbclass).
SRCREV = "6c2bb389fa21ebefbde200e03e9b6487e9940369"
PV = "0.1.33"
