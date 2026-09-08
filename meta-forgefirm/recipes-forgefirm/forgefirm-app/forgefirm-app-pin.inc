# forgefirm-app pin (python3-gfhardware repository, forgefirm-app/ tree).
# Bump deliberately (AUTOREV is not reproducible); keep only SRCREV and PV
# here - the image manifest leaves *-pin.inc out of the layer content hash
# because the component entry already identifies the pinned source
# (forgefirm-image-manifest.bbclass). The python3-gfhardware recipe in
# meta-glowforge-bsp pins the same repository; move both together.
SRCREV = "53d57f7c84fd314012f21bbbee883dde95353ded"

# Bump PV with every SRCREV move: the hash-derived package version is not
# monotonic on its own and buildhistory QA fails the build when it sorts
# backwards.
PV = "0.1.27+git"
