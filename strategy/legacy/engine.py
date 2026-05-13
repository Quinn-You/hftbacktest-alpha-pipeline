#!/usr/bin/env python3
"""Legacy 固定持仓：阈值开仓，持有至目标时刻或日末强平；与 ``strategy.backtest`` 共用撮合内核。"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..backtest import GTC
from ..backtest import LIMIT
from ..backtest import Trade
from ..backtest import build_hbt
from ..backtest import compute_order_shares
from ..backtest import get_aggressive_limit_price
from ..backtest import hold_target_ts_excluding_lunch
from ..backtest import is_order_filled
from ..backtest import submit_entry_order
from ..backtest import summarize_trades
from ..backtest import to_ns_utc
from ..backtest import _forced_flatten_price
from ..backtest import _is_tradable_for_side_l1_min1lot
from ..backtest import _load_last_trade_price_from_eventstream
from ..aum import capped_entry_shares
from ..aum import open_ledger_increments


def _legacy_long_short_exposure(open_positions: list[OpenPosition], closed_positions: set[int]) -> tuple[int, int]:
	long_sh = 0
	short_sh = 0
	for i, p in enumerate(open_positions):
		if i in closed_positions:
			continue
		if p.side > 0:
			long_sh += int(p.shares)
		else:
			short_sh += int(p.shares)
	return long_sh, short_sh


@dataclass
class OpenPosition:
	"""策略主循环中的持仓状态。

	它只在回测过程中存在，用于记录开仓后、平仓前的临时状态。
	"""

	side: int
	shares: int
	alpha_signal: float
	entry_order_id: int
	entry_ts_ns: int
	entry_px: float
	target_exit_ts_ns: int
	exit_order_id: int | None = None


def submit_exit_order(
	hbt: Any,
	order_id: int,
	pos: OpenPosition,
	price: float,
	close_shares: int | None = None,
) -> None:
	"""针对现有持仓提交对手方向平仓单（使用可控价格的限价单）。"""
	sh = int(pos.shares if close_shares is None else min(int(close_shares), int(pos.shares)))
	if sh <= 0:
		return
	if pos.side > 0:
		hbt.submit_sell_order(0, order_id, float(price), float(sh), GTC, LIMIT, False)
	else:
		hbt.submit_buy_order(0, order_id, float(price), float(sh), GTC, LIMIT, False)


def _finalize_trade_from_position(
	pos: OpenPosition,
	exit_ts_ns: int,
	exit_px: float,
	commission_rate: float,
	stamp_duty_rate: float,
	close_shares: int | None = None,
) -> Trade:
	"""用给定平仓价格把持仓结算为最终 Trade（``close_shares`` 为部分平仓股数，默认与 ``pos.shares`` 一致）。"""
	sh = int(close_shares) if close_shares is not None else int(pos.shares)
	entry_notional = float(pos.entry_px * sh)
	exit_notional = float(exit_px * sh)
	forward_return = (exit_px - pos.entry_px) / pos.entry_px if pos.entry_px > 0 else 0.0
	pnl_gross = pos.side * (exit_px - pos.entry_px) * sh
	commission = (entry_notional + exit_notional) * commission_rate
	sell_notional = exit_notional if pos.side > 0 else entry_notional
	stamp_duty = sell_notional * stamp_duty_rate
	total_cost = commission + stamp_duty
	pnl = pnl_gross - total_cost
	ret = float(forward_return)
	return Trade(
		side="long" if pos.side > 0 else "short",
		entry_ts_ns=pos.entry_ts_ns,
		exit_ts_ns=exit_ts_ns,
		entry_px=pos.entry_px,
		exit_px=float(exit_px),
		shares=sh,
		entry_notional=entry_notional,
		exit_notional=exit_notional,
		pnl_gross=float(pnl_gross),
		commission=float(commission),
		stamp_duty=float(stamp_duty),
		total_cost=float(total_cost),
		pnl=float(pnl),
		ret=float(ret),
		alpha_signal=pos.alpha_signal,
	)


def run_backtest_for_alpha_records(
	eventstream_path: Path,
	alpha_records: list[tuple[int, float]],
	trade_date: str,
	alpha_threshold: float,
	hold_minutes: int,
	notional: float,
	order_size_mode: str,
	order_lots: int,
	lot_size: int,
	aggressive_ticks: int,
	step_ns: int,
	tick_size: float,
	order_latency_ns: int,
	roi_lb: float,
	roi_ub: float,
	commission_rate: float,
	stamp_duty_rate: float,
	force_flatten_hhmmss: str = "14:45:00",
	force_flatten_extra_ticks: int = 5,
	aum: float | None = None,
	trade_unit_clip_frac: float | None = None,
) -> tuple[list[Trade], dict[str, float]]:
	"""执行单只股票的回测主循环。

	输入：
	- eventstream_path: 该股票该交易日的盘口事件流。
	- alpha_records: [(信号时间, alpha 值)] 序列，且已按时间排序。
	- 其余参数均为回测和成本模型参数。

	输出：
	- trades: 单笔交易明细。
	- summary: 从 trades 进一步汇总出的摘要统计。
	"""

	if GTC is None or LIMIT is None:
		raise RuntimeError("当前 hftbacktest 版本不支持 GTC/LIMIT 订单类型")
	if commission_rate < 0 or stamp_duty_rate < 0:
		raise ValueError("commission_rate 和 stamp_duty_rate 不能为负数")
	if order_size_mode not in {"notional", "lots"}:
		raise ValueError("order_size_mode 必须是 notional 或 lots")
	if order_lots < 1:
		raise ValueError("order_lots 必须 >= 1")
	if aggressive_ticks < 0:
		raise ValueError("aggressive_ticks 不能为负数")
	if force_flatten_extra_ticks < 0:
		raise ValueError("force_flatten_extra_ticks 不能为负数")
	try:
		force_flatten_ts_ns = to_ns_utc(trade_date, force_flatten_hhmmss)
	except ValueError as exc:
		raise ValueError("force_flatten_hhmmss 必须是 HH:MM:SS 格式，例如 14:45:00") from exc
	stuck_check_cutoff_ts_ns = to_ns_utc(trade_date, "14:45:00")
	if len(alpha_records) == 0:
		return [], summarize_trades([])

	hbt = build_hbt(
		eventstream_path=eventstream_path,
		lot_size=lot_size,
		tick_size=tick_size,
		order_latency_ns=order_latency_ns,
		roi_lb=roi_lb,
		roi_ub=roi_ub,
	)

	trades: list[Trade] = []
	open_positions: list[OpenPosition] = []
	pending_entry: dict[int, tuple[int, float, int]] = {}
	pending_exit: dict[int, int] = {}
	closed_positions: set[int] = set()
	blocked_entry_l1_count = 0
	blocked_timed_exit_l1_count = 0
	blocked_forced_exit_l1_count = 0
	fallback_stats_after_1445 = (0, 0, 0)
	fallback_shares_after_1445 = 0
	used_long = 0.0
	used_short = 0.0
	blocked_aum_count = 0
	next_signal_idx = 0
	next_order_id = 1
	last_trade_px = _load_last_trade_price_from_eventstream(eventstream_path)
	flatten_triggered = False

	try:
		while True:
			# 推进撮合引擎到下一个离散时间点，并读取当前盘口快照。
			status = hbt.elapse(step_ns)
			now_ts = int(hbt.current_timestamp)
			depth = hbt.depth(0)

			# 先检查之前挂出的开仓单是否已经成交；成交后把状态转成 open_positions。
			#这里pending_entry表明挂了的单子，但是不知道是否成交
			filled_entry_ids: list[int] = []
			for oid, (side, alpha_val, shares) in pending_entry.items():
				order = hbt.orders(0).get(oid)
				if not is_order_filled(order):
					continue
				#如果单子成交了，更新持仓状态，在OpenPosition中体现
				entry_ts_ns = int(order.exch_timestamp)
				entry_px = float(order.exec_price)
				filled_sh = int(float(order.exec_qty))
				bl, bs = _legacy_long_short_exposure(open_positions, closed_positions)
				al = bl + (filled_sh if side > 0 else 0)
				ash = bs + (filled_sh if side < 0 else 0)
				dl, ds = open_ledger_increments(bl, bs, al, ash, entry_px)
				used_long += dl
				used_short += ds
				open_positions.append(
					OpenPosition(
						side=side,
						shares=filled_sh,
						alpha_signal=alpha_val,
						entry_order_id=oid,
						entry_ts_ns=entry_ts_ns,
						entry_px=entry_px,
						target_exit_ts_ns=hold_target_ts_excluding_lunch(entry_ts_ns, hold_minutes),
					)
				)
				filled_entry_ids.append(oid)
			for oid in filled_entry_ids:
				pending_entry.pop(oid, None)

			# 再检查持仓是否到达目标持有时长；到时就发送激进限价平仓单（单侧盘口 + 对手盘一档量，不强求双边）。
			for idx, pos in enumerate(open_positions):
				if idx in closed_positions or pos.exit_order_id is not None or now_ts < pos.target_exit_ts_ns:
					continue
				oid = next_order_id
				next_order_id += 1
				#exit_side为平仓方向，-1为平多仓，1为平空仓。也就是-1要卖，1要买。
				exit_side = -1 if pos.side > 0 else 1
				#检查当前的深度是否可以平仓，若不可以则不提交平仓单，并计数。
				if not _is_tradable_for_side_l1_min1lot(depth, exit_side, lot_size):
					blocked_timed_exit_l1_count += 1
					continue
				exit_px = get_aggressive_limit_price(
					best_bid=float(depth.best_bid),
					best_ask=float(depth.best_ask),
					side=exit_side,
					tick_size=tick_size,
					aggressive_ticks=aggressive_ticks,
				)
				submit_exit_order(hbt, oid, pos, exit_px)
				pos.exit_order_id = oid
				pending_exit[oid] = idx

			# 平仓单成交后，生成最终 Trade，并在这里统一结算手续费、印花税和收益率。
			filled_exit_ids: list[int] = []
			for oid, pos_idx in pending_exit.items():
				order = hbt.orders(0).get(oid)
				if not is_order_filled(order):
					continue
				pos = open_positions[pos_idx]
				exit_px = float(order.exec_price)
				filled_sh = int(float(order.exec_qty))
				trades.append(
					_finalize_trade_from_position(
						pos=pos,
						exit_ts_ns=int(order.exch_timestamp),
						exit_px=exit_px,
						commission_rate=commission_rate,
						stamp_duty_rate=stamp_duty_rate,
						close_shares=filled_sh,
					)
				)
				pos.shares -= filled_sh
				if pos.shares <= 0:
					closed_positions.add(pos_idx)
				else:
					pos.exit_order_id = None
				filled_exit_ids.append(oid)
			for oid in filled_exit_ids:
				pending_exit.pop(oid, None)

			# 固定时点后进入“只平不开”：到达强平时间后，不再开新仓，并持续尝试平掉所有未平仓位。
			if now_ts >= force_flatten_ts_ns:
				if not flatten_triggered:
					flatten_triggered = True
				for idx, pos in enumerate(open_positions):
					if idx in closed_positions or pos.exit_order_id is not None:
						continue
					oid = next_order_id
					next_order_id += 1
					exit_side = -1 if pos.side > 0 else 1
					if not _is_tradable_for_side_l1_min1lot(depth, exit_side, lot_size):
						blocked_forced_exit_l1_count += 1
						continue
					#检查当前的深度是否可以平仓，若可以则submit激进限价单去完成平仓
					exit_px = get_aggressive_limit_price(
						best_bid=float(depth.best_bid),
						best_ask=float(depth.best_ask),
						side=exit_side,
						tick_size=tick_size,
						aggressive_ticks=aggressive_ticks,
					)
					submit_exit_order(hbt, oid, pos, exit_px)
					pos.exit_order_id = oid
					pending_exit[oid] = idx

			# 消费当前时点之前已经到达的 alpha 信号，并尝试根据阈值开仓。
			while (not flatten_triggered) and next_signal_idx < len(alpha_records) and alpha_records[next_signal_idx][0] <= now_ts:
				_, alpha_val = alpha_records[next_signal_idx]
				next_signal_idx += 1
				side = 0
				if alpha_val >= alpha_threshold:
					side = 1
				elif alpha_val <= -alpha_threshold:
					side = -1
				if side == 0:
					continue
				if not _is_tradable_for_side_l1_min1lot(depth, side, lot_size):
					blocked_entry_l1_count += 1
					continue

				entry_ref_px = float(depth.best_ask) if side > 0 else float(depth.best_bid)
				shares_uncapped = compute_order_shares(
					entry_ref_px=entry_ref_px,
					notional=notional,
					lot_size=lot_size,
					order_size_mode=order_size_mode,
					order_lots=order_lots,
				)
				used_open = used_long if side > 0 else used_short
				shares = capped_entry_shares(
					entry_ref_px,
					shares_uncapped,
					lot_size,
					aum,
					used_open,
					trade_unit_clip_frac,
				)
				if shares < lot_size:
					if aum is not None and shares_uncapped >= lot_size:
						blocked_aum_count += 1
					continue

				entry_px = get_aggressive_limit_price(
					best_bid=float(depth.best_bid),
					best_ask=float(depth.best_ask),
					side=side,
					tick_size=tick_size,
					aggressive_ticks=aggressive_ticks,
				)

				oid = next_order_id
				next_order_id += 1
				submit_entry_order(hbt, oid, side, shares, entry_px)
				pending_entry[oid] = (side, alpha_val, shares)

			# 回放结束后，只要后续没有未消费信号、未成交开仓单和未完成平仓流程，就退出。
			if status != 0:
				# 日末兜底：确保未完成持仓全部平仓，避免隔夜仓位。
				n_fallback_flatten_after_1445 = 0
				n_fallback_long_after_1445 = 0
				n_fallback_short_after_1445 = 0
				shares_unflattenable_after_1445 = 0
				for pos_idx, pos in enumerate(open_positions):
					if pos_idx in closed_positions:
						continue
					exit_side = -1 if pos.side > 0 else 1
					# “平不掉”定义：14:45 后进入兜底时，该方向仍不满足对手盘一档可交易约束。
					if (now_ts >= stuck_check_cutoff_ts_ns) and (
						not _is_tradable_for_side_l1_min1lot(depth, exit_side, lot_size)
					):
						n_fallback_flatten_after_1445 += 1
						shares_unflattenable_after_1445 += int(pos.shares)
						if pos.side > 0:
							n_fallback_long_after_1445 += 1
						else:
							n_fallback_short_after_1445 += 1
					fallback_px = _forced_flatten_price(
						last_trade_px=last_trade_px,
						entry_px=pos.entry_px,
						tick_size=tick_size,
						force_flatten_extra_ticks=force_flatten_extra_ticks,
					)
					fpx = float(fallback_px)
					sh_close = int(pos.shares)
					if sh_close > 0:
						trades.append(
							_finalize_trade_from_position(
								pos=pos,
								exit_ts_ns=now_ts,
								exit_px=fpx,
								commission_rate=commission_rate,
								stamp_duty_rate=stamp_duty_rate,
								close_shares=sh_close,
							)
						)
						pos.shares = 0
						closed_positions.add(pos_idx)

				# 回放结束时，未成交开仓单直接忽略；仓位已在上面兜底平仓。
				pending_entry.clear()
				pending_exit.clear()
				fallback_stats_after_1445 = (
					n_fallback_flatten_after_1445,
					n_fallback_long_after_1445,
					n_fallback_short_after_1445,
				)
				fallback_shares_after_1445 = shares_unflattenable_after_1445
				all_exited = len(closed_positions) == len(open_positions)
				if next_signal_idx >= len(alpha_records) and all_exited:
					break
				break

			hbt.clear_inactive_orders(0)
	finally:
		hbt.close()

	summary = summarize_trades(trades)
	summary["blocked_entry_l1_count"] = int(blocked_entry_l1_count)
	summary["blocked_timed_exit_l1_count"] = int(blocked_timed_exit_l1_count)
	summary["blocked_forced_exit_l1_count"] = int(blocked_forced_exit_l1_count)
	# fallback_*_after_1445：14:45 后兜底时仍不满足对手盘一档可交易约束的平不掉仓位计数。
	summary["n_fallback_flatten_after_1445"] = int(fallback_stats_after_1445[0])
	summary["n_fallback_long_after_1445"] = int(fallback_stats_after_1445[1])
	summary["n_fallback_short_after_1445"] = int(fallback_stats_after_1445[2])
	summary["n_unflattenable_after_1445"] = int(fallback_stats_after_1445[0])
	summary["n_unflattenable_long_after_1445"] = int(fallback_stats_after_1445[1])
	summary["n_unflattenable_short_after_1445"] = int(fallback_stats_after_1445[2])
	summary["has_unflattenable_after_1445"] = int(fallback_stats_after_1445[0] > 0)
	summary["shares_unflattenable_after_1445"] = int(fallback_shares_after_1445)
	summary["used_long"] = float(used_long)
	summary["used_short"] = float(used_short)
	summary["blocked_aum_count"] = int(blocked_aum_count)
	return trades, summary
