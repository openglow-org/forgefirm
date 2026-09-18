# SPDX-License-Identifier: MIT

# NXP distributes the i.MX VPU/EPDC firmware blobs under its firmware EULA
# (accepted with ACCEPT_FSL_EULA), so an image carrying those blobs has to
# carry the license text with them.
#
# LICENSE_CREATE_PACKAGE builds the license package, but license.bbclass adds
# it to PACKAGES during do_package - too late for an image to depend on the
# name. Declaring it here makes it resolvable at parse time; the class then
# logs one "package already existed" note for this recipe, which is why FILES
# is set here as well.
LICENSE_CREATE_PACKAGE = "1"

PACKAGES:prepend = "${PN}-lic "
FILES:${PN}-lic = "${datadir}/licenses/${PN}"
