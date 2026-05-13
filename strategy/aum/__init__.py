"""策略层 AUM：双 ledger（``used_long`` / ``used_short``）、仅开仓累计；供 legacy / dynamic_hold 共用。

- ``strategy.aum.budget``：裁剪与 ledger 增量计算
"""

from .budget import capped_entry_shares
from .budget import clip_notional
from .budget import open_ledger_increments

__all__ = [
	"clip_notional",
	"open_ledger_increments",
	"capped_entry_shares",
]
