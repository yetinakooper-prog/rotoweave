from __future__ import annotations

import sys
from pathlib import Path


SERVER_ROOT = Path(__file__).resolve().parents[1]
CONTRACTS_ROOT = SERVER_ROOT.parent / "RotoWeaveContracts"
for root in (SERVER_ROOT, CONTRACTS_ROOT):
    if str(root) not in sys.path:
        sys.path.insert(0, str(root))
