"""TWAP 风格拆单：按固定名义切片（与 ``step_ns`` 对齐，默认每 5s 一次 elapse 挂一片）。

仅当 ``order_size_mode == "notional"`` 且 ``twap_slice_notional > 0`` 时启用；``lots`` 模式不拆单。
"""

from __future__ import annotations

from .backtest import compute_order_shares


def twap_by_notional_enabled(twap_slice_notional: float | None, order_size_mode: str) -> bool:
	'''这里返回一个bool值，判断需不需要用twap'''
	return (
		twap_slice_notional is not None
		and float(twap_slice_notional) > 0.0
		and order_size_mode == "notional"
	)


def entry_slice_notional_cap(
	*,
	twap_slice_notional: float,
	remaining_shares: int,
	entry_ref_px: float,
) -> float:
	"""本步名义上限：``min(切片名义, 剩余股×参考价)``。"""
	rem = max(0.0, float(remaining_shares) * float(entry_ref_px))
	return min(float(twap_slice_notional), rem)


def shares_for_notional_slice(
	*,
	entry_ref_px: float,
	slice_notional: float,
	lot_size: int,
	order_lots: int,
) -> int:
	"""按名义算股数并向下取整到手。"""
	u = compute_order_shares(
		entry_ref_px=float(entry_ref_px),
		notional=float(slice_notional),
		lot_size=int(lot_size),
		order_size_mode="notional",
		order_lots=int(order_lots),
	)
	sh = int(u)
	if sh < int(lot_size):
		return 0
	return (sh // int(lot_size)) * int(lot_size)
