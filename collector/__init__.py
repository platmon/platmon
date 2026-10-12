"""Platform collection entrypoints; runtime scheduling lives in runtime/."""
from .core import BOARDS, BOARD, PLATFORM, collect, collect_recorded, detect

__all__ = ["BOARDS", "BOARD", "PLATFORM", "collect", "collect_recorded", "detect"]
