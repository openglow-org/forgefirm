# Copyright 2026 514 LLC d/b/a OpenGlow
# Written by Scott Wiederhold
# https://community.openglow.org
# SPDX-License-Identifier:    MIT

"""The acceptance suite: one module per subsystem, imported in display
order. Each module registers its tests with @catalog.test."""

from . import image      # noqa: F401,E402
from . import kernel     # noqa: F401,E402
from . import forgectrl  # noqa: F401,E402
from . import setup # noqa: F401,E402
from . import setup_dark  # noqa: F401,E402
from . import setup_sheet  # noqa: F401,E402
from . import logs       # noqa: F401,E402
from . import motion     # noqa: F401,E402
from . import cooling    # noqa: F401,E402
from . import laser      # noqa: F401,E402
from . import camera     # noqa: F401,E402
from . import update     # noqa: F401,E402
from . import cloud      # noqa: F401,E402
