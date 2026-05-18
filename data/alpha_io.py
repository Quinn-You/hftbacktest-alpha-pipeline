"""Alpha Parquet 定位与按股票分组读取alpha数据（仅 data 层使用，与 strategy 无依赖）。"""

from __future__ import annotations

from collections import defaultdict
from datetime import datetime
from datetime import timezone
from pathlib import Path
from typing import Iterable

import polars as pl


def to_ns_utc(date_str: str, hhmmss: str) -> int:
	"""把 YYYY-MM-DD + HH:MM:SS 转成 UTC 纳秒时间戳。"""
	dt = datetime.strptime(f"{date_str} {hhmmss}", "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
	return int(dt.timestamp() * 1_000_000_000)


def resolve_alpha_path(alpha_path: Path, trade_date: str) -> Path:
	"""解析当日 alpha parquet。

	- 传具体 ``.parquet`` 文件：原样返回（须存在且为文件）。
	- 传目录：只在该目录树下 ``rglob`` 当日文件名，**不会**自动抬到 ``alpha_monthly`` 全盘搜索（多命中则报错）。
	"""
	if alpha_path.is_file():
		return alpha_path

	target_name = f"{trade_date}.parquet"
	search_root = alpha_path.parent if alpha_path.suffix == ".parquet" else alpha_path
	if not search_root.exists():
		raise FileNotFoundError(f"未找到 alpha 路径：{alpha_path}")

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
