"""Module alias for the shared remote matting v1 DTOs."""

import sys
from contracts import remote_protocol as _shared

sys.modules[__name__] = _shared
