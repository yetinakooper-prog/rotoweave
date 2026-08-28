from __future__ import annotations

import os
import sys
from pathlib import Path


# OpenCV reads this option when its native module is first imported.  Several
# test modules import cv2 before backend.app, so mirror the production package
# bootstrap here.
os.environ.setdefault("OPENCV_IO_ENABLE_OPENEXR", "1")

CONTRACTS_ROOT = Path(__file__).resolve().parents[3] / "RotoWeaveContracts"
if str(CONTRACTS_ROOT) not in sys.path:
    sys.path.insert(0, str(CONTRACTS_ROOT))
