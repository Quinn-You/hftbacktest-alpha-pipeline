#!/usr/bin/env python3
"""独立入口：按 main.py 风格批量运行 gridtrading。"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Iterable

import polars as pl

from data import default_repo_dir
from data import normalize_requested_symbols
from strategy.backtest import load_alpha_records_grouped
from strategy.backtest import resolve_alpha_path
from strategy.gridtrading.engine import grid_points_to_dicts
from strategy.gridtrading.engine import run_gridtrading_backtest


DEFAULT_EVENTSTREAM_ROOT = Path("/data/users/haoranyou/shared_eventstream")
FALLBACK_EVENTSTREAM_ROOT = Path("/home/haoranyou/data/shared_eventstream")
DEFAULT_ALPHA_FACTOR = "mid_hivol_hiliq_mid_hivol_loliq"


def _resolve_market(symbol: str) -> str | None:
	if symbol.startswith(("00", "001", "002", "003", "200", "300", "301")):
		return "SZSE"
	if symbol.startswith(("60", "68", "90")):
		return "SSE"
	return None


def _select_symbols(alpha_symbols: Iterable[str], requested_symbols: list[str] | None) -> list[str]:
	available = sorted({str(item) for item in alpha_symbols})
	if len(available) == 0:
		raise ValueError("当日 alpha 文件中没有任何 symbol")
	if requested_symbols is None:
		return available
	req = sorted({str(item) for item in requested_symbols})
	missing = [item for item in req if item not in available]
	if missing:
		raise ValueError(
			f"请求的 symbol 不在当日 alpha 文件中: {', '.join(missing[:20])}。可选示例: {', '.join(available[:20])}"
		)
	return req


def _resolve_eventstream_path(
	trade_date: str,
	symbol: str,
	eventstream_path: Path | None,
	eventstream_root: Path | None,
	market: str | None,
) -> Path:
	if eventstream_path is not None:
		if not eventstream_path.is_file():
			raise FileNotFoundError(f"--eventstream-path 文件不存在: {eventstream_path}")
		return eventstream_path

	candidates: list[Path] = []
	roots: list[Path] = []
	if eventstream_root is not None:
		roots.append(eventstream_root)
	if DEFAULT_EVENTSTREAM_ROOT not in roots:
		roots.append(DEFAULT_EVENTSTREAM_ROOT)
	if FALLBACK_EVENTSTREAM_ROOT not in roots:
		roots.append(FALLBACK_EVENTSTREAM_ROOT)

	markets = [market] if market in {"SZSE", "SSE"} else ["SZSE", "SSE"]
	for root in roots:
		for mkt in markets:
			candidates.append(root / trade_date / mkt.lower() / f"{symbol}_{trade_date}.npz")

	for path in candidates:
		if path.is_file():
			return path

	show = ", ".join(str(item) for item in candidates[:6])
	raise FileNotFoundError(
		f"未找到 {symbol} {trade_date} 的 eventstream。尝试路径示例: {show}。可用 --eventstream-path 显式指定。"
	)


def _build_parser() -> argparse.ArgumentParser:
	p = argparse.ArgumentParser(
		description="完整 gridtrading pipeline：读取 alpha、批量回测、输出汇总",
		formatter_class=argparse.ArgumentDefaultsHelpFormatter,
	)
	p.add_argument("--date", type=str, required=True, help="交易日 YYYY-MM-DD")
	p.add_argument("--run-name", type=str, required=True, help="输出目录 run 名")
	p.add_argument("--symbol", type=str, default=None, help="单标的模式：股票代码，如 000858")
	p.add_argument("--symbols", type=str, default=None, help="多标的模式：逗号分隔股票代码；不传则跑当日 alpha 全部股票")
	p.add_argument("--max-symbols", type=int, default=None, help="可选，仅运行前 N 只股票")
	p.add_argument(
		"--market",
		type=str,
		choices=["SZSE", "SSE"],
		default=None,
		help="可选，交易所；单标的下可手动指定，不传则按代码前缀自动识别",
	)
	p.add_argument("--eventstream-path", type=Path, default=None, help="可选，显式指定 eventstream npz 路径（仅单标的）")
	p.add_argument(
		"--eventstream-root",
		type=Path,
		default=DEFAULT_EVENTSTREAM_ROOT,
		help="eventstream 根目录（默认 /data/users/haoranyou/shared_eventstream）",
	)
	p.add_argument(
		"--alpha",
		type=Path,
		default=Path("/data/sihang/AlphaPROBETick/alpha_monthly/mid_hivol_hiliq_mid_hivol_loliq"),
		help="alpha parquet 路径或 alpha_monthly 根目录",
	)
	p.add_argument(
		"--alpha-factor",
		type=str,
		default=DEFAULT_ALPHA_FACTOR,
		help="当 --alpha 指向 alpha_monthly 根目录时，优先使用的子目录名",
	)
	p.add_argument("--repo-dir", type=Path, default=None, help="输出 repo 目录；默认 /home/haoranyou/data/output/{run_name}/{date}")
	p.add_argument("--desired-inventory-shares", type=int, default=None, help="目标底仓股数（整手）")
	p.add_argument("--target-notional", type=float, default=1_000_000.0, help="若未给目标股数，用名义金额换算目标底仓")
	p.add_argument("--grid-num", type=int, default=8, help="单边网格层数")
	p.add_argument("--order-lots", type=int, default=2, help="每笔下单手数")
	p.add_argument("--inventory-span-lots", type=int, default=40, help="围绕目标底仓的对称带宽（手）")
	p.add_argument("--half-spread-ticks", type=int, default=0, help="半边价差（tick）")
	p.add_argument("--grid-step-ticks", type=int, default=1, help="网格层间距（tick）")
	p.add_argument("--skew1", type=float, default=1.0, help="仓位偏离项系数")
	p.add_argument("--skew2", type=float, default=1.0, help="alpha 项系数")
	p.add_argument("--step-ns", type=int, default=5_000_000_000, help="策略步长（单位为纳秒）")
	p.add_argument("--recorder-interval-ns", type=int, default=1_000_000_000, help="记录间隔（纳秒）")
	p.add_argument("--order-latency-ns", type=int, default=2_000_000_000, help="订单单边延迟（纳秒）")
	p.add_argument("--cutoff-buffer-ns", type=int, default=None, help="阶段切换撤单缓冲（纳秒）；不传则 2*order_latency")
	p.add_argument("--lot-size", type=int, default=100, help="每手股数")
	p.add_argument("--tick-size", type=float, default=0.01, help="最小价格单位")
	p.add_argument("--roi-lb", type=float, default=0.01, help="hbt ROI 下界")
	p.add_argument("--roi-ub", type=float, default=2000.0, help="hbt ROI 上界")
	p.add_argument("--force-flatten-hhmmss", type=str, default="14:45:00", help="强平开始时间")
	p.add_argument("--force-flatten-end-hhmmss", type=str, default="14:57:00", help="强平结束时间")
	p.add_argument("--force-flatten-extra-ticks", type=int, default=10, help="强平激进额外 tick")
	p.add_argument("--daily-aum", type=float, default=None, help="单标的 AUM 上限（多空分别记账）")
	p.add_argument("--trade-unit-clip-frac", type=float, default=None, help="单笔最多使用剩余预算比例 (0,1]")
	p.add_argument("--alpha-default-to-zero", action="store_true", help="alpha 未到首点之前按 0 处理")
	return p


def _output_dir(repo_dir: Path) -> Path:
	out_dir = repo_dir / "strategy_layer" / "gridtrading"
	out_dir.mkdir(parents=True, exist_ok=True)
	return out_dir


def _resolve_alpha_file(args: argparse.Namespace) -> Path:
	alpha_input = args.alpha
	if alpha_input.is_file():
		return alpha_input
	target_name = f"{args.date}.parquet"
	# 目录模式优先按“精确文件”定位；resolve_alpha_path 亦只搜传入目录树。
	if alpha_input.is_dir():
		direct = alpha_input / target_name
		if direct.is_file():
			return direct
		factor_direct = alpha_input / str(args.alpha_factor) / target_name
		if factor_direct.is_file():
			return factor_direct
	return resolve_alpha_path(alpha_input, args.date)


def _write_run_config(args: argparse.Namespace, repo_dir: Path, alpha_file: Path) -> None:
	config_path = repo_dir / "run_config.json"
	config = {
		"date": args.date,
		"run_name": args.run_name,
		"alpha": str(args.alpha),
		"alpha_file": str(alpha_file),
		"alpha_factor": args.alpha_factor,
		"symbol": args.symbol,
		"symbols": args.symbols,
		"max_symbols": args.max_symbols,
		"market": args.market,
		"eventstream_path": str(args.eventstream_path) if args.eventstream_path else None,
		"eventstream_root": str(args.eventstream_root) if args.eventstream_root else None,
		"desired_inventory_shares": args.desired_inventory_shares,
		"target_notional": args.target_notional,
		"grid_num": args.grid_num,
		"order_lots": args.order_lots,
		"inventory_span_lots": args.inventory_span_lots,
		"half_spread_ticks": args.half_spread_ticks,
		"grid_step_ticks": args.grid_step_ticks,
		"skew1": args.skew1,
		"skew2": args.skew2,
		"step_ns": args.step_ns,
		"recorder_interval_ns": args.recorder_interval_ns,
		"order_latency_ns": args.order_latency_ns,
		"cutoff_buffer_ns": args.cutoff_buffer_ns,
		"lot_size": args.lot_size,
		"tick_size": args.tick_size,
		"roi_lb": args.roi_lb,
		"roi_ub": args.roi_ub,
		"force_flatten_hhmmss": args.force_flatten_hhmmss,
		"force_flatten_end_hhmmss": args.force_flatten_end_hhmmss,
		"force_flatten_extra_ticks": args.force_flatten_extra_ticks,
		"daily_aum": args.daily_aum,
		"trade_unit_clip_frac": args.trade_unit_clip_frac,
		"alpha_default_to_zero": args.alpha_default_to_zero,
	}
	config_path.write_text(json.dumps(config, ensure_ascii=False, indent=2), encoding="utf-8")


def run_once(args: argparse.Namespace) -> None:
	if args.symbol and args.symbols:
		raise ValueError("--symbol 与 --symbols 不能同时传")

	repo_dir = args.repo_dir or default_repo_dir(args.date, args.run_name)
	repo_dir.mkdir(parents=True, exist_ok=True)
	out_dir = _output_dir(repo_dir)

	alpha_file = _resolve_alpha_file(args)
	_write_run_config(args, repo_dir, alpha_file)
	requested_symbols = normalize_requested_symbols(args.symbols)
	if args.symbol:
		requested_symbols = [args.symbol]
	alpha_map = load_alpha_records_grouped(alpha_file, args.date, requested_symbols)
	selected_symbols = _select_symbols(alpha_map.keys(), requested_symbols)
	if args.max_symbols is not None:
		selected_symbols = selected_symbols[: max(int(args.max_symbols), 0)]
	if len(selected_symbols) == 0:
		raise ValueError("没有可运行的 symbol（请检查 --symbols/--max-symbols）")
	if args.eventstream_path is not None and len(selected_symbols) != 1:
		raise ValueError("--eventstream-path 仅支持单标的，请改用 --eventstream-root + --symbols")

	summary_rows: list[dict[str, object]] = []
	failure_rows: list[dict[str, str]] = []

	for symbol in selected_symbols:
		alpha_records = alpha_map.get(symbol, [])
		market = args.market if len(selected_symbols) == 1 else None
		market = market or _resolve_market(symbol)
		try:
			eventstream_path = _resolve_eventstream_path(
				trade_date=args.date,
				symbol=symbol,
				eventstream_path=args.eventstream_path,
				eventstream_root=args.eventstream_root,
				market=market,
			)
			points, summary = run_gridtrading_backtest(
				eventstream_path=eventstream_path,
				alpha_records=alpha_records,
				trade_date=args.date,
				desired_inventory_shares=args.desired_inventory_shares,
				target_notional=args.target_notional,
				grid_num=args.grid_num,
				order_lots=args.order_lots,
				inventory_span_lots=args.inventory_span_lots,
				half_spread_ticks=args.half_spread_ticks,
				grid_step_ticks=args.grid_step_ticks,
				skew1=args.skew1,
				skew2=args.skew2,
				step_ns=args.step_ns,
				recorder_interval_ns=args.recorder_interval_ns,
				order_latency_ns=args.order_latency_ns,
				cutoff_buffer_ns=args.cutoff_buffer_ns,
				lot_size=args.lot_size,
				tick_size=args.tick_size,
				roi_lb=args.roi_lb,
				roi_ub=args.roi_ub,
				force_flatten_hhmmss=args.force_flatten_hhmmss,
				force_flatten_end_hhmmss=args.force_flatten_end_hhmmss,
				force_flatten_extra_ticks=args.force_flatten_extra_ticks,
				aum=args.daily_aum,
				trade_unit_clip_frac=args.trade_unit_clip_frac,
				alpha_default_to_zero=args.alpha_default_to_zero,
			)
		except Exception as exc:
			failure_rows.append({"symbol": symbol, "reason": str(exc)})
			continue

		points_df = pl.DataFrame(grid_points_to_dicts(points)) if points else pl.DataFrame()
		points_path = out_dir / f"{symbol}_{args.date}_grid_points.csv"
		points_df.write_csv(points_path)

		summary_with_meta = {
			"date": args.date,
			"symbol": symbol,
			"market": market,
			"eventstream_path": str(eventstream_path),
			"alpha_file": str(alpha_file),
			"alpha_points": int(len(alpha_records)),
			**summary,
		}
		summary_path = out_dir / f"{symbol}_{args.date}_grid_summary.json"
		summary_path.write_text(json.dumps(summary_with_meta, ensure_ascii=False, indent=2), encoding="utf-8")
		summary_rows.append(summary_with_meta)

	summary_df = pl.DataFrame(summary_rows, infer_schema_length=None) if summary_rows else pl.DataFrame(
		{"symbol": [], "market": [], "alpha_points": [], "final_equity": []}
	)
	if summary_df.height > 0 and "final_equity" in summary_df.columns:
		summary_df = summary_df.sort("final_equity", descending=True)
	summary_df.write_csv(out_dir / "symbol_summary.csv")
	failures_df = pl.DataFrame(failure_rows or {"symbol": [], "reason": []})
	failures_df.write_csv(out_dir / "failures.csv")

	print("=" * 72)
	print("gridtrading 回测完成")
	print(f"date              : {args.date}")
	print(f"run_name          : {args.run_name}")
	print(f"repo_dir          : {repo_dir}")
	print(f"alpha_file        : {alpha_file}")
	print(f"n_symbols_total   : {len(selected_symbols)}")
	print(f"n_symbols_done    : {summary_df.height}")
	print(f"n_symbols_failed  : {failures_df.height}")
	if summary_df.height > 0 and "symbol" in summary_df.columns:
		best3 = ", ".join(summary_df.get_column("symbol").head(3).to_list())
		print(f"best_by_equity    : {best3}")
	print(f"output_dir        : {out_dir}")
	print(f"summary_csv       : {out_dir / 'symbol_summary.csv'}")
	print(f"failures_csv      : {out_dir / 'failures.csv'}")
	print("=" * 72)


def main() -> None:
	run_once(_build_parser().parse_args())


if __name__ == "__main__":
	main()
