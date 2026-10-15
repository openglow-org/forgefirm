# SPDX-License-Identifier: MIT

# forgeext pin. Bump deliberately after pushing forgeext changes; keep
# only SRCREV and PV here - the image manifest leaves *-pin.inc out of the
# layer content hash because the component entry already identifies the
# pinned source (forgefirm-image-manifest.bbclass).
SRCREV = "bb75b23088b6ecce502ee3122bfb089b1c2641f1"
PV = "0.1.4"
