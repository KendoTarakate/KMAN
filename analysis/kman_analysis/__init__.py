"""KMAN Step 1 offline analysis: breathing, chest-abdomen phase, neck events."""

__version__ = "1.0.0"

from .config import load_config
from .pipeline import Analysis, analyze

__all__ = ["Analysis", "analyze", "load_config"]
