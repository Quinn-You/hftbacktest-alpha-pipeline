"""固定持仓 legacy 策略：阈值开仓，持有至目标时刻或日末强平。"""

from .engine import OpenPosition
from .engine import run_backtest_for_alpha_records

__all__ = ["OpenPosition", "run_backtest_for_alpha_records"]
