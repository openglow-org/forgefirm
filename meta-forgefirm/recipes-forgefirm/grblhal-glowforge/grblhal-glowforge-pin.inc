# SPDX-License-Identifier: MIT

# grblHAL-glowforge pin. Bump deliberately after pushing grblHAL-glowforge
# changes; keep only SRCREV and PV here - the image manifest leaves
# *-pin.inc out of the layer content hash because the component entry
# already identifies the pinned source (forgefirm-image-manifest.bbclass).
SRCREV = "6218dcd1e00dd28c40e06b4b8c6c4c705089b87e"
PV = "0.1.23"
