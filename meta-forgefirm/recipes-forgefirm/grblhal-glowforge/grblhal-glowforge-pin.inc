# grblHAL-glowforge pin. Bump deliberately after pushing grblHAL-glowforge
# changes; keep only SRCREV and PV here - the image manifest leaves
# *-pin.inc out of the layer content hash because the component entry
# already identifies the pinned source (forgefirm-image-manifest.bbclass).
SRCREV = "d902e37e11530e8f7f48eefdfa62d8191f8c718a"
PV = "0.1.7"
