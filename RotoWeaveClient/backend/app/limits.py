from __future__ import annotations


MIB = 1024 * 1024
GIB = 1024 * MIB

# Upload limits are product contracts rather than web-framework defaults.  They
# are centralized here so the API, worker and documentation cannot drift.
MAX_CORE_IMAGE_UPLOAD_BYTES = 32 * MIB
MAX_ATLAS_REPAIR_UPLOAD_BYTES = 128 * MIB
MAX_MEDIA_UPLOAD_BYTES = 8 * GIB
MAX_SHEET_RGBA_BYTES = 1 * GIB
MAX_SHEET_UPLOAD_BYTES = 2 * GIB
