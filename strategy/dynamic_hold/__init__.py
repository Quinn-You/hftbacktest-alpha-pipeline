"""持仓期内按 alpha 相对开仓基准阶梯调仓；止盈/止损/到期/信号反向（达阈值，与开仓对称）任一平仓。

run_dynamic_hold_backtest 返回的 summary 与 legacy 内核一致包含 after_1445 系列字段及
``shares_unflattenable_after_1445``（见 engine 模块文档）。
"""

from .engine import DynamicPosition
from .engine import run_dynamic_hold_backtest

__all__ = ["DynamicPosition", "run_dynamic_hold_backtest"]
