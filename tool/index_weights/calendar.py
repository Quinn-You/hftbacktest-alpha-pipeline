"""指数权重表交易日与成分解析（无 matplotlib，供 index_weights.allocate / index_filter 共用）。"""

from __future__ import annotations

import bisect
from datetime import date, datetime
from pathlib import Path

import polars as pl


def parse_trade_date(trade_date: str) -> date:
	return datetime.strptime(trade_date, "%Y-%m-%d").date()


def trading_days_sorted(weights_path: Path) -> list[date]:
	"""从权重 parquet 中出现过的 ``date`` 去重升序，视为该表定义的交易日序列。"""
	df = (
		pl.scan_parquet(weights_path)
		.select("date")
		.unique()
		.sort("date")
		.collect()
	)
	if df.height == 0:
		return []
	raw = df.get_column("date").to_list()
	out: list[date] = []
	for x in raw:
		if isinstance(x, date):
			out.append(x)
		else:
			out.append(datetime.strptime(str(x)[:10], "%Y-%m-%d").date())
	return sorted(set(out))


def next_trading_day_after(t: date, trading_days: list[date]) -> date | None:
	"""``trading_days`` 已排序；返回第一个严格大于 ``t`` 的日期。"""
	if not trading_days:
		return None
	#在已排序的列里面做二分查找，返回索引
	i = bisect.bisect_right(trading_days, t)
	if i < len(trading_days):
		return trading_days[i]
	return None


def load_constituents_for_date(
	weights_path: Path,
	trade_date: str,
	trading_days: list[date],
) -> tuple[pl.DataFrame, int, str | None, str]:
	"""取交易日 T 的成分：**权重表中 ``date == T`` 的下一交易日**（由表里出现过的 date 序列定义）。"""
	d = parse_trade_date(trade_date)
	lookup = next_trading_day_after(d, trading_days)
	if lookup is None:
		return pl.DataFrame(), 0, None, "(表中无 T 之后交易日)"
	lookup_s = lookup.strftime("%Y-%m-%d")
	df = (
		pl.scan_parquet(weights_path)
		.filter(pl.col("date") == pl.lit(lookup, dtype=pl.Date))
		.collect()
	)
	if df.height == 0:
		return df, 0, None, lookup_s
	n_unique = int(df.select(pl.col("stock_code").n_unique()).item())
	code_val = df.get_column("code")[0] if "code" in df.columns else None
	return df, n_unique, str(code_val) if code_val is not None else None, lookup_s
