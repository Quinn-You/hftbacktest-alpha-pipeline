#!/usr/bin/env python3
"""数据准备：生成 event stream，并整理当日 alpha 输入。"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
from pathlib import Path

import polars as pl

from .alpha_io import load_alpha_records_grouped
from .alpha_io import resolve_alpha_path
from .sse_eventstream import convert_sse_symbol
from .szse_eventstream import convert_szse_symbol


DEFAULT_OUTPUT_ROOT = Path("/home/haoranyou/data/output")
DEFAULT_SHARED_EVENTSTREAM_ROOT = Path("/home/haoranyou/data/shared_eventstream")


@dataclass
class PreparedSymbol:
	"""单只股票的数据准备结果。"""

	symbol: str
	market: str
	eventstream_path: Path
	alpha_points: int
	status: str
	message: str = ""


@dataclass
class PreparedUniverseBundle:
	"""策略执行所需的数据包（类型定义在 data；main 负责转成 strategy 侧输入）。"""

	trade_date: str
	run_name: str
	alpha_file: Path
	repo_dir: Path
	prepared_symbols: list[PreparedSymbol]
	alpha_records_by_symbol: dict[str, list[tuple[int, float]]]
	skipped_symbols: list[dict[str, str]]


def _build_parser() -> argparse.ArgumentParser:
	p = argparse.ArgumentParser(description="数据准备层：生成 event stream 并整理 alpha")
	p.add_argument("--date", type=str, default=None, help="交易日 YYYY-MM-DD(单日期模式)")
	p.add_argument(
		"--dates-file",
		type=Path,
		default=None,
		help="日期文件路径(多日期模式);文件中每行一个日期,格式 YYYY-MM-DD",
	)
	p.add_argument(
		"--alpha",
		type=Path,
		default=Path("/data/sihang/AlphaPROBETick/alpha_monthly/mid_hivol_hiliq_mid_hivol_loliq"),
		help="alpha parquet 路径或 alpha_monthly 根目录",
	)
	p.add_argument(
		"--symbols",
		type=str,
		default=None,
		help="可选，逗号分隔的股票代码；不传则使用当日 alpha 中全部股票",
	)
	p.add_argument(
		"--max-symbols",
		type=int,
		default=None,
		help="可选，仅处理前 N 只股票，便于调试",
	)
	p.add_argument(
		"--run-name",
		type=str,
		required=True,
		help="方案名称（必填）；输出目录为 /home/haoranyou/data/output/{run_name}/{date}",
	)
	p.add_argument(
		"--repo-dir",
		type=Path,
		default=None,
		help="输出 repo 目录；默认 /home/haoranyou/data/output/{run_name}/{date}",
	)
	p.add_argument(
		"--force-regenerate",
		action="store_true",
		help="若 event stream 已存在，是否强制重新生成",
	)
	return p


def default_repo_dir(trade_date: str, run_name: str) -> Path:
	return DEFAULT_OUTPUT_ROOT / run_name / trade_date


def normalize_requested_symbols(symbols: str | None) -> list[str] | None:
	if not symbols:
		return None
	items = [item.strip() for item in symbols.split(",") if item.strip()]
	return items or None


def _resolve_market(symbol: str) -> str | None:
	if symbol.startswith(("00", "001", "002", "003", "200", "300", "301")):
		return "SZSE"
	if symbol.startswith(("60", "68", "90")):
		return "SSE"
	return None


def _eventstream_out_dir(trade_date: str, market: str) -> Path:
	return DEFAULT_SHARED_EVENTSTREAM_ROOT / trade_date / market.lower()


def _manifest_dir(repo_dir: Path) -> Path:
	return repo_dir / "data_layer" / "manifest"


def _write_manifest_files(
	repo_dir: Path,
	prepared_symbols: list[PreparedSymbol],
	skipped_symbols: list[dict[str, str]],
) -> None:
	manifest_dir = _manifest_dir(repo_dir)
	manifest_dir.mkdir(parents=True, exist_ok=True)

	prepared_df = pl.DataFrame(
		[
			{
				"symbol": item.symbol,
				"market": item.market,
				"eventstream_path": str(item.eventstream_path),
				"alpha_points": item.alpha_points,
				"status": item.status,
				"message": item.message,
			}
			for item in prepared_symbols
		]
		or {
			"symbol": [],
			"market": [],
			"eventstream_path": [],
			"alpha_points": [],
			"status": [],
			"message": [],
		}
	)
	prepared_df.write_csv(manifest_dir / "prepared_symbols.csv")

	skipped_df = pl.DataFrame(skipped_symbols or {"symbol": [], "reason": []})
	skipped_df.write_csv(manifest_dir / "skipped_symbols.csv")


def prepare_universe_data(
	trade_date: str,
	run_name: str,
	alpha_path: Path,
	repo_dir: Path | None = None,
	requested_symbols: list[str] | None = None,
	max_symbols: int | None = None,
	force_regenerate: bool = False,
) -> PreparedUniverseBundle:
	repo_root = repo_dir or default_repo_dir(trade_date, run_name)
	repo_root.mkdir(parents=True, exist_ok=True)
	alpha_file = resolve_alpha_path(alpha_path, trade_date)
	alpha_records_by_symbol = load_alpha_records_grouped(alpha_file, trade_date, requested_symbols)
	symbols = sorted(alpha_records_by_symbol)
	if max_symbols is not None:
		symbols = symbols[:max_symbols]

	prepared_symbols: list[PreparedSymbol] = []
	skipped_symbols: list[dict[str, str]] = []

	for symbol in symbols:
		market = _resolve_market(symbol)
		if market is None:
			skipped_symbols.append({"symbol": symbol, "reason": "无法根据代码前缀识别交易所"})
			continue

		out_dir = _eventstream_out_dir(trade_date, market)
		out_dir.mkdir(parents=True, exist_ok=True)
		eventstream_path = out_dir / f"{symbol}_{trade_date}.npz"

		try:
			if force_regenerate or (not eventstream_path.exists()):
				if market == "SSE":
					convert_sse_symbol(symbol, trade_date, out_dir)
				else:
					convert_szse_symbol(symbol, trade_date, out_dir)
				status = "generated"
				message = ""
			else:
				status = "existing"
				message = "复用已有 event stream"
		except Exception as exc:
			skipped_symbols.append({"symbol": symbol, "reason": f"eventstream 生成失败: {exc}"})
			continue

		prepared_symbols.append(
			PreparedSymbol(
				symbol=symbol,
				market=market,
				eventstream_path=eventstream_path,
				alpha_points=len(alpha_records_by_symbol[symbol]),
				status=status,
				message=message,
			)
		)

	_write_manifest_files(repo_root, prepared_symbols, skipped_symbols)

	return PreparedUniverseBundle(
		trade_date=trade_date,
		run_name=run_name,
		alpha_file=alpha_file,
		repo_dir=repo_root,
		prepared_symbols=prepared_symbols,
		alpha_records_by_symbol={item.symbol: alpha_records_by_symbol[item.symbol] for item in prepared_symbols},
		skipped_symbols=skipped_symbols,
	)


def _load_dates_from_file(dates_file: Path) -> list[str]:
	if not dates_file.exists():
		raise FileNotFoundError(f"日期文件不存在: {dates_file}")

	dates = []
	with open(dates_file, "r", encoding="utf-8") as f:
		for line in f:
			line = line.strip()
			if line and not line.startswith("#"):
				dates.append(line)

	if not dates:
		raise ValueError(f"日期文件中没有有效的日期: {dates_file}")

	return dates


def main() -> None:
	args = _build_parser().parse_args()

	if args.date and args.dates_file:
		raise ValueError("不能同时指定 --date 和 --dates-file,请选择其中一种模式")
	if not args.date and not args.dates_file:
		raise ValueError("必须指定 --date 或 --dates-file 其中之一")

	if args.dates_file:
		trade_dates = _load_dates_from_file(args.dates_file)
		print(f"多日期模式:从文件 {args.dates_file} 读取到 {len(trade_dates)} 个日期")
	else:
		trade_dates = [args.date]

	success_count = 0
	failed_dates = []

	for trade_date in trade_dates:
		try:
			print(f"\n{'=' * 72}")
			print(f"处理日期: {trade_date}")
			print(f"{'=' * 72}")

			bundle = prepare_universe_data(
				trade_date=trade_date,
				run_name=args.run_name,
				alpha_path=args.alpha,
				repo_dir=args.repo_dir,
				requested_symbols=normalize_requested_symbols(args.symbols),
				max_symbols=args.max_symbols,
				force_regenerate=args.force_regenerate,
			)

			print("=" * 72)
			print("数据准备完成")
			print(f"date              : {bundle.trade_date}")
			print(f"run_name          : {bundle.run_name}")
			print(f"alpha_file        : {bundle.alpha_file}")
			print(f"repo_dir          : {bundle.repo_dir}")
			print(f"prepared_symbols  : {len(bundle.prepared_symbols)}")
			print(f"skipped_symbols   : {len(bundle.skipped_symbols)}")
			print("=" * 72)

			success_count += 1

		except Exception as exc:
			print(f"\n❌ 日期 {trade_date} 处理失败: {exc}")
			failed_dates.append((trade_date, str(exc)))

	print(f"\n{'=' * 72}")
	print("批量处理总结")
	print(f"{'=' * 72}")
	print(f"总日期数    : {len(trade_dates)}")
	print(f"成功        : {success_count}")
	print(f"失败        : {len(failed_dates)}")

	if failed_dates:
		print("\n失败的日期:")
		for date, error in failed_dates:
			print(f"  - {date}: {error}")

	print("=" * 72)


if __name__ == "__main__":
	main()
