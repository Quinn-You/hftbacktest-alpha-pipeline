#!/usr/bin/env python3
"""
legacy 与 dynamic_hold 共用的 hft 撮合内核与工具（不 import strategy.legacy）。

主要包括：
1. alpha 文件定位与读取。
2. 时间字段与回测时间戳转换。
3. hftbacktest 引擎初始化、订单与盘口工具。
4. 成交结果的汇总与表格化。

固定持仓主循环见 ``strategy.legacy.run_backtest_for_alpha_records``。
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import asdict
from dataclasses import dataclass
from datetime import datetime
from datetime import timezone
from pathlib import Path
from typing import Any
from typing import Iterable

import numpy as np
import polars as pl


# 这里复制 alpha_bt 的底层依赖和回测模板，保证 pipeline 后续不依赖 alpha_bt 文件本身。
try:
	from hftbacktest import BUY_EVENT
	from hftbacktest import DEPTH_EVENT
	from hftbacktest import GTC
	from hftbacktest import LIMIT
	from hftbacktest import MARKET
	from hftbacktest import SELL_EVENT
	from hftbacktest import BacktestAsset
	from hftbacktest import ROIVectorMarketDepthBacktest
except Exception:
	GTC = None
	LIMIT = None
	MARKET = None
	BacktestAsset = None
	ROIVectorMarketDepthBacktest = None
	DEPTH_EVENT = np.uint64(0x1)
	BUY_EVENT = np.uint64(0x20000000)
	SELL_EVENT = np.uint64(0x10000000)


@dataclass
class Trade:
	"""单笔已完成交易的标准结果结构。

	策略层和报表层都依赖这个统一结构，避免各层重复定义字段。
	"""

	side: str
	entry_ts_ns: int
	exit_ts_ns: int
	entry_px: float
	exit_px: float
	shares: int
	entry_notional: float
	exit_notional: float
	pnl_gross: float
	commission: float
	stamp_duty: float
	total_cost: float
	pnl: float
	ret: float
	alpha_signal: float


def to_ns_utc(date_str: str, hhmmss: str) -> int:
	"""把 YYYY-MM-DD + HH:MM:SS 转成 UTC 纳秒时间戳。"""
	dt = datetime.strptime(f"{date_str} {hhmmss}", "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
	return int(dt.timestamp() * 1_000_000_000)


def hold_target_ts_excluding_lunch(entry_ts_ns: int, hold_minutes: int) -> int:
	"""计算平仓目标时间：11:30-13:00 午休不计入持仓时间。"""
	hold_ns = int(hold_minutes * 60 * 1_000_000_000)
	raw_target = entry_ts_ns + hold_ns

	entry_dt = datetime.fromtimestamp(entry_ts_ns / 1e9, tz=timezone.utc)
	lunch_start = entry_dt.replace(hour=11, minute=30, second=0, microsecond=0)
	lunch_end = entry_dt.replace(hour=13, minute=0, second=0, microsecond=0)
	lunch_start_ns = int(lunch_start.timestamp() * 1_000_000_000)
	lunch_end_ns = int(lunch_end.timestamp() * 1_000_000_000)

	if entry_ts_ns < lunch_start_ns and raw_target > lunch_start_ns:
		return raw_target + (lunch_end_ns - lunch_start_ns)
	return raw_target


def ns_to_text(ts_ns: int) -> str:
	"""把纳秒时间戳转为报表可读的文本时间。"""
	return datetime.fromtimestamp(ts_ns / 1e9, tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def resolve_alpha_path(alpha_path: Path, trade_date: str) -> Path:
	"""兼容直接传 parquet 文件或传 alpha_monthly 根目录两种用法。"""
	if alpha_path.is_file():
		return alpha_path

	target_name = f"{trade_date}.parquet"
	search_root = alpha_path.parent if alpha_path.suffix == ".parquet" else alpha_path
	if search_root.name != "alpha_monthly":
		for parent in [search_root, *search_root.parents]:
			if parent.name == "alpha_monthly":
				search_root = parent
				break

	if search_root.exists():
		matches = sorted(search_root.rglob(target_name))
		if len(matches) == 1:
			return matches[0]
		if len(matches) > 1:
			raise ValueError(f"找到多个 alpha 文件，请手工指定更精确路径：{[str(item) for item in matches[:5]]}")

	raise FileNotFoundError(f"未找到 alpha 文件：{alpha_path}，且在 {search_root} 下也未找到 {target_name}")


def load_alpha_records_grouped(
	alpha_path: Path,
	trade_date: str,
	symbols: Iterable[str] | None = None,
) -> dict[str, list[tuple[int, float]]]:
	"""一次性读取当日 alpha，并按股票分组。"""
	filters = None
	if symbols is not None:
		symbol_list = sorted({str(symbol) for symbol in symbols if symbol is not None})
		if len(symbol_list) == 0:
			return {}
		filters = pl.col("symbol").is_in(symbol_list)

	query = pl.read_parquet(alpha_path)
	if filters is not None:
		query = query.filter(filters)

	df = query.select(["symbol", "time", "alpha"])
	if df.height == 0:
		return {}

	records_by_symbol: dict[str, list[tuple[int, float]]] = defaultdict(list)
	for symbol, time_text, alpha_value in df.iter_rows():
		if symbol is None or time_text is None or alpha_value is None:
			continue
		records_by_symbol[str(symbol)].append((to_ns_utc(trade_date, str(time_text)), float(alpha_value)))

	for records in records_by_symbol.values():
		records.sort(key=lambda item: item[0])
	return dict(records_by_symbol)


def build_hbt(
	eventstream_path: Path,
	lot_size: int,
	tick_size: float,
	order_latency_ns: int,
	roi_lb: float,
	roi_ub: float,
) -> Any:
	"""根据 event stream 和撮合参数构造单资产回测引擎。"""

	if BacktestAsset is None or ROIVectorMarketDepthBacktest is None:
		raise RuntimeError("当前环境未安装 hftbacktest，无法使用引擎撮合回测")

	asset = (
		BacktestAsset()
		.data(str(eventstream_path))
		.linear_asset(1.0)
		.constant_order_latency(order_latency_ns, order_latency_ns)
		.power_prob_queue_model(2.0)
		.no_partial_fill_exchange()
		.tick_size(tick_size)
		.lot_size(lot_size)
		.roi_lb(roi_lb)
		.roi_ub(roi_ub)
	)
	return ROIVectorMarketDepthBacktest([asset])


def is_order_filled(order: object) -> bool:
	"""判断订单是否已经完全成交。"""
	return (order is not None) and (float(order.leaves_qty) <= 0.0) and (float(order.exec_qty) > 0.0)


# 平仓挂单超过该时长仍未完全成交则撤单并解锁策略层 pending（与 rules 一致：60s）。
EXIT_ORDER_STALE_NS = 60 * 1_000_000_000


def cancel_exit_order_if_active(hbt: Any, oid: int) -> None:
	"""对 asset 0 的平仓单做 best-effort 撤单；订单不存在或不可撤时静默忽略。"""
	try:
		order = hbt.orders(0).get(int(oid))
		if order is not None and bool(getattr(order, "cancellable", False)):
			hbt.cancel(0, int(oid), True)
	except Exception:
		pass


def submit_entry_order(
	hbt: Any,
	order_id: int,
	side: int,
	shares: int,
	price: float,
) -> None:
	"""按方向提交开仓单（使用可控价格的限价单）。"""
	if side > 0:
		hbt.submit_buy_order(0, order_id, float(price), float(shares), GTC, LIMIT, False)
	else:
		hbt.submit_sell_order(0, order_id, float(price), float(shares), GTC, LIMIT, False)


def compute_order_shares(
	entry_ref_px: float,
	notional: float,
	lot_size: int,
	order_size_mode: str,
	order_lots: int,
) -> int:
	"""根据下单规模模式计算每笔下单股数。"""
	if order_size_mode == "lots":
		return int(order_lots) * int(lot_size)
	shares = int((notional // entry_ref_px) // lot_size) * lot_size
	return int(shares)


def get_aggressive_limit_price(
	best_bid: float,
	best_ask: float,
	side: int,
	tick_size: float,
	aggressive_ticks: int,
) -> float:
	"""构造对手盘一价偏移 n tick 的限价单价格。"""
	offset = float(aggressive_ticks) * float(tick_size)
	#side为1时，是买，买对手盘的卖一价+offset，卖则是对手盘的买一价-offset
	if side > 0:
		return float(best_ask) + offset
	return max(float(tick_size), float(best_bid) - offset)


def _has_valid_book_for_side(depth: Any, side: int) -> bool:
	"""按交易方向检查一侧盘口：买需有效卖一，卖需有效买一（涨停卖盘空时仍可卖）。"""
	if side > 0:
		return np.isfinite(depth.best_ask) and float(depth.best_ask) > 0
	return np.isfinite(depth.best_bid) and float(depth.best_bid) > 0


def _extract_ladder_qtys(depth: Any, side: str, levels: int = 5) -> list[float]:
	"""从 depth 中尽力提取买/卖前 N 档挂量，兼容多种字段命名。"""

	if side not in {"bid", "ask"}:
		return []

	def _to_float(value: Any) -> float | None:
		try:
			val = float(value)
			return val if np.isfinite(val) else None
		except Exception:
			return None

	# 优先尝试 tick-based 接口（当前 hftbacktest 深度对象的稳定实现）。
	best_tick_attr = f"best_{side}_tick"
	qty_at_tick_attr = f"{side}_qty_at_tick"
	best_tick_raw = getattr(depth, best_tick_attr, None)
	qty_at_tick_fn = getattr(depth, qty_at_tick_attr, None)
	best_tick = _to_float(best_tick_raw)
	if best_tick is not None and callable(qty_at_tick_fn):
		base_tick = int(best_tick)
		out: list[float] = []
		for idx in range(levels):
			tick = base_tick - idx if side == "bid" else base_tick + idx
			qty = _to_float(qty_at_tick_fn(tick))
			if qty is None:
				break
			out.append(qty)
		if len(out) == levels:
			return out

	# 先尝试数组/序列字段。
	array_attr_candidates = [
		f"{side}_qty",
		f"{side}_size",
		f"{side}_vol",
		f"{side}_volume",
		f"{side}s",
	]
	for attr in array_attr_candidates:
		seq = getattr(depth, attr, None)
		if seq is None:
			continue
		try:
			values = list(seq)
		except Exception:
			continue
		if len(values) == 0:
			continue
		out: list[float] = []
		for item in values:
			# bids/asks 常见结构为 [(px, qty), ...]
			if isinstance(item, (tuple, list)) and len(item) >= 2:
				qty = _to_float(item[1])
			else:
				qty = _to_float(item)
			if qty is None:
				break
			out.append(qty)
			if len(out) >= levels:
				return out

	# 再尝试逐档命名字段。
	name_templates = [
		"{side}_qty{idx}",
		"{side}_size{idx}",
		"{side}_vol{idx}",
		"{side}_volume{idx}",
		"{side}_quantity{idx}",
	]
	for idx in range(1, levels + 1):
		found = None
		for template in name_templates:
			value = getattr(depth, template.format(side=side, idx=idx), None)
			if value is None:
				continue
			parsed = _to_float(value)
			if parsed is not None:
				found = parsed
				break
		if found is None:
			return []
		if idx == 1:
			out = [found]
		else:
			out.append(found)
	return out if "out" in locals() else []


def _is_tradable_for_side_l1_min1lot(depth: Any, side: int, lot_size: int) -> bool:
	"""按方向检查可交易性：对手盘第一档挂量 >= 1 手（整手 lot_size）。"""
	if not _has_valid_book_for_side(depth, side):
		return False
	if lot_size <= 0:
		return False

	opp_side = "ask" if side > 0 else "bid"
	qtys = _extract_ladder_qtys(depth, opp_side, levels=1)
	if len(qtys) < 1:
		# 无法稳定提取对手盘一档时按不可交易处理，避免误下单。
		return False
	q0 = qtys[0]
	return bool(np.isfinite(q0) and float(q0) >= float(lot_size))


def _forced_flatten_price(
	last_trade_px: float | None,
	entry_px: float,
	tick_size: float,
	force_flatten_extra_ticks: int,
) -> float:
	"""计算日末兜底平仓价格：统一使用当日最后一笔可用成交价。"""
	# 兼容保留参数 force_flatten_extra_ticks；该模式下不再使用额外滑点。
	_ = force_flatten_extra_ticks
	if last_trade_px is not None and np.isfinite(last_trade_px) and float(last_trade_px) > 0:
		return float(last_trade_px)
	return max(float(tick_size), float(entry_px))


def _load_last_trade_price_from_eventstream(eventstream_path: Path) -> float | None:
	"""从 eventstream 中提取当日最后一笔可用成交价。"""
	try:
		arr = np.load(eventstream_path)["data"]
	except Exception:
		return None
	if arr.size == 0:
		return None
	trade_mask = (arr["ev"] & np.uint64(0x2)) != 0
	if not np.any(trade_mask):
		return None
	trade_px = arr["px"][trade_mask]
	if trade_px.size == 0:
		return None
	valid_px = trade_px[np.isfinite(trade_px) & (trade_px > 0)]
	if valid_px.size == 0:
		return None
	return float(valid_px[-1])


def summarize_trades(trades: Iterable[Trade]) -> dict[str, float]:
	"""把交易列表压缩成策略层和报表层可直接使用的摘要指标。"""
	trade_list = list(trades)
	if not trade_list:
		return {
			"n_trades": 0,
			"n_long": 0,
			"n_short": 0,
			"win_rate": 0.0,
			"total_pnl": 0.0,
			"avg_pnl": 0.0,
			"avg_ret": 0.0,
		}

	pnls = np.array([item.pnl for item in trade_list], dtype=np.float64)
	rets = np.array([item.ret for item in trade_list], dtype=np.float64)
	sides = [item.side for item in trade_list]
	return {
		"n_trades": int(len(trade_list)),
		"n_long": int(sum(1 for side in sides if side == "long")),
		"n_short": int(sum(1 for side in sides if side == "short")),
		"win_rate": float((pnls > 0).mean()),
		"total_pnl": float(pnls.sum()),
		"avg_pnl": float(pnls.mean()),
		"avg_ret": float(rets.mean()),
	}


def trades_to_dataframe(trades: list[Trade]) -> pl.DataFrame:
	"""把内部 Trade 对象列表转换成可落盘的 Polars DataFrame。"""
	rows = []
	for trade in trades:
		row = asdict(trade)
		row["entry_time"] = ns_to_text(trade.entry_ts_ns)
		row["exit_time"] = ns_to_text(trade.exit_ts_ns)
		rows.append(row)
	if not rows:
		return pl.DataFrame(
			{
				"side": [],
				"entry_time": [],
				"exit_time": [],
				"entry_px": [],
				"exit_px": [],
				"shares": [],
				"entry_notional": [],
				"exit_notional": [],
				"pnl_gross": [],
				"commission": [],
				"stamp_duty": [],
				"total_cost": [],
				"pnl": [],
				"ret": [],
				"alpha_signal": [],
			}
		)
	return pl.DataFrame(rows).select(
		[
			"side",
			"entry_time",
			"exit_time",
			"entry_px",
			"exit_px",
			"shares",
			"entry_notional",
			"exit_notional",
			"pnl_gross",
			"commission",
			"stamp_duty",
			"total_cost",
			"pnl",
			"ret",
			"alpha_signal",
		]
	)