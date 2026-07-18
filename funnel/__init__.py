"""Funnel — Windows .exe in, Linux desktop app out. Mechanical. No LLM."""

from .pack import pack_path, PackResult
from .detect import InspectionResult, detect_input, inspect_payload

__version__ = "0.1.0"
__all__ = [
    "pack_path",
    "PackResult",
    "detect_input",
    "inspect_payload",
    "InspectionResult",
    "__version__",
]
