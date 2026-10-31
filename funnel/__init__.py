"""Funnel — Windows game in, Linux desktop launcher out. Local and deterministic."""

from .detect import Detected, detect_input
from .pack import PackResult, pack_path

__version__ = "0.2.0"
__all__ = ["pack_path", "PackResult", "detect_input", "Detected", "__version__"]
