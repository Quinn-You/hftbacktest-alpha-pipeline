"""基于日度 repo 生成区间（月度/多月）汇总报表。"""

from .core import DateRepo
from .core import build_period_repo

__all__ = ["DateRepo", "build_period_repo"]
