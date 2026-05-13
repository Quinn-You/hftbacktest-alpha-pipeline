"""AUM 回测侧：多空分账 open ledger、开仓前名义裁剪（引擎内调用）。"""

from __future__ import annotations


def clip_notional(
	requested: float,
	aum: float | None,
	used_open: float,
	clip_frac: float | None,
) -> float:
	"""``effective_notional = min(requested, aum - used_open)``；可选 ``clip_frac`` 限制单笔不超过 ``remaining * frac``。"""
	if aum is None:
		return float(requested)
	if aum <= 0:
		return 0.0
	rem = max(0.0, float(aum) - float(used_open))
	out = min(float(requested), rem)
	if clip_frac is not None and clip_frac > 0 and rem > 0:
		out = min(out, rem * float(clip_frac))
	return max(0.0, out)


def open_ledger_increments(
	before_long: int,
	before_short: int,
	after_long: int,
	after_short: int,
	exec_price: float,
) -> tuple[float, float]:
	"""成交后相对成交前，多头股数、空头股数若有增加，则各自增加 open ledger 名义。"""
	px = float(exec_price)
	inc_long = max(0, int(after_long) - int(before_long))
	inc_short = max(0, int(after_short) - int(before_short))
	return px * float(inc_long), px * float(inc_short)


def capped_entry_shares(
	entry_ref_px: float,
	shares_uncapped: int,
	lot_size: int,
	aum: float | None,
	used_open_for_side: float,
	clip_frac: float | None,
) -> int:
	"""按**该开仓方向**已用 open 名义 ``used_open_for_side`` 裁剪意图股数（整手）。"""
	if shares_uncapped <= 0 or entry_ref_px <= 0:
		return 0
	if aum is None:
		return int(shares_uncapped)
	if aum <= 0:
		return 0
	req = float(entry_ref_px) * float(shares_uncapped)
	eff = clip_notional(req, aum, used_open_for_side, clip_frac)
	sh = int((eff // float(entry_ref_px)) // int(lot_size)) * int(lot_size)
	return min(int(shares_uncapped), sh)
