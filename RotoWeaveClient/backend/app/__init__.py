"""Application package for the RotoWeave local API."""

import os

# OpenCV reads this switch when its native image-codec module is initialized.
# Set it at package import time, before any sibling module can import cv2.
os.environ.setdefault("OPENCV_IO_ENABLE_OPENEXR", "1")

from contracts.product import PRODUCT_VERSION

__version__ = PRODUCT_VERSION
