# SPDX-License-Identifier: MIT

# forgectrl pin. Bump deliberately after pushing forgectrl changes; keep
# only SRCREV and PV here - the image manifest leaves *-pin.inc out of the
# layer content hash because the component entry already identifies the
# pinned source (forgefirm-image-manifest.bbclass).
SRCREV = "c274f19b09f2c0b0b553edbbe56cb9d9c193703e"
PV = "0.1.30"
