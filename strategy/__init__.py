"""交易与回测策略层。

子包 **``strategy.aum``**：AUM 双 ledger 与开仓名义裁剪（实现见 ``strategy.aum.budget``，由 legacy / dynamic_hold 引用）。子包 **``strategy.twap``**：按名义切片（由 ``dynamic_hold`` 在启用 TWAP 时使用）。
"""

from .run import PreparedSymbolInput
from .run import StrategyConfig
from .run import StrategyRunResult
from .run import StrategyUniverseInput
from .run import run_universe_strategy

__all__ = [
	"PreparedSymbolInput",
	"StrategyConfig",
	"StrategyRunResult",
	"StrategyUniverseInput",
	"run_universe_strategy",
]
