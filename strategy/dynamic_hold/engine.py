#!/usr/bin/env python3
"""Dynamic hold：阈值开仓，持仓期内按 alpha 相对 α₀ 阶梯调仓（须先过方向阈值；按步取整 ``k×adjust_notional``、每点仅用当前 delta），止盈/止损/时间/信号反向（达阈值）任一平仓。可选 **TWAP**：``twap_slice_notional``>0 且 ``order_size_mode=notional`` 时，每帧至多一片开仓；**全平类**里时间/TP/SL 等仍可按片平仓，**信号反向**与 **TWAP 期间阶梯减仓超量** 为一次性满仓 aggressive 限价（见仓库根 ``rules.md`` §3.2）。

说明：存在未完成平仓挂单（含部分减仓）时，本期暂不叠加新的 alpha 平仓/阶梯；**超时撤单**与**强平撤非强平挂单**可避免单笔限价长期不成交导致后续全平/强平永远无法再挂单。强平在途单由 `forced_flatten_order_id` 标记，强平分支不撤该 oid。
信号反向与开仓对称：多仓须 ``alpha <= -alpha_threshold``、空仓须 ``alpha >= alpha_threshold`` 才全平。

与 ``strategy.legacy.run_backtest_for_alpha_records`` 对齐的 summary 字段含：
``shares_unflattenable_after_1445``、启用 AUM 时的 ``used_long`` / ``used_short`` / ``blocked_aum_count`` 等。
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any
from typing import Literal

import math

import numpy as np

from ..backtest import GTC
from ..backtest import LIMIT
from ..backtest import Trade
from ..backtest import build_hbt
from ..backtest import cancel_exit_order_if_active
from ..backtest import compute_order_shares
from ..backtest import EXIT_ORDER_STALE_NS
from ..backtest import get_aggressive_limit_price
from ..backtest import is_order_filled
from ..backtest import submit_entry_order
from ..backtest import summarize_trades
from ..backtest import to_ns_utc
from ..backtest import _forced_flatten_price
from ..backtest import _is_tradable_for_side_l1_min1lot
from ..backtest import _load_last_trade_price_from_eventstream
from ..backtest import hold_target_ts_excluding_lunch
from ..aum import capped_entry_shares
from ..aum import open_ledger_increments
from ..twap import entry_slice_notional_cap
from ..twap import shares_for_notional_slice
from ..twap import twap_by_notional_enabled

# 全平不按 TWAP 出仓切片、一次性 aggressive 限价（rules.md §3.2）
_FULL_EXIT_ONESHOT_REASONS = frozenset({"alpha_flip", "ladder_over_twap"})
# 开仓挂单来源：tw=TWAP 片；ld=阶梯加；op=非 TWAP 单笔
EntrySrc = Literal["tw", "ld", "op"]


def _dynamic_long_short_exposure(cur: DynamicPosition | None) -> tuple[int, int]:
	"""将当前动态持仓拆成 (多头股数, 空头股数)，供 AUM/敞口统计；无仓为 (0, 0)，多 side>0 记多头，否则记空头。"""
	if cur is None:
		return 0, 0
	if cur.side > 0:
		return int(cur.shares), 0
	return 0, int(cur.shares)


@dataclass
class DynamicPosition:
	"""单标的至多一笔合并持仓（加仓缩仓均更新均价与股数）。"""

	side: int
	shares: int
	avg_entry_px: float
	alpha_0: float
	first_entry_ts_ns: int
	target_exit_ts_ns: int


def _submit_close_shares(hbt: Any, order_id: int, position_side: int, shares: int, price: float) -> None:
	"""平掉 position_side 方向的持仓：多仓卖出、空仓买回。"""
	if shares <= 0:
		return
	if position_side > 0:
		hbt.submit_sell_order(0, order_id, float(price), int(shares), GTC, LIMIT, False)
	else:
		hbt.submit_buy_order(0, order_id, float(price), int(shares), GTC, LIMIT, False)


def _finalize_trade_chunk(
	side_str: str,
	avg_entry_px: float,
	exit_px: float,
	shares: int,
	entry_ts_ns: int,
	exit_ts_ns: int,
	alpha_signal: float,
	commission_rate: float,
	stamp_duty_rate: float,
) -> Trade:
	"""按均价结算一段已实现盈亏（多头/空头公式与 backtest 一致）。"""
	entry_notional = float(avg_entry_px * shares)
	exit_notional = float(exit_px * shares)
	if side_str == "long":
		pnl_gross = float((exit_px - avg_entry_px) * shares)
		forward_return = (exit_px - avg_entry_px) / avg_entry_px if avg_entry_px > 0 else 0.0
	else:
		pnl_gross = float((avg_entry_px - exit_px) * shares)
		forward_return = (avg_entry_px - exit_px) / avg_entry_px if avg_entry_px > 0 else 0.0
	commission = (entry_notional + exit_notional) * commission_rate
	sell_notional = exit_notional if side_str == "long" else entry_notional
	stamp_duty = sell_notional * stamp_duty_rate
	total_cost = commission + stamp_duty
	pnl = float(pnl_gross - total_cost)
	return Trade(
		side=side_str,
		entry_ts_ns=entry_ts_ns,
		exit_ts_ns=exit_ts_ns,
		entry_px=float(avg_entry_px),
		exit_px=float(exit_px),
		shares=int(shares),
		entry_notional=entry_notional,
		exit_notional=exit_notional,
		pnl_gross=float(pnl_gross),
		commission=float(commission),
		stamp_duty=float(stamp_duty),
		total_cost=float(total_cost),
		pnl=float(pnl),
		ret=float(forward_return),
		alpha_signal=float(alpha_signal),
	)


def _mid_mark(depth: Any, last_trade_px: float | None, tick_size: float) -> float:
	"""盘口无效时退回最近成交价；盘口有效时，中间价作为盯市盈亏，并返回中间价。"""
	bid = float(depth.best_bid)
	ask = float(depth.best_ask)
	if np.isfinite(bid) and np.isfinite(ask) and bid > 0 and ask > 0:
		return (bid + ask) / 2.0
	if last_trade_px is not None and np.isfinite(last_trade_px) and last_trade_px > 0:
		return float(last_trade_px)
	return float(tick_size)


def _tp_sl_hit_long(avg_px: float, mark: float, take_profit_pct: float | None, stop_loss_pct: float | None) -> str | None:
	"""多头：返回 'tp' / 'sl' / None。用来判断是否应该出于take profit or stop loss的目的平仓"""
	if take_profit_pct is not None and take_profit_pct > 0 and mark >= avg_px * (1.0 + float(take_profit_pct)):
		return "tp"
	if stop_loss_pct is not None and stop_loss_pct > 0 and mark <= avg_px * (1.0 - float(stop_loss_pct)):
		return "sl"
	return None


def _tp_sl_hit_short(avg_px: float, mark: float, take_profit_pct: float | None, stop_loss_pct: float | None) -> str | None:
	"""空头：价格下跌盈利、上涨亏损。用来判断是否应该出于take profit or stop loss的目的平仓"""
	if take_profit_pct is not None and take_profit_pct > 0 and mark <= avg_px * (1.0 - float(take_profit_pct)):
		return "tp"
	if stop_loss_pct is not None and stop_loss_pct > 0 and mark >= avg_px * (1.0 + float(stop_loss_pct)):
		return "sl"
	return None


def run_dynamic_hold_backtest(
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
	alpha_adjust_step: float = 0.01,
	adjust_notional: float = 10_000.0,
	take_profit_pct: float | None = None,
	stop_loss_pct: float | None = None,
	aum: float | None = None,
	trade_unit_clip_frac: float | None = None,
	twap_slice_notional: float | None = None,
) -> tuple[list[Trade], dict[str, float]]:
	if GTC is None or LIMIT is None:
		raise RuntimeError("当前环境未安装 hftbacktest 或缺少 GTC/LIMIT")
	if alpha_adjust_step <= 0:
		raise ValueError("alpha_adjust_step 必须 > 0")
	if adjust_notional <= 0:
		raise ValueError("adjust_notional 必须 > 0")
	if commission_rate < 0 or stamp_duty_rate < 0:
		raise ValueError("commission_rate / stamp_duty_rate 不能为负数")
	try:
		force_flatten_ts_ns = to_ns_utc(trade_date, force_flatten_hhmmss)
	except ValueError as exc:
		raise ValueError("force_flatten_hhmmss 格式须为 HH:MM:SS") from exc
	stuck_check_cutoff_ts_ns = to_ns_utc(trade_date, "14:45:00")

	if len(alpha_records) == 0:
		return [], summarize_trades([])

	_use_twap = twap_by_notional_enabled(twap_slice_notional, order_size_mode)
	'''这里判断是否需要用twap，如果需要用twap，则_use_twap为True，否则为False'''
	_twap_slice = float(twap_slice_notional) if twap_slice_notional is not None else 0.0

	hbt = build_hbt(
		eventstream_path=eventstream_path,
		lot_size=lot_size,
		tick_size=tick_size,
		order_latency_ns=order_latency_ns,
		roi_lb=roi_lb,
		roi_ub=roi_ub,
	)

	trades: list[Trade] = []
	# 已为该平仓 oid 记过 Trade；防止撮合层对同一 fully-filled oid 在多步重复触发 is_order_filled
	exit_oid_traded: set[int] = set()
	current: DynamicPosition | None = None
	# 开仓/加仓挂单：oid -> (方向, alpha, 股数, 标签, 来源 tw|ld|op)；见 rules.md §3.2
	pending_entry: dict[int, tuple[int, float, int, Literal["initial", "add"], EntrySrc]] = {}
	# 平仓挂单：oid -> ("full"|"partial", 平仓股数)；submit_ts 记录挂单时刻（纳秒）用于超时撤单
	pending_exit: dict[int, tuple[Literal["full", "partial"], int]] = {}
	pending_exit_submit_ts: dict[int, int] = {}

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
	# 当前未成交的「强平全平」挂单 oid；强平分支不撤该单，避免每步撤掉刚挂的强平单
	forced_flatten_order_id: int | None = None
	# TWAP：`twap_tgt_sh` 计划总股数（阶梯加/减会修订）；`twap_rem_sh` 尚未成交/挂出的补仓股数
	twap_tgt_sh: int = 0
	twap_rem_sh: int = 0
	twap_entry_side: int = 0
	twap_entry_alpha: float = 0.0

	try:
		while True:
			status = hbt.elapse(step_ns)
			now_ts = int(hbt.current_timestamp)
			depth = hbt.depth(0)

			def _twap_clear_tgt_if_idle() -> None:
				"""TWAP 补仓已尽且无在途 tw/ld 加仓单时，清零目标以免阶梯误判。"""
				nonlocal twap_tgt_sh
				if twap_tgt_sh <= 0:
					return
				if twap_rem_sh > 0:
					return
				if any(v[4] == "tw" for v in pending_entry.values()):
					return
				if any(v[4] == "ld" for v in pending_entry.values()):
					return
				twap_tgt_sh = 0

			# --- 开仓、加仓限价单成交 → 更新均价与股数 ---
			filled_entry_ids: list[int] = []
			for oid, (side, alpha_val, shares, tag, esrc) in pending_entry.items():
				#去看pending_entry里的订单是否成交
				order = hbt.orders(0).get(oid)
				#订单成交返回True，也就会跳出if
				if not is_order_filled(order):
					continue
				entry_ts_ns = int(order.exch_timestamp)
				entry_px = float(order.exec_price)
				filled_sh = int(float(order.exec_qty))
				bl, bs = _dynamic_long_short_exposure(current)
				if current is None:
					al = filled_sh if side > 0 else 0
					ash = filled_sh if side < 0 else 0
				else:
					new_sh = int(current.shares) + filled_sh
					if current.side > 0:
						al, ash = new_sh, 0
					else:
						al, ash = 0, new_sh
				dl, ds = open_ledger_increments(bl, bs, al, ash, entry_px)
				used_long += dl
				used_short += ds
				if current is None:
					current = DynamicPosition(
						side=side,
						shares=filled_sh,
						avg_entry_px=entry_px,
						alpha_0=alpha_val,
						first_entry_ts_ns=entry_ts_ns,
						target_exit_ts_ns=hold_target_ts_excluding_lunch(entry_ts_ns, hold_minutes),
					)
				else:
					old_sh = current.shares
					new_sh = old_sh + filled_sh
					#股数加权计算得到均价
					current.avg_entry_px = float(
						(current.avg_entry_px * old_sh + entry_px * filled_sh) / new_sh if new_sh > 0 else current.avg_entry_px
					)
					current.shares = int(new_sh)
				if twap_tgt_sh > 0:
					if esrc == "tw":
						twap_rem_sh = max(0, int(twap_rem_sh) - int(filled_sh))
					elif esrc == "ld":
						twap_tgt_sh = int(twap_tgt_sh) + int(filled_sh)
						# ld 成交本身已经增加了当前仓位；此处仅提高总目标，不应再把 remaining 叠加一次。
				filled_entry_ids.append(oid)
			for oid in filled_entry_ids:
				pending_entry.pop(oid, None)
			_twap_clear_tgt_if_idle()

			# --- 平仓限价单成交 → 生成 Trade；部分减仓后均价不变 ---
			filled_exit_ids: list[int] = []
			for oid, (_kind, _close_sh) in pending_exit.items():
				order = hbt.orders(0).get(oid)
				if not is_order_filled(order):
					continue
				if oid in exit_oid_traded:
					filled_exit_ids.append(oid)
					continue
				if current is None:
					filled_exit_ids.append(oid)
					continue
				exit_ts_ns = int(order.exch_timestamp)
				exit_px = float(order.exec_price)
				filled_sh = int(float(order.exec_qty))
				filled_sh = min(filled_sh, int(current.shares))
				if filled_sh <= 0:
					filled_exit_ids.append(oid)
					continue
				side_str = "long" if current.side > 0 else "short"
				trades.append(
					_finalize_trade_chunk(
						side_str=side_str,
						avg_entry_px=current.avg_entry_px,
						exit_px=exit_px,
						shares=filled_sh,
						entry_ts_ns=current.first_entry_ts_ns,
						exit_ts_ns=exit_ts_ns,
						alpha_signal=current.alpha_0,
						commission_rate=commission_rate,
						stamp_duty_rate=stamp_duty_rate,
					)
				)
				exit_oid_traded.add(oid)
				current.shares -= int(filled_sh)
				if current.shares <= 0:
					current = None
					twap_rem_sh = 0
					twap_tgt_sh = 0
				else:
					if twap_tgt_sh > 0:
						twap_tgt_sh = max(0, int(twap_tgt_sh) - int(filled_sh))
						twap_rem_sh = max(0, int(twap_rem_sh) - int(filled_sh))

				filled_exit_ids.append(oid)
			for oid in filled_exit_ids:
				pending_exit.pop(oid, None)
				pending_exit_submit_ts.pop(oid, None)
				if forced_flatten_order_id == oid:
					forced_flatten_order_id = None

			# --- 平仓挂单超时：>60s 未完全成交则撤单并解锁，后续定时/TP/SL/alpha 可再挂 ---
			for oid in list(pending_exit.keys()):
				sub_ts = int(pending_exit_submit_ts.get(oid, now_ts))
				if now_ts - sub_ts < EXIT_ORDER_STALE_NS:
					continue
				cancel_exit_order_if_active(hbt, oid)
				pending_exit.pop(oid, None)
				pending_exit_submit_ts.pop(oid, None)
				if forced_flatten_order_id == oid:
					forced_flatten_order_id = None

			def _compute_exit_close_shares_this_step() -> int:
				"""全平路径下本步平仓股数：非 TWAP 为满仓；TWAP 为至多 ``twap_slice_notional`` 对应股数（整手）。"""
				assert current is not None
				if not _use_twap:
					return int(current.shares)
				exit_ref_px = float(depth.best_bid) if current.side > 0 else float(depth.best_ask)
				full_notional = float(current.shares) * exit_ref_px
				# 当单片名义已覆盖整仓时直接返回整仓，避免浮点取整把 200 算成 100。
				if float(_twap_slice) + 1e-9 >= full_notional:
					return int(current.shares)
				slice_n = min(_twap_slice, float(current.shares) * exit_ref_px)
				slice_uncapped = compute_order_shares(
					entry_ref_px=exit_ref_px,
					notional=float(slice_n),
					lot_size=lot_size,
					order_size_mode="notional",
					order_lots=order_lots,
				)
				close_sh = int(slice_uncapped)
				close_sh = (close_sh // lot_size) * lot_size
				return min(close_sh, int(current.shares))

			def _exit_shares_oneshot() -> int:
				"""rules.md §3.2：信号反向 / 阶梯减超 TWAP 目标时一次性全平股数（含不足一手尾仓）。"""
				assert current is not None
				return int(current.shares)

			def _submit_exit_limit_chunk(close_sh: int, *, forced: bool) -> None:
				nonlocal current, next_order_id, pending_exit, pending_exit_submit_ts, forced_flatten_order_id
				nonlocal blocked_timed_exit_l1_count, blocked_forced_exit_l1_count
				assert current is not None
				if close_sh <= 0:
					return
				exit_side = -1 if current.side > 0 else 1
				if not _is_tradable_for_side_l1_min1lot(depth, exit_side, lot_size):
					if forced:
						blocked_forced_exit_l1_count += 1
					else:
						blocked_timed_exit_l1_count += 1
					return
				exit_px = get_aggressive_limit_price(
					best_bid=float(depth.best_bid),
					best_ask=float(depth.best_ask),
					side=exit_side,
					tick_size=tick_size,
					aggressive_ticks=aggressive_ticks,
				)
				oid = next_order_id
				next_order_id += 1
				_submit_close_shares(hbt, oid, current.side, int(close_sh), exit_px)
				kind: Literal["full", "partial"] = (
					"full" if int(close_sh) >= int(current.shares) else "partial"
				)
				pending_exit[oid] = (kind, int(close_sh))
				pending_exit_submit_ts[oid] = now_ts
				if forced:
					forced_flatten_order_id = oid

			def _try_submit_full_exit(_reason: str) -> None:
				nonlocal current, next_order_id, twap_rem_sh, twap_tgt_sh, blocked_timed_exit_l1_count
				assert current is not None
				twap_rem_sh = 0
				twap_tgt_sh = 0
				if pending_exit:
					return
				if _reason in _FULL_EXIT_ONESHOT_REASONS:
					close_sh = _exit_shares_oneshot()
					if close_sh <= 0:
						return
				else:
					close_sh = _compute_exit_close_shares_this_step()
					if close_sh < lot_size:
						blocked_timed_exit_l1_count += 1
						return
				_submit_exit_limit_chunk(close_sh, forced=False)

			def _try_submit_twap_entry_continue() -> None:
				"""TWAP 开仓：无未完成开仓挂单时，按步挂下一片（名义上限 ``twap_slice_notional``）。"""
				nonlocal current, next_order_id, twap_rem_sh, blocked_entry_l1_count, blocked_aum_count
				if (
					not _use_twap
					or twap_rem_sh <= 0
					or pending_entry
					or flatten_triggered
				):
					return
				side = int(twap_entry_side)
				if not _is_tradable_for_side_l1_min1lot(depth, side, lot_size):
					blocked_entry_l1_count += 1
					return
				entry_ref_px = float(depth.best_ask) if side > 0 else float(depth.best_bid)
				slice_n = entry_slice_notional_cap(
					twap_slice_notional=_twap_slice,
					remaining_shares=int(twap_rem_sh),
					entry_ref_px=entry_ref_px,
				)
				slice_uncapped = shares_for_notional_slice(
					entry_ref_px=entry_ref_px,
					slice_notional=float(slice_n),
					lot_size=lot_size,
					order_lots=order_lots,
				)
				if slice_uncapped < lot_size:
					if int(twap_rem_sh) > 0 and int(twap_rem_sh) < int(lot_size):
						twap_rem_sh = 0
					return
				step_sh = min(int(slice_uncapped), int(twap_rem_sh))
				step_sh = (step_sh // lot_size) * lot_size
				if step_sh < lot_size:
					twap_rem_sh = 0
					return
				used_open = used_long if side > 0 else used_short
				step_sh = capped_entry_shares(
					entry_ref_px,
					step_sh,
					lot_size,
					aum,
					used_open,
					trade_unit_clip_frac,
				)
				step_sh = min(int(step_sh), int(twap_rem_sh))
				step_sh = (step_sh // lot_size) * lot_size
				if step_sh < lot_size:
					if aum is not None and slice_uncapped >= lot_size:
						blocked_aum_count += 1
					return
				tag: Literal["initial", "add"] = "initial" if current is None else "add"
				entry_px = get_aggressive_limit_price(
					best_bid=float(depth.best_bid),
					best_ask=float(depth.best_ask),
					side=side,
					tick_size=tick_size,
					aggressive_ticks=aggressive_ticks,
				)
				oid = next_order_id
				next_order_id += 1
				submit_entry_order(hbt, oid, side, step_sh, entry_px)
				pending_entry[oid] = (side, float(twap_entry_alpha), step_sh, tag, "tw")

			# --- 盯市盈亏：止盈 / 止损 / 持有到期（先于当前步 alpha 消费）---
			if current is not None and not pending_exit:
				mark = _mid_mark(depth, last_trade_px, tick_size)
				if now_ts >= current.target_exit_ts_ns:
					_try_submit_full_exit("time")
				elif current.side > 0:
					hit = _tp_sl_hit_long(current.avg_entry_px, mark, take_profit_pct, stop_loss_pct)
					if hit:
						_try_submit_full_exit(hit)
				else:
					hit = _tp_sl_hit_short(current.avg_entry_px, mark, take_profit_pct, stop_loss_pct)
					if hit:
						_try_submit_full_exit(hit)

			# --- 日末强平时刻起只平不开 ---
			if now_ts >= force_flatten_ts_ns:
				if not flatten_triggered:
					flatten_triggered = True
				if current is not None:
					twap_rem_sh = 0
					twap_tgt_sh = 0
					# 强平：撤掉非强平来源的未成交平仓单（保留当前 forced_flatten_order_id），再在无挂单时挂强平全平
					for oid in list(pending_exit.keys()):
						if forced_flatten_order_id is not None and oid == forced_flatten_order_id:
							continue
						cancel_exit_order_if_active(hbt, oid)
						pending_exit.pop(oid, None)
						pending_exit_submit_ts.pop(oid, None)
					if not pending_exit:
						close_sh = _compute_exit_close_shares_this_step()
						if close_sh < lot_size:
							blocked_forced_exit_l1_count += 1
						else:
							_submit_exit_limit_chunk(close_sh, forced=True)

			# --- Alpha：新开仓（仅空仓）、反向信号达阈值全平、相对 α₀ 阶梯加减仓 ---
			while (not flatten_triggered) and next_signal_idx < len(alpha_records) and alpha_records[next_signal_idx][0] <= now_ts:
				_, alpha_val = alpha_records[next_signal_idx]
				next_signal_idx += 1

				if current is None:
					if twap_rem_sh > 0:
						continue
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
					if _use_twap:
						twap_tgt_sh = int(shares)
						twap_rem_sh = int(shares)
						twap_entry_side = int(side)
						twap_entry_alpha = float(alpha_val)
						_try_submit_twap_entry_continue()
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
					pending_entry[oid] = (side, alpha_val, shares, "initial", "op")
					continue

				assert current is not None
				if pending_exit:
					continue

				# 信号反向：与开仓对称，须达到 alpha_threshold 幅度才全平
				if (current.side > 0 and alpha_val <= -alpha_threshold) or (
					current.side < 0 and alpha_val >= alpha_threshold
				):
					_try_submit_full_exit("alpha_flip")
					continue

				# 阶梯加减仓须与开仓一致先过方向阈值，再相对 α₀ 判步长
				if current.side > 0:
					if float(alpha_val) < float(alpha_threshold):
						continue
				else:
					if float(alpha_val) > float(-alpha_threshold):
						continue

				# 相对 α₀ 的阶梯：多 α−α₀，空 |α|−|α₀|；按步取整 k×adjust_notional（每点仅用当前 delta，不记历史；连发同 delta 可重复）
				if current.side > 0:
					delta = float(alpha_val - current.alpha_0)
				else:
					delta = float(abs(alpha_val) - abs(current.alpha_0))

				step_f = float(alpha_adjust_step)
				k_add = int(math.floor(delta / step_f)) if delta >= step_f else 0
				k_red = int(math.floor((-delta) / step_f)) if delta <= -step_f else 0
				if k_add <= 0 and k_red <= 0:
					continue

				ref_px = float(depth.best_ask) if current.side > 0 else float(depth.best_bid)

				if k_add > 0:
					n_add = float(k_add) * float(adjust_notional)
					step_uncapped = compute_order_shares(
						entry_ref_px=ref_px,
						notional=n_add,
						lot_size=lot_size,
						order_size_mode="notional",
						order_lots=order_lots,
					)
					used_open_add = used_long if current.side > 0 else used_short
					step_sh = capped_entry_shares(
						ref_px, step_uncapped, lot_size, aum, used_open_add, trade_unit_clip_frac
					)
					if step_sh < lot_size:
						if aum is not None and step_uncapped >= lot_size:
							blocked_aum_count += 1
						continue
					add_side = current.side
					if not _is_tradable_for_side_l1_min1lot(depth, add_side, lot_size):
						blocked_entry_l1_count += 1
						continue
					px_in = get_aggressive_limit_price(
						best_bid=float(depth.best_bid),
						best_ask=float(depth.best_ask),
						side=add_side,
						tick_size=tick_size,
						aggressive_ticks=aggressive_ticks,
					)
					oid = next_order_id
					next_order_id += 1
					submit_entry_order(hbt, oid, add_side, step_sh, px_in)
					pending_entry[oid] = (add_side, alpha_val, step_sh, "add", "ld")
					continue

				# k_red > 0
				n_red = float(k_red) * float(adjust_notional)
				step_uncapped = compute_order_shares(
					entry_ref_px=ref_px,
					notional=n_red,
					lot_size=lot_size,
					order_size_mode="notional",
					order_lots=order_lots,
				)
				reduce_sh = min(int(step_uncapped), int(current.shares))
				reduce_sh = (reduce_sh // lot_size) * lot_size
				if reduce_sh < lot_size:
					if current.shares > 0:
						reduce_sh = int(current.shares)
					else:
						continue
				exit_side = -1 if current.side > 0 else 1
				px_out = get_aggressive_limit_price(
					best_bid=float(depth.best_bid),
					best_ask=float(depth.best_ask),
					side=exit_side,
					tick_size=tick_size,
					aggressive_ticks=aggressive_ticks,
				)
				if reduce_sh < lot_size:
					continue
				if not _is_tradable_for_side_l1_min1lot(depth, exit_side, lot_size):
					blocked_entry_l1_count += 1
					continue
				if twap_tgt_sh > 0 and int(reduce_sh) > int(twap_tgt_sh):
					twap_rem_sh = 0
					twap_tgt_sh = 0
					_try_submit_full_exit("ladder_over_twap")
					continue
				oid = next_order_id
				next_order_id += 1
				_submit_close_shares(hbt, oid, current.side, reduce_sh, px_out)
				pending_exit[oid] = ("partial", reduce_sh)
				pending_exit_submit_ts[oid] = now_ts

			_try_submit_twap_entry_continue()
			_twap_clear_tgt_if_idle()

			# 回放结束后，只要后续没有未消费信号、未成交开仓单和未完成平仓流程，就退出。
			if status != 0:
				# 日末兜底：确保未完成持仓全部平仓，避免隔夜仓位。
				n_fallback_flatten_after_1445 = 0
				n_fallback_long_after_1445 = 0
				n_fallback_short_after_1445 = 0
				shares_unflattenable_after_1445 = 0
				if current is not None:
					exit_side = -1 if current.side > 0 else 1
					# “平不掉”定义：14:45 后进入兜底时，该方向仍不满足对手盘一档可交易约束。
					if (now_ts >= stuck_check_cutoff_ts_ns) and (
						not _is_tradable_for_side_l1_min1lot(depth, exit_side, lot_size)
					):
						n_fallback_flatten_after_1445 = 1
						shares_unflattenable_after_1445 = int(current.shares)
						if current.side > 0:
							n_fallback_long_after_1445 = 1
						else:
							n_fallback_short_after_1445 = 1
					fallback_px = _forced_flatten_price(
						last_trade_px=last_trade_px,
						entry_px=current.avg_entry_px,
						tick_size=tick_size,
						force_flatten_extra_ticks=force_flatten_extra_ticks,
					)
					fpx = float(fallback_px)
					side_str = "long" if current.side > 0 else "short"
					sh_fb = int(current.shares)
					if sh_fb > 0:
						trades.append(
							_finalize_trade_chunk(
								side_str=side_str,
								avg_entry_px=current.avg_entry_px,
								exit_px=fpx,
								shares=sh_fb,
								entry_ts_ns=current.first_entry_ts_ns,
								exit_ts_ns=now_ts,
								alpha_signal=current.alpha_0,
								commission_rate=commission_rate,
								stamp_duty_rate=stamp_duty_rate,
							)
						)
						current = None

				# 回放结束时，未成交开仓单直接忽略；仓位已在上面兜底平仓。
				pending_entry.clear()
				pending_exit.clear()
				pending_exit_submit_ts.clear()
				twap_rem_sh = 0
				twap_tgt_sh = 0
				fallback_stats_after_1445 = (
					n_fallback_flatten_after_1445,
					n_fallback_long_after_1445,
					n_fallback_short_after_1445,
				)
				fallback_shares_after_1445 = shares_unflattenable_after_1445
				all_exited = current is None
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
	# fallback_*_after_1445 / shares_*：14:45 后兜底时仍不满足对手盘一档可交易约束的平不掉仓位计数与股数。
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
