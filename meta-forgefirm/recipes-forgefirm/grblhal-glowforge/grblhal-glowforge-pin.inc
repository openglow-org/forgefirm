# SPDX-License-Identifier: MIT

# grblHAL-glowforge pin. Bump deliberately after pushing grblHAL-glowforge
# changes; keep only SRCREV and PV here - the image manifest leaves
# *-pin.inc out of the layer content hash because the component entry
# already identifies the pinned source (forgefirm-image-manifest.bbclass).
SRCREV = "0da0fe64ec40baf0be2114e1cdcd0a3767ff9846"
PV = "0.1.19"
