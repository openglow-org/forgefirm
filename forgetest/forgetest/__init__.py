# Copyright 2026 514 LLC d/b/a OpenGlow
# Written by Scott Wiederhold
# https://community.openglow.org
# SPDX-License-Identifier:    MIT

"""forgetest - the ForgeFIRM release acceptance tool.

A daemon on the dev image (HTTP :8090) that runs the acceptance catalog
against the machine, keeps the append-only result log, decides which
results still apply to the image that is running, and exports the
release artifact the release gate reads. The bench diagnostics page rides
the same daemon.
"""

VERSION = "0.1.0"
