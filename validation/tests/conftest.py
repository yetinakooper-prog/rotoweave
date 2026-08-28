from __future__ import annotations

import os
import sys
from pathlib import Path


WORKSPACE = Path(__file__).resolve().parents[2]
os.environ.setdefault("OPENCV_IO_ENABLE_OPENEXR", "1")
for source_root in (
    WORKSPACE / "RotoWeaveClient",
    WORKSPACE / "RotoWeaveServer",
    WORKSPACE / "RotoWeaveContracts",
    WORKSPACE,
):
    value = str(source_root)
    if value not in sys.path:
        sys.path.insert(0, value)
