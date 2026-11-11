# SPDX-License-Identifier: MIT

# grblHAL-glowforge pin. Bump deliberately after pushing grblHAL-glowforge
# changes; keep only SRCREV and PV here - the image manifest leaves
# *-pin.inc out of the layer content hash because the component entry
# already identifies the pinned source (forgefirm-image-manifest.bbclass).
SRCREV = "ad734238205f8686f72f15e9b79343479e230b8e"
PV = "0.1.24"
