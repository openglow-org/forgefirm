# SPDX-License-Identifier: MIT

DESCRIPTION = "Shared ForgeFIRM web-service hardware-machine glue (gfhome + gfcloud)"

require forgefirm-app.inc

inherit python3-dir

do_install() {
    install -Dm 0644 ${S}/forgefirm-app/ffmachine.py ${D}${PYTHON_SITEPACKAGES_DIR}/ffmachine.py
}

FILES:${PN} += "${PYTHON_SITEPACKAGES_DIR}/ffmachine.py"

RDEPENDS:${PN} += "python3-core python3-gfhardware python3-gfutilities python3-requests"
