#!/usr/bin/env python3
"""总入口：串联 data → strategy → analysis，可选调用 tool 指数成分过滤。

各子包（data / strategy / analysis / tool）互不 import；本文件负责类型转换与编排。
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from analysis import AnalysisReportInput
from analysis import generate_report
from data import PreparedUniverseBundle
from data import default_repo_dir
from data import normalize_requested_symbols
from data import prepare_universe_data
from strategy import PreparedSymbolInput
from strategy import StrategyConfig
from strategy import StrategyRunResult
from strategy import StrategyUniverseInput
from strategy import run_universe_strategy


def prepared_bundle_to_strategy_input(bundle: PreparedUniverseBundle) -> StrategyUniverseInput:
	return StrategyUniverseInput(
		trade_date=bundle.trade_date,
		repo_dir=bundle.repo_dir,
		prepared_symbols=[
			PreparedSymbolInput(
				symbol=p.symbol,
				market=p.market,
				eventstream_path=p.eventstream_path,
				alpha_points=p.alpha_points,
				status=p.status,
				message=p.message,
			)
			for p in bundle.prepared_symbols
		],
		alpha_records_by_symbol=bundle.alpha_records_by_symbol,
	)


def strategy_result_to_analysis_input(result: StrategyRunResult) -> AnalysisReportInput:
	return AnalysisReportInput(
		trade_date=result.trade_date,
		repo_dir=result.repo_dir,
		hold_minutes=result.hold_minutes,
		summary_df=result.summary_df,
		all_trades_df=result.all_trades_df,
		failures_df=result.failures_df,
	)


def _build_parser(default_strategy_mode: str = "legacy") -> argparse.ArgumentParser:
	p = argparse.ArgumentParser(
		description="完整 pipeline：准备数据、运行策略、输出报表；可选指数过滤",
		formatter_class=argparse.ArgumentDefaultsHelpFormatter,
	)
	p.add_argument("--date", type=str, required=True, help="交易日 YYYY-MM-DD")
	p.add_argument(
		"--run-name",
		type=str,
		required=True,
		help="方案名称（必填）；输出目录为 /home/haoranyou/data/output/{run_name}/{date}",
	)
	p.add_argument(
		"--alpha",
		type=Path,
		default=Path("/data/sihang/AlphaPROBETick/alpha_monthly/mid_hivol_hiliq_mid_hivol_loliq"),
		help="alpha parquet 路径或 alpha_monthly 根目录",
	)
	p.add_argument("--threshold", type=float, default=0.4, help="alpha 开仓阈值")
	p.add_argument("--hold-min", type=int, default=5, help="固定持有分钟数")
	p.add_argument("--notional", type=float, default=100_000.0, help="每笔名义资金")
	p.add_argument(
		"--order-size-mode",
		type=str,
		choices=["notional", "lots"],
		default="notional",
		help="下单规模模式：notional=按资金，lots=按手数",
	)
	p.add_argument("--order-lots", type=int, default=1, help="按手数下单时，每笔下单手数（1手=lot-size股）")
	p.add_argument("--lot-size", type=int, default=100, help="最小交易数")
	p.add_argument("--aggressive-ticks", type=int, default=3, help="对手盘一价偏移 tick 数；买单=卖一+n*tick，卖单=买一-n*tick")
	p.add_argument("--step-ns", type=int, default=5_000_000_000, help="引擎时间推进步长（纳秒）")
	p.add_argument("--tick-size", type=float, default=0.01, help="最小价格变动")
	p.add_argument("--order-latency-ns", type=int, default=2_000_000_000, help="单边下单延迟（纳秒）")
	p.add_argument("--roi-lb", type=float, default=0.1, help="hbt 价格回放下界")
	p.add_argument("--roi-ub", type=float, default=2000.0, help="hbt 价格回放上界")
	p.add_argument("--commission-rate", type=float, default=0.00015, help="双边手续费率")
	p.add_argument("--stamp-duty-rate", type=float, default=0.0005, help="印花税率（仅卖出）")
	p.add_argument("--force-flatten-hhmmss", type=str, default="14:45:00", help="固定日末强平触发时间 HH:MM:SS")
	p.add_argument("--force-flatten-extra-ticks", type=int, default=5, help="日末兜底强平价格相对市价参考的额外 tick")
	p.add_argument("--symbols", type=str, default=None, help="可选，逗号分隔的股票代码")
	p.add_argument("--max-symbols", type=int, default=None, help="可选，仅运行前 N 只股票")
	p.add_argument("--repo-dir", type=Path, default=None, help="输出 repo 目录")
	p.add_argument("--force-regenerate", action="store_true", help="强制重建 event stream")
	p.add_argument(
		"--index-weights",
		type=Path,
		default=None,
		help="可选：指数权重 parquet；若提供则在报表后执行 tool.index_filter",
	)
	p.add_argument(
		"--index-filter-output",
		type=Path,
		default=Path("/home/haoranyou/data/index_filter"),
		help="指数过滤输出根目录（与 --index-weights 联用）",
	)
	p.add_argument(
		"--index-label",
		type=str,
		default=None,
		help="指数过滤子目录名；默认取权重文件名 stem",
	)
	p.add_argument(
		"--strategy-mode",
		type=str,
		choices=["legacy", "dynamic_hold"],
		default=default_strategy_mode,
		help="策略内核：legacy=固定持仓；dynamic_hold=阈值开仓+阶梯调仓+止盈止损到期反向（反向须达阈值）",
	)
	p.add_argument("--alpha-adjust-step", type=float, default=0.01, help="dynamic：相对 α₀ 的调仓步长")
	p.add_argument("--adjust-notional", type=float, default=10_000.0, help="dynamic：每步加减仓对应名义金")
	p.add_argument("--take-profit-pct", type=float, default=None, help="dynamic：止盈相对均价比例，如 0.02；不设则关闭")
	p.add_argument("--stop-loss-pct", type=float, default=None, help="dynamic：止损相对均价比例，如 0.01；不设则关闭")
	p.add_argument(
		"--daily-aum",
		type=float,
		default=None,
		help="可选：当日全市场预算（>0 时按指数权重分摊到各标的 aum；引擎内 used_long/used_short 仅开仓累计，平仓不计）",
	)
	p.add_argument(
		"--trade-unit-clip-frac",
		type=float,
		default=None,
		help="可选：单笔名义 additionally 不超过剩余预算×该比例（0~1）",
	)
	p.add_argument(
		"--twap-slice-notional",
		type=float,
		default=None,
		help="dynamic_hold：TWAP 每步名义切片（>0 启用）；与 --step-ns 对齐，默认 step-ns=5s 即每 5s 至多一片；仅 notional 模式",
	)
	return p


def _write_run_config(
	args: argparse.Namespace,
	repo_dir: Path,
	*,
	aum_weights_resolved: str | None = None,
) -> None:
	config_path = repo_dir / "run_config.json"
	aum_wp = aum_weights_resolved
	if aum_wp is None and args.index_weights is not None:
		aum_wp = str(args.index_weights.resolve())
	config = {
		"date": args.date,
		"run_name": args.run_name,
		"alpha": str(args.alpha),
		"threshold": args.threshold,
		"hold_min": args.hold_min,
		"notional": args.notional,
		"order_size_mode": args.order_size_mode,
		"order_lots": args.order_lots,
		"lot_size": args.lot_size,
		"aggressive_ticks": args.aggressive_ticks,
		"step_ns": args.step_ns,
		"tick_size": args.tick_size,
		"order_latency_ns": args.order_latency_ns,
		"roi_lb": args.roi_lb,
		"roi_ub": args.roi_ub,
		"commission_rate": args.commission_rate,
		"stamp_duty_rate": args.stamp_duty_rate,
		"force_flatten_hhmmss": args.force_flatten_hhmmss,
		"force_flatten_extra_ticks": args.force_flatten_extra_ticks,
		"symbols": args.symbols,
		"max_symbols": args.max_symbols,
		"force_regenerate": args.force_regenerate,
		"index_weights": str(args.index_weights) if args.index_weights else None,
		"index_filter_output": str(args.index_filter_output),
		"index_label": args.index_label,
		"strategy_mode": args.strategy_mode,
		"alpha_adjust_step": args.alpha_adjust_step,
		"adjust_notional": args.adjust_notional,
		"take_profit_pct": args.take_profit_pct,
		"stop_loss_pct": args.stop_loss_pct,
		"daily_aum": args.daily_aum,
		"trade_unit_clip_frac": args.trade_unit_clip_frac,
		"twap_slice_notional": args.twap_slice_notional,
		"aum_weights_path": aum_wp,
	}
	config_path.write_text(json.dumps(config, ensure_ascii=False, indent=2), encoding="utf-8")


def run_pipeline(args: argparse.Namespace) -> None:
	repo_dir = args.repo_dir or default_repo_dir(args.date, args.run_name)
	repo_dir.mkdir(parents=True, exist_ok=True)

	bundle = prepare_universe_data(
		trade_date=args.date,
		run_name=args.run_name,
		alpha_path=args.alpha,
		repo_dir=repo_dir,
		requested_symbols=normalize_requested_symbols(args.symbols),
		max_symbols=args.max_symbols,
		force_regenerate=args.force_regenerate,
	)

	aum_by_symbol: dict[str, float] | None = None
	aum_weights_resolved: str | None = None
	if args.daily_aum is not None and args.daily_aum > 0:
		from tool.index_weights import allocate_daily_aum_by_weights
		from tool.index_weights import build_aum_by_symbol

		default_weights = Path("/data/index_weights_300.parquet")
		weights_path = args.index_weights if args.index_weights is not None else default_weights
		aum_weights_resolved = str(weights_path.resolve())
		if not weights_path.is_file():
			raise FileNotFoundError(f"AUM 需要指数权重 parquet 文件: {weights_path}")
		alloc_df = allocate_daily_aum_by_weights(weights_path, args.date, float(args.daily_aum))
		syms = [p.symbol for p in bundle.prepared_symbols]
		aum_by_symbol = build_aum_by_symbol(syms, alloc_df)
		strat_dir = repo_dir / "strategy_layer"
		strat_dir.mkdir(parents=True, exist_ok=True)
		alloc_df.write_parquet(strat_dir / "aum_allocation.parquet")

	_write_run_config(args, repo_dir, aum_weights_resolved=aum_weights_resolved)

	strategy_input = prepared_bundle_to_strategy_input(bundle)
	strategy_result = run_universe_strategy(
		strategy_input,
		StrategyConfig(
			alpha_threshold=args.threshold,
			hold_minutes=args.hold_min,
			notional=args.notional,
			order_size_mode=args.order_size_mode,
			order_lots=args.order_lots,
			lot_size=args.lot_size,
			aggressive_ticks=args.aggressive_ticks,
			step_ns=args.step_ns,
			tick_size=args.tick_size,
			order_latency_ns=args.order_latency_ns,
			roi_lb=args.roi_lb,
			roi_ub=args.roi_ub,
			commission_rate=args.commission_rate,
			stamp_duty_rate=args.stamp_duty_rate,
			force_flatten_hhmmss=args.force_flatten_hhmmss,
			force_flatten_extra_ticks=args.force_flatten_extra_ticks,
			strategy_mode=args.strategy_mode,
			alpha_adjust_step=args.alpha_adjust_step,
			adjust_notional=args.adjust_notional,
			take_profit_pct=args.take_profit_pct,
			stop_loss_pct=args.stop_loss_pct,
			aum_by_symbol=aum_by_symbol,
			trade_unit_clip_frac=args.trade_unit_clip_frac,
			twap_slice_notional=args.twap_slice_notional,
		),
	)

	analysis_input = strategy_result_to_analysis_input(strategy_result)
	overall_summary = generate_report(analysis_input)

	if args.index_weights is not None:
		from tool.index_filter import run_index_filter_repo

		label = args.index_label or args.index_weights.stem
		index_out = run_index_filter_repo(
			repo_root=repo_dir.parent.resolve(),
			index_weights=args.index_weights.resolve(),
			output_root=args.index_filter_output.resolve(),
			index_label=label,
			start_date=args.date,
			end_date=args.date,
		)
		print(f"index_filter 输出目录: {index_out}")

	print("=" * 72)
	print("完整 pipeline 完成")
	print(f"date              : {args.date}")
	print(f"run_name          : {args.run_name}")
	print(f"repo_dir          : {repo_dir}")
	print(f"n_symbols_done    : {overall_summary['n_symbols_completed']}")
	print(f"n_symbols_failed  : {overall_summary['n_symbols_failed']}")
	print(f"n_trades          : {overall_summary['n_trades']}")
	print(f"total_pnl         : {overall_summary['total_pnl']:.8f}")
	print(f"sharpe_ratio      : {overall_summary['sharpe_ratio']}")
	print(f"best_by_pnl       : {', '.join(overall_summary['best_symbols_by_pnl'])}")
	print(f"worst_by_pnl      : {', '.join(overall_summary['worst_symbols_by_pnl'])}")
	print(f"best_by_sharpe    : {', '.join(overall_summary['best_symbols_by_sharpe'])}")
	print(f"worst_by_sharpe   : {', '.join(overall_summary['worst_symbols_by_sharpe'])}")
	print(f"symbol_pos_charts : {overall_summary['n_symbol_net_position_charts']}")
	print("=" * 72)


def main() -> None:
	run_pipeline(_build_parser().parse_args())


if __name__ == "__main__":
	main()
