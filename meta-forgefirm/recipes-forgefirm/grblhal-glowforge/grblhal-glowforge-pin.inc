# grblHAL-glowforge pin. Bump deliberately after pushing grblHAL-glowforge
# changes; keep only SRCREV and PV here - the image manifest leaves
# *-pin.inc out of the layer content hash because the component entry
# already identifies the pinned source (forgefirm-image-manifest.bbclass).
SRCREV = "16d2b9e1c1381a074e30122a6869cfd39db1dbd4"
PV = "0.1.12"
