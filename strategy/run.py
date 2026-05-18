#!/usr/bin/env python3
"""策略执行：逐股票 hftbacktest 回测（legacy / dynamic_hold + 本包 backtest 共用内核，不 import data/analysis/tool）。"""

from __future__ import annotations

import math
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import polars as pl

from .backtest import trades_to_dataframe
from .legacy import run_backtest_for_alpha_records
from .dynamic_hold import run_dynamic_hold_backtest


@dataclass
class PreparedSymbolInput:
	"""与 data 层 PreparedSymbol 字段对齐；由 main 从 data bundle 填入。"""

	symbol: str
	market: str
	eventstream_path: Path
	alpha_points: int
	status: str
	message: str = ""


@dataclass
class StrategyUniverseInput:
	"""策略输入：由 main 构造，避免 strategy 依赖 data 模块。"""

	trade_date: str
	repo_dir: Path
	prepared_symbols: list[PreparedSymbolInput]
	alpha_records_by_symbol: dict[str, list[tuple[int, float]]]


@dataclass
class StrategyConfig:
	alpha_threshold: float
	hold_minutes: int
	notional: float
	order_size_mode: str
	order_lots: int
	lot_size: int
	aggressive_ticks: int
	step_ns: int
	tick_size: float
	order_latency_ns: int
	roi_lb: float
	roi_ub: float
	commission_rate: float
	stamp_duty_rate: float
	force_flatten_hhmmss: str
	force_flatten_extra_ticks: int
	entry_liq: str = "taker"
	exit_liq: str = "taker"
	# legacy | dynamic_hold
	strategy_mode: str = "legacy"
	alpha_adjust_step: float = 0.01
	adjust_notional: float = 10_000.0
	take_profit_pct: float | None = None
	stop_loss_pct: float | None = None
	# AUM：``aum_by_symbol`` 为 None 时关闭；否则每标的 ``aum``；引擎内 ``used_long`` / ``used_short`` 仅随开仓型成交增加
	aum_by_symbol: dict[str, float] | None = None
	trade_unit_clip_frac: float | None = None
	# dynamic_hold：按名义切片开/平仓（与 ``step_ns`` 对齐；``None`` 关闭）；仅 ``order_size_mode=notional`` 时生效
	twap_slice_notional: float | None = None


@dataclass
class StrategyRunResult:
	trade_date: str
	repo_dir: Path
	hold_minutes: int
	summary_df: pl.DataFrame
	all_trades_df: pl.DataFrame
	failures_df: pl.DataFrame


def _safe_sharpe(realized_returns: list[float]) -> float | None:
	if len(realized_returns) < 2:
		return None
	arr = np.asarray(realized_returns, dtype=np.float64)
	std = float(arr.std(ddof=1))
	if not np.isfinite(std) or std <= 0:
		return None
	return float(math.sqrt(arr.size) * arr.mean() / std)


def _strategy_dir(repo_dir: Path) -> Path:
	return repo_dir / "strategy_layer"


def run_universe_strategy(bundle: StrategyUniverseInput, config: StrategyConfig) -> StrategyRunResult:
	strategy_dir = _strategy_dir(bundle.repo_dir)
	trades_dir = strategy_dir / "trades"
	strategy_dir.mkdir(parents=True, exist_ok=True)
	trades_dir.mkdir(parents=True, exist_ok=True)

	summary_rows: list[dict[str, object]] = []
	failure_rows: list[dict[str, str]] = []
	all_trade_frames: list[pl.DataFrame] = []

	for prepared in bundle.prepared_symbols:
		alpha_records = bundle.alpha_records_by_symbol[prepared.symbol]
		symbol_aum: float | None = None
		if config.aum_by_symbol is not None:
			symbol_aum = float(config.aum_by_symbol.get(prepared.symbol, 0.0))
		try:
			if config.strategy_mode == "dynamic_hold":
				trades, summary = run_dynamic_hold_backtest(
					eventstream_path=prepared.eventstream_path,
					alpha_records=alpha_records,
					trade_date=bundle.trade_date,
					alpha_threshold=config.alpha_threshold,
					hold_minutes=config.hold_minutes,
					notional=config.notional,
					order_size_mode=config.order_size_mode,
					order_lots=config.order_lots,
					lot_size=config.lot_size,
					aggressive_ticks=config.aggressive_ticks,
					step_ns=config.step_ns,
					tick_size=config.tick_size,
					order_latency_ns=config.order_latency_ns,
					roi_lb=config.roi_lb,
					roi_ub=config.roi_ub,
					commission_rate=config.commission_rate,
					stamp_duty_rate=config.stamp_duty_rate,
					force_flatten_hhmmss=config.force_flatten_hhmmss,
					force_flatten_extra_ticks=config.force_flatten_extra_ticks,
					entry_liq=config.entry_liq,
					exit_liq=config.exit_liq,
					alpha_adjust_step=config.alpha_adjust_step,
					adjust_notional=config.adjust_notional,
					take_profit_pct=config.take_profit_pct,
					stop_loss_pct=config.stop_loss_pct,
					aum=symbol_aum,
					trade_unit_clip_frac=config.trade_unit_clip_frac,
					twap_slice_notional=config.twap_slice_notional,
				)
			else:
				trades, summary = run_backtest_for_alpha_records(
					eventstream_path=prepared.eventstream_path,
					alpha_records=alpha_records,
					trade_date=bundle.trade_date,
					alpha_threshold=config.alpha_threshold,
					hold_minutes=config.hold_minutes,
					notional=config.notional,
					order_size_mode=config.order_size_mode,
					order_lots=config.order_lots,
					lot_size=config.lot_size,
					aggressive_ticks=config.aggressive_ticks,
					step_ns=config.step_ns,
					tick_size=config.tick_size,
					order_latency_ns=config.order_latency_ns,
					roi_lb=config.roi_lb,
					roi_ub=config.roi_ub,
					commission_rate=config.commission_rate,
					stamp_duty_rate=config.stamp_duty_rate,
					force_flatten_hhmmss=config.force_flatten_hhmmss,
					force_flatten_extra_ticks=config.force_flatten_extra_ticks,
					entry_liq=config.entry_liq,
					exit_liq=config.exit_liq,
					aum=symbol_aum,
					trade_unit_clip_frac=config.trade_unit_clip_frac,
				)
		except Exception as exc:
			failure_rows.append({"symbol": prepared.symbol, "reason": f"策略执行失败: {exc}"})
			continue

		trades_df = trades_to_dataframe(trades)
		trades_path = trades_dir / f"{prepared.symbol}_{bundle.trade_date}.csv"
		trades_df.write_csv(trades_path)

		if trades_df.height > 0:
			all_trade_frames.append(trades_df.with_columns(pl.lit(prepared.symbol).alias("symbol")))

		realized_return_values = (
			trades_df.with_columns(
				pl.when(pl.col("entry_notional") > 0)
				.then(pl.col("pnl") / pl.col("entry_notional"))
				.otherwise(0.0)
				.alias("realized_return")
			)
			.get_column("realized_return")
			.cast(pl.Float64)
			.to_list()
			if trades_df.height > 0
			else []
		)
		summary_rows.append(
			{
				"symbol": prepared.symbol,
				"market": prepared.market,
				"alpha_points": prepared.alpha_points,
				"n_trades": int(summary["n_trades"]),
				"n_long": int(summary["n_long"]),
				"n_short": int(summary["n_short"]),
				"win_rate": float(summary["win_rate"]),
				"total_pnl": float(summary["total_pnl"]),
				"avg_pnl": float(summary["avg_pnl"]),
				"avg_ret": float(summary["avg_ret"]),
				"sharpe_ratio": _safe_sharpe(realized_return_values),
				"blocked_entry_l1_count": int(summary.get("blocked_entry_l1_count", 0)),
				"blocked_timed_exit_l1_count": int(summary.get("blocked_timed_exit_l1_count", 0)),
				"blocked_forced_exit_l1_count": int(summary.get("blocked_forced_exit_l1_count", 0)),
				"n_fallback_flatten_after_1445": int(summary.get("n_fallback_flatten_after_1445", 0)),
				"n_fallback_long_after_1445": int(summary.get("n_fallback_long_after_1445", 0)),
				"n_fallback_short_after_1445": int(summary.get("n_fallback_short_after_1445", 0)),
				"n_unflattenable_after_1445": int(summary.get("n_unflattenable_after_1445", 0)),
				"n_unflattenable_long_after_1445": int(summary.get("n_unflattenable_long_after_1445", 0)),
				"n_unflattenable_short_after_1445": int(summary.get("n_unflattenable_short_after_1445", 0)),
				"has_unflattenable_after_1445": int(summary.get("has_unflattenable_after_1445", 0)),
				"shares_unflattenable_after_1445": int(summary.get("shares_unflattenable_after_1445", 0)),
				"aum": float(symbol_aum) if symbol_aum is not None else None,
				"used_long": float(summary.get("used_long", 0.0)),
				"used_short": float(summary.get("used_short", 0.0)),
				"blocked_aum_count": int(summary.get("blocked_aum_count", 0)),
			}
		)

	all_trades_df = (
		pl.concat(all_trade_frames, how="diagonal_relaxed")
		if all_trade_frames
		else pl.DataFrame(
			schema={
				"symbol": pl.Utf8,
				"side": pl.Utf8,
				"entry_time": pl.Utf8,
				"exit_time": pl.Utf8,
				"entry_px": pl.Float64,
				"exit_px": pl.Float64,
				"shares": pl.Int64,
				"entry_notional": pl.Float64,
				"exit_notional": pl.Float64,
				"pnl_gross": pl.Float64,
				"commission": pl.Float64,
				"stamp_duty": pl.Float64,
				"total_cost": pl.Float64,
				"pnl": pl.Float64,
				"ret": pl.Float64,
				"alpha_signal": pl.Float64,
			}
		)
	)
	summary_df = (
		pl.DataFrame(summary_rows, infer_schema_length=None)
		if summary_rows
		else pl.DataFrame(
			{
				"symbol": [],
				"market": [],
				"alpha_points": [],
				"n_trades": [],
				"n_long": [],
				"n_short": [],
				"win_rate": [],
				"total_pnl": [],
				"avg_pnl": [],
				"avg_ret": [],
				"sharpe_ratio": [],
				"blocked_entry_l1_count": [],
				"blocked_timed_exit_l1_count": [],
				"blocked_forced_exit_l1_count": [],
				"n_fallback_flatten_after_1445": [],
				"n_fallback_long_after_1445": [],
				"n_fallback_short_after_1445": [],
				"n_unflattenable_after_1445": [],
				"n_unflattenable_long_after_1445": [],
				"n_unflattenable_short_after_1445": [],
				"has_unflattenable_after_1445": [],
				"shares_unflattenable_after_1445": [],
			"aum": [],
			"used_long": [],
			"used_short": [],
			"blocked_aum_count": [],
			}
		)
	)
	failures_df = pl.DataFrame(failure_rows or {"symbol": [], "reason": []})

	summary_df.sort("total_pnl", descending=True).write_csv(strategy_dir / "symbol_summary.csv")
	summary_df.filter(pl.col("has_unflattenable_after_1445") > 0).sort(
		["n_fallback_flatten_after_1445", "total_pnl"],
		descending=[True, False],
	).write_csv(strategy_dir / "unflattenable_after_1445_symbols.csv")
	all_trades_df.write_csv(strategy_dir / "all_trades.csv")
	failures_df.write_csv(strategy_dir / "strategy_failures.csv")

	return StrategyRunResult(
		trade_date=bundle.trade_date,
		repo_dir=bundle.repo_dir,
		hold_minutes=config.hold_minutes,
		summary_df=summary_df,
		all_trades_df=all_trades_df,
		failures_df=failures_df,
	)
