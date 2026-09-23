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
# Last: it stands on the setup record's consent and on the cloud suite's dark print.
from . import exthost    # noqa: F401,E402
from . import extcore    # noqa: F401,E402
from . import extcall    # noqa: F401,E402
from . import extdest    # noqa: F401,E402
from . import extlife    # noqa: F401,E402
from . import evmore     # noqa: F401,E402
from . import extcat     # noqa: F401,E402
from . import extmcode   # noqa: F401,E402
from . import extwizard  # noqa: F401,E402
from . import updlock    # noqa: F401,E402
from . import homeoff    # noqa: F401,E402
from . import bedsize    # noqa: F401,E402
