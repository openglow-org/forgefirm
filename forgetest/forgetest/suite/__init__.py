"""The acceptance suite: one module per subsystem, imported in display
order. Each module registers its tests with @catalog.test."""

from . import image      # noqa: F401,E402
from . import kernel     # noqa: F401,E402
from . import forgectrl  # noqa: F401,E402
from . import commission # noqa: F401,E402
from . import commission_dark  # noqa: F401,E402
from . import commission_sheet  # noqa: F401,E402
from . import logs       # noqa: F401,E402
from . import motion     # noqa: F401,E402
from . import cooling    # noqa: F401,E402
from . import laser      # noqa: F401,E402
from . import camera     # noqa: F401,E402
from . import update     # noqa: F401,E402
from . import cloud      # noqa: F401,E402
