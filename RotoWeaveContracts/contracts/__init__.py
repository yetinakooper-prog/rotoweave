"""RotoWeave 4.0 cross-application contracts.

This package is the only Python dependency shared by the client and remote
server. It must not import either application implementation.
"""

from .paths import MODELS_ROOT_ENV, resolve_models_root

__all__ = ["MODELS_ROOT_ENV", "resolve_models_root"]
