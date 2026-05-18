#!/usr/bin/env python3
"""Grid trading: 以目标底仓为中心做双边网格，尾盘切换强平收敛。"""

from __future__ import annotations

from dataclasses import asdict
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np

from ..aum import open_ledger_increments
from ..backtest import EXIT_ORDER_STALE_NS
from ..backtest import GTC
from ..backtest import LIMIT
from ..backtest import build_hbt
from ..backtest import to_ns_utc
from ..backtest import _is_tradable_for_side_l1_min1lot

DAY_NS = 24 * 60 * 60 * 1_000_000_000
MINUTE_NS = 60 * 1_000_000_000

MORNING_GRID_START_NS = (9 * 60 + 45) * MINUTE_NS
MORNING_END_NS = (11 * 60 + 30) * MINUTE_NS
AFTERNOON_START_NS = 13 * 60 * MINUTE_NS


@dataclass
class GridPoint:
	"""策略曲线采样点。"""

	timestamp: int
	position: int
	balance: float
	fee: float
	num_trades: int
	best_bid: float
	best_ask: float
	mid_price: float
	mark_price: float
	equity_wo_fee: float
	equity: float
	alpha: float
	desired_inventory: int


def _clock_ns(ts_ns: int) -> int:
	return int(ts_ns % DAY_NS)


def _in_window(clock_ns: int, start_ns: int, end_ns: int) -> bool:
	return bool(start_ns <= clock_ns < end_ns)


def _split_exposure(position: int) -> tuple[int, int]:
	pos = int(position)
	if pos >= 0:
		return pos, 0
	return 0, -pos


def _round_lot(shares: int, lot_size: int) -> int:
	return (int(shares) // int(lot_size)) * int(lot_size)


def _tick_align_down(price: float, tick_size: float) -> float:
	return float(np.floor(price / tick_size) * tick_size)


def _tick_align_up(price: float, tick_size: float) -> float:
	return float(np.ceil(price / tick_size) * tick_size)


def _iter_orders(orders: Any) -> list[tuple[int, Any]]:
	"""兼容 hftbacktest OrderDict / dict，返回 (order_id, order) 列表。"""
	try:
		return [(int(oid), order) for oid, order in orders.items()]
	except Exception:
		pass

	values_attr = getattr(orders, "values", None)
	if callable(values_attr):
		vals = values_attr()
		if hasattr(vals, "has_next"):
			out: list[tuple[int, Any]] = []
			while vals.has_next():
				order = vals.get()
				oid = int(getattr(order, "order_id", -1))
				if oid >= 0:
					out.append((oid, order))
			return out
		try:
			return [
				(int(getattr(order, "order_id", -1)), order)
				for order in vals
				if int(getattr(order, "order_id", -1)) >= 0
			]
		except Exception:
			pass

	keys_attr = getattr(orders, "keys", None)
	if callable(keys_attr):
		out: list[tuple[int, Any]] = []
		try:
			for oid in keys_attr():
				order = _get_order(orders, int(oid))
				if order is not None:
					out.append((int(oid), order))
		except Exception:
			return out
		return out
	return []


def _get_order(orders: Any, order_id: int) -> Any | None:
	getter = getattr(orders, "get", None)
	if callable(getter):
		try:
			return getter(int(order_id))
		except Exception:
			return None
	return None


def _has_order(orders: Any, order_id: int) -> bool:
	if _get_order(orders, int(order_id)) is not None:
		return True
	try:
		return bool(int(order_id) in orders)
	except Exception:
		return False


def _calc_equity_snapshot(
	hbt: Any,
	best_bid: float,
	best_ask: float,
	last_trade_px: float | None,
	current_alpha: float,
	desired_inventory: int,
	synthetic_balance: float,
	synthetic_fee: float,
	synthetic_num_trades: int,
) -> GridPoint:
	balance_fn = getattr(hbt, "balance", None)
	if callable(balance_fn):
		try:
			balance = float(balance_fn(0))
		except Exception:
			balance = float(synthetic_balance)
	else:
		balance = float(synthetic_balance)
	position = int(hbt.position(0))
	fee_fn = getattr(hbt, "fee", None)
	if callable(fee_fn):
		try:
			fee = float(fee_fn(0))
		except Exception:
			fee = float(synthetic_fee)
	else:
		fee = float(synthetic_fee)
	num_trades_fn = getattr(hbt, "num_trades", None)
	if callable(num_trades_fn):
		try:
			num_trades = int(num_trades_fn(0))
		except Exception:
			num_trades = int(synthetic_num_trades)
	else:
		num_trades = int(synthetic_num_trades)
	mid_price = float(0.5 * (best_bid + best_ask))
	if np.isfinite(mid_price) and mid_price > 0:
		mark = mid_price
	elif last_trade_px is not None and np.isfinite(last_trade_px) and last_trade_px > 0:
		mark = float(last_trade_px)
	else:
		mark = max(float(best_bid), float(best_ask), 0.0)
	equity_wo_fee = balance + float(position) * mark
	equity = equity_wo_fee - fee
	return GridPoint(
		timestamp=int(hbt.current_timestamp),
		position=position,
		balance=balance,
		fee=fee,
		num_trades=num_trades,
		best_bid=float(best_bid),
		best_ask=float(best_ask),
		mid_price=mid_price,
		mark_price=float(mark),
		equity_wo_fee=float(equity_wo_fee),
		equity=float(equity),
		alpha=float(current_alpha),
		desired_inventory=int(desired_inventory),
	)


def _max_buy_by_aum(
	position: int,
	price: float,
	lot_size: int,
	aum: float | None,
	used_long: float,
	trade_unit_clip_frac: float | None,
) -> int:
	if aum is None:
		return 10**18
	remaining = max(0.0, float(aum) - float(used_long))
	if trade_unit_clip_frac is not None and trade_unit_clip_frac > 0:
		remaining = min(remaining, max(0.0, float(aum) - float(used_long)) * float(trade_unit_clip_frac))
	if remaining <= 0 or price <= 0:
		return 0
	# 先求最多可新增的多头股数，再映射回买单股数（可能含回补空头部分）。
	max_additional_long = _round_lot(int(remaining // price), lot_size)
	if position >= 0:
		return max_additional_long
	return _round_lot(max_additional_long + abs(int(position)), lot_size)


def _max_sell_by_aum(
	position: int,
	price: float,
	lot_size: int,
	aum: float | None,
	used_short: float,
	trade_unit_clip_frac: float | None,
) -> int:
	if aum is None:
		return 10**18
	remaining = max(0.0, float(aum) - float(used_short))
	if trade_unit_clip_frac is not None and trade_unit_clip_frac > 0:
		remaining = min(remaining, max(0.0, float(aum) - float(used_short)) * float(trade_unit_clip_frac))
	if remaining <= 0 or price <= 0:
		return 0
	max_additional_short = _round_lot(int(remaining // price), lot_size)
	if position <= 0:
		return max_additional_short
	return _round_lot(max_additional_short + int(position), lot_size)


def run_gridtrading_backtest(
	eventstream_path: Path,
	alpha_records: list[tuple[int, float]],
	trade_date: str,
	desired_inventory_shares: int | None = None,
	target_notional: float | None = None,
	grid_num: int = 8,
	order_lots: int = 2,
	inventory_span_lots: int = 40,
	half_spread_ticks: int = 0,
	grid_step_ticks: int = 1,
	skew1: float = 1.0,
	skew2: float = 0.0,
	step_ns: int = 100_000_000,
	recorder_interval_ns: int = 100_000_000,
	order_latency_ns: int = 50_000_000,
	cutoff_buffer_ns: int | None = None,
	lot_size: int = 100,
	tick_size: float = 0.01,
	roi_lb: float = 0.0,
	roi_ub: float = 200.0,
	force_flatten_hhmmss: str = "14:45:00",
	force_flatten_end_hhmmss: str = "14:57:00",
	force_flatten_extra_ticks: int = 20,
	aum: float | None = None,
	trade_unit_clip_frac: float | None = None,
	alpha_default_to_zero: bool = True,
) -> tuple[list[GridPoint], dict[str, float]]:
	"""运行单标的网格回测并返回曲线采样与摘要。"""
	if GTC is None or LIMIT is None:
		raise RuntimeError("当前环境未安装 hftbacktest 或缺少 GTC/LIMIT")
	if grid_num < 1:
		raise ValueError("grid_num 必须 >= 1")
	if order_lots < 1:
		raise ValueError("order_lots 必须 >= 1")
	if inventory_span_lots < 0:
		raise ValueError("inventory_span_lots 不能为负数")
	if grid_step_ticks < 1:
		raise ValueError("grid_step_ticks 必须 >= 1")
	if force_flatten_extra_ticks < 0:
		raise ValueError("force_flatten_extra_ticks 不能为负数")
	if desired_inventory_shares is None and (target_notional is None or target_notional <= 0):
		raise ValueError("desired_inventory_shares 与 target_notional 至少提供一个正值")
	if cutoff_buffer_ns is None:
		cutoff_buffer_ns = 2 * int(order_latency_ns)
	force_flatten_ts_ns = to_ns_utc(trade_date, force_flatten_hhmmss)
	force_flatten_end_ts_ns = to_ns_utc(trade_date, force_flatten_end_hhmmss)
	if force_flatten_end_ts_ns <= force_flatten_ts_ns:
		raise ValueError("force_flatten_end_hhmmss 必须晚于 force_flatten_hhmmss")

	hbt = build_hbt(
		eventstream_path=eventstream_path,
		lot_size=lot_size,
		tick_size=tick_size,
		order_latency_ns=order_latency_ns,
		roi_lb=roi_lb,
		roi_ub=roi_ub,
	)

	alpha_idx = 0
	has_alpha = False
	current_alpha = 0.0
	desired_inventory = None if desired_inventory_shares is None else _round_lot(desired_inventory_shares, lot_size)
	inventory_span = int(inventory_span_lots) * int(lot_size)
	order_qty = int(order_lots) * int(lot_size)

	points: list[GridPoint] = []
	next_record_ts: int | None = None
	last_trade_px: float | None = None
	used_long = 0.0
	used_short = 0.0
	shadow_position = 0
	exec_qty_seen: dict[int, int] = {}
	synthetic_balance = 0.0
	synthetic_fee = 0.0
	synthetic_num_trades = 0
	closeout_order_id: int | None = None
	closeout_order_submit_ts: int | None = None
	closeout_retries = 0
	blocked_entry_l1_count = 0
	blocked_forced_exit_l1_count = 0
	blocked_aum_count = 0
	shares_unflattenable_after_1457 = 0
	fallback_flatten_shares = 0
	final_position_before_fallback = 0
	final_position_after_fallback = 0

	try:
		while True:
			status = hbt.elapse(step_ns)
			now_ts = int(hbt.current_timestamp)
			clock_ns = _clock_ns(now_ts)
			depth = hbt.depth(0)
			orders = hbt.orders(0)

			best_bid = float(depth.best_bid)
			best_ask = float(depth.best_ask)
			if not (np.isfinite(best_bid) and np.isfinite(best_ask) and best_bid > 0 and best_ask > 0):
				if status != 0:
					final_position_before_fallback = int(hbt.position(0))
					final_position_after_fallback = int(final_position_before_fallback)
					break
				continue
			mid_price = 0.5 * (best_bid + best_ask)
			last_trade_px = mid_price

			if desired_inventory is None:
				assert target_notional is not None
				raw_shares = int(float(target_notional) // mid_price)
				desired_inventory = _round_lot(raw_shares, lot_size)

			while alpha_idx < len(alpha_records) and alpha_records[alpha_idx][0] <= now_ts:
				current_alpha = float(alpha_records[alpha_idx][1])
				has_alpha = True
				alpha_idx += 1
			alpha_term = current_alpha if (has_alpha or alpha_default_to_zero) else 0.0

			for oid, order in _iter_orders(orders):
				exec_qty = int(float(getattr(order, "exec_qty", 0.0)))
				seen_qty = int(exec_qty_seen.get(int(oid), 0))
				if exec_qty <= seen_qty:
					continue
				delta_qty = exec_qty - seen_qty
				side = int(getattr(order, "side", 0))
				if side not in {1, -1}:
					exec_qty_seen[int(oid)] = exec_qty
					continue
				exec_price = float(getattr(order, "exec_price", mid_price))
				# 兼容不提供 balance/fee/num_trades API 的 hbt 版本：按成交现金流维护账户快照。
				if side > 0:
					synthetic_balance -= float(exec_price) * float(delta_qty)
				else:
					synthetic_balance += float(exec_price) * float(delta_qty)
				synthetic_num_trades += 1
				before_long, before_short = _split_exposure(shadow_position)
				shadow_position = int(shadow_position + side * delta_qty)
				after_long, after_short = _split_exposure(shadow_position)
				dl, ds = open_ledger_increments(before_long, before_short, after_long, after_short, exec_price)
				used_long += float(dl)
				used_short += float(ds)
				exec_qty_seen[int(oid)] = exec_qty
			# 先统计本步成交，再清理 inactive，避免漏记已成交后失活订单。
			hbt.clear_inactive_orders(0)
			orders = hbt.orders(0)

			if next_record_ts is None:
				next_record_ts = now_ts
			if now_ts >= next_record_ts:
				points.append(
					_calc_equity_snapshot(
						hbt=hbt,
						best_bid=best_bid,
						best_ask=best_ask,
						last_trade_px=last_trade_px,
						current_alpha=float(alpha_term),
						desired_inventory=int(desired_inventory),
						synthetic_balance=float(synthetic_balance),
						synthetic_fee=float(synthetic_fee),
						synthetic_num_trades=int(synthetic_num_trades),
					)
				)
				next_record_ts = now_ts + int(recorder_interval_ns)

			morning_cutoff = MORNING_END_NS - int(cutoff_buffer_ns)
			flatten_start_clock = _clock_ns(force_flatten_ts_ns)
			flatten_end_clock = _clock_ns(force_flatten_end_ts_ns)
			afternoon_grid_end = flatten_start_clock - int(cutoff_buffer_ns)
			in_morning = _in_window(clock_ns, MORNING_GRID_START_NS, morning_cutoff)
			in_afternoon = _in_window(clock_ns, AFTERNOON_START_NS, afternoon_grid_end)
			in_grid_window = in_morning or in_afternoon
			in_closeout_window = _in_window(clock_ns, flatten_start_clock, flatten_end_clock)

			if in_closeout_window:
				for oid, order in _iter_orders(orders):
					if closeout_order_id is not None and int(oid) == int(closeout_order_id):
						continue
					if bool(getattr(order, "cancellable", False)):
						hbt.cancel(0, int(oid), False)

				if closeout_order_id is not None:
					active = _get_order(orders, int(closeout_order_id))
					if active is None or not bool(getattr(active, "cancellable", False)):
						closeout_order_id = None
						closeout_order_submit_ts = None
					elif closeout_order_submit_ts is not None and now_ts - closeout_order_submit_ts >= EXIT_ORDER_STALE_NS:
						hbt.cancel(0, int(closeout_order_id), False)
						closeout_order_id = None
						closeout_order_submit_ts = None
						closeout_retries += 1

				position = int(hbt.position(0))
				gap = int(desired_inventory - position)
				if closeout_order_id is None and abs(gap) >= lot_size:
					side = 1 if gap > 0 else -1
					if _is_tradable_for_side_l1_min1lot(depth, side, lot_size):
						qty = _round_lot(abs(gap), lot_size)
						if qty >= lot_size:
							if side > 0:
								px = best_ask + float(force_flatten_extra_ticks) * float(tick_size)
								oid = int(9_000_000_001)
								hbt.submit_buy_order(0, oid, float(px), float(qty), GTC, LIMIT, False)
							else:
								px = max(float(tick_size), best_bid - float(force_flatten_extra_ticks) * float(tick_size))
								oid = int(9_000_000_002)
								hbt.submit_sell_order(0, oid, float(px), float(qty), GTC, LIMIT, False)
							closeout_order_id = int(oid)
							closeout_order_submit_ts = now_ts
					else:
						blocked_forced_exit_l1_count += 1
			else:
				closeout_order_id = None
				closeout_order_submit_ts = None

			if in_grid_window:
				position = int(hbt.position(0))
				deviation_in_lots = (float(position) - float(desired_inventory)) / float(max(lot_size, 1))
				reservation_price = mid_price - float(skew1) * float(tick_size) * deviation_in_lots + float(skew2) * float(alpha_term)
				half_spread = max(int(half_spread_ticks), 0) * float(tick_size)
				grid_step = max(float(grid_step_ticks) * float(tick_size), float(tick_size))

				bid_anchor = _tick_align_down(min(reservation_price - half_spread, best_bid), grid_step)
				ask_anchor = _tick_align_up(max(reservation_price + half_spread, best_ask), grid_step)

				upper_position = int(desired_inventory + inventory_span)
				lower_position = int(desired_inventory - inventory_span)

				outstanding_buy_qty = 0
				outstanding_sell_qty = 0
				for _, order in _iter_orders(orders):
					if not bool(getattr(order, "cancellable", False)):
						continue
					side = int(getattr(order, "side", 0))
					leaves = int(float(getattr(order, "leaves_qty", 0.0)))
					if side > 0:
						outstanding_buy_qty += max(leaves, 0)
					elif side < 0:
						outstanding_sell_qty += max(leaves, 0)

				max_buy_by_band = max(0, upper_position - position - outstanding_buy_qty)
				max_sell_by_band = max(0, position - lower_position - outstanding_sell_qty)
				max_buy_by_aum = _max_buy_by_aum(
					position=position,
					price=best_ask,
					lot_size=lot_size,
					aum=aum,
					used_long=used_long,
					trade_unit_clip_frac=trade_unit_clip_frac,
				)
				max_sell_by_aum = _max_sell_by_aum(
					position=position,
					price=best_bid,
					lot_size=lot_size,
					aum=aum,
					used_short=used_short,
					trade_unit_clip_frac=trade_unit_clip_frac,
				)
				max_buy_qty = _round_lot(min(max_buy_by_band, max_buy_by_aum), lot_size)
				max_sell_qty = _round_lot(min(max_sell_by_band, max_sell_by_aum), lot_size)
				if aum is not None:
					if max_buy_by_band >= lot_size and max_buy_qty < lot_size:
						blocked_aum_count += 1
					if max_sell_by_band >= lot_size and max_sell_qty < lot_size:
						blocked_aum_count += 1

				target_buy_levels: dict[int, float] = {}
				target_sell_levels: dict[int, float] = {}
				if max_buy_qty >= lot_size:
					px = float(bid_anchor)
					for _ in range(int(grid_num)):
						tick = int(round(px / tick_size))
						target_buy_levels[tick] = px
						px -= grid_step
				if max_sell_qty >= lot_size:
					px = float(ask_anchor)
					for _ in range(int(grid_num)):
						tick = int(round(px / tick_size))
						target_sell_levels[tick] = px
						px += grid_step

				for oid, order in _iter_orders(orders):
					if not bool(getattr(order, "cancellable", False)):
						continue
					side = int(getattr(order, "side", 0))
					keep = False
					if side > 0 and int(oid) in target_buy_levels:
						keep = True
					elif side < 0 and int(oid) in target_sell_levels:
						keep = True
					if not keep:
						hbt.cancel(0, int(oid), False)

				for oid, px in target_buy_levels.items():
					if _has_order(orders, oid):
						continue
					if max_buy_qty < lot_size:
						break
					if not _is_tradable_for_side_l1_min1lot(depth, 1, lot_size):
						blocked_entry_l1_count += 1
						break
					qty = min(order_qty, max_buy_qty)
					qty = _round_lot(qty, lot_size)
					if qty < lot_size:
						break
					hbt.submit_buy_order(0, int(oid), float(px), float(qty), GTC, LIMIT, False)
					max_buy_qty -= qty

				for oid, px in target_sell_levels.items():
					if _has_order(orders, oid):
						continue
					if max_sell_qty < lot_size:
						break
					if not _is_tradable_for_side_l1_min1lot(depth, -1, lot_size):
						blocked_entry_l1_count += 1
						break
					qty = min(order_qty, max_sell_qty)
					qty = _round_lot(qty, lot_size)
					if qty < lot_size:
						break
					hbt.submit_sell_order(0, int(oid), float(px), float(qty), GTC, LIMIT, False)
					max_sell_qty -= qty
			else:
				if not in_closeout_window:
					for oid, order in _iter_orders(orders):
						if bool(getattr(order, "cancellable", False)):
							hbt.cancel(0, int(oid), False)

			if status != 0:
				final_position_before_fallback = int(hbt.position(0))
				final_position_after_fallback = int(final_position_before_fallback)
				gap_before = int(desired_inventory - final_position_before_fallback)
				if abs(gap_before) >= lot_size:
					fallback_flatten_shares = int(abs(gap_before))
					final_position_after_fallback = int(desired_inventory)
					# 回放结束兜底：若该方向在 14:57 后仍不可交易，记录未能盘中平完股数。
					side = 1 if gap_before > 0 else -1
					if not _is_tradable_for_side_l1_min1lot(depth, side, lot_size):
						shares_unflattenable_after_1457 = int(abs(gap_before))
				break
	finally:
		hbt.close()

	final_gap_before_fallback = abs(int(final_position_before_fallback) - int(desired_inventory or 0))
	final_gap_after_fallback = abs(int(final_position_after_fallback) - int(desired_inventory or 0))
	if final_gap_before_fallback >= lot_size and fallback_flatten_shares == 0:
		fallback_flatten_shares = int(final_gap_before_fallback)

	summary = {
		"n_samples": int(len(points)),
		"final_position_before_fallback": int(final_position_before_fallback),
		"final_position_after_fallback": int(final_position_after_fallback),
		"final_position": int(final_position_after_fallback),
		"desired_inventory": int(desired_inventory or 0),
		"final_position_gap_before_fallback": int(final_gap_before_fallback),
		"final_position_gap_after_fallback": int(final_gap_after_fallback),
		"final_position_gap": int(final_gap_after_fallback),
		"fallback_flatten_shares": int(fallback_flatten_shares),
		"shares_unflattenable_after_1457": int(shares_unflattenable_after_1457),
		"used_long": float(used_long),
		"used_short": float(used_short),
		"blocked_entry_l1_count": int(blocked_entry_l1_count),
		"blocked_forced_exit_l1_count": int(blocked_forced_exit_l1_count),
		"blocked_aum_count": int(blocked_aum_count),
		"closeout_retries": int(closeout_retries),
		"final_equity": float(points[-1].equity) if points else 0.0,
	}
	return points, summary


def grid_points_to_dicts(points: list[GridPoint]) -> list[dict[str, Any]]:
	"""序列化辅助：dataclass 列表转字典列表。"""
	return [asdict(item) for item in points]
