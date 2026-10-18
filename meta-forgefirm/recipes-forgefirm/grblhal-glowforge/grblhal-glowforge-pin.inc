# SPDX-License-Identifier: MIT

# grblHAL-glowforge pin. Bump deliberately after pushing grblHAL-glowforge
# changes; keep only SRCREV and PV here - the image manifest leaves
# *-pin.inc out of the layer content hash because the component entry
# already identifies the pinned source (forgefirm-image-manifest.bbclass).
SRCREV = "e15972005c6e3e3b5bcb4d7408df0eaf83b5cc43"
PV = "0.1.22"
