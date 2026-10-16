"""Standalone HYBRIQX buyer; no server, model runner, or exercise execution."""

from .client import BuyerError, adapt_workout, offline_demo, run_workout
from .contracts import WODScalingRequest, WODScalingResponse

__version__ = "0.1.0"
__all__ = [
    "BuyerError", "adapt_workout", "run_workout", "offline_demo",
    "WODScalingRequest", "WODScalingResponse",
]
