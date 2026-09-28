# SPDX-License-Identifier: MIT

# grblHAL-glowforge pin. Bump deliberately after pushing grblHAL-glowforge
# changes; keep only SRCREV and PV here - the image manifest leaves
# *-pin.inc out of the layer content hash because the component entry
# already identifies the pinned source (forgefirm-image-manifest.bbclass).
SRCREV = "b34636f5fbce586e375c7d4c4d474ee20c3c69b7"
PV = "0.1.20"
