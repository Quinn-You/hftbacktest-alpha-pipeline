"""按指数权重将 ``daily_aum`` 分摊到各 ``stock_code``，与 index_filter 权重表一致。"""

from __future__ import annotations

from pathlib import Path

import polars as pl

from .calendar import load_constituents_for_date
from .calendar import trading_days_sorted


def alpha_symbol_to_stock_code(symbol: str) -> str:
	"""与 ``index_filter`` 中 ``stock_code`` 对齐（*.XSHG / *.XSHE）。"""
	if symbol.endswith(".XSHG") or symbol.endswith(".XSHE"):
		return symbol
	if "." in symbol:
		root, suf = symbol.split(".", 1)
		if len(root) == 6 and root.isdigit():
			if suf.upper() in ("SH", "XSHG"):
				return root + ".XSHG"
			return root + ".XSHE"
		return symbol
	if len(symbol) == 6 and symbol.isdigit():
		return symbol + ".XSHG" if symbol.startswith("6") else symbol + ".XSHE"
	return symbol


def allocate_daily_aum_by_weights(weights_path: Path, trade_date: str, daily_aum: float) -> pl.DataFrame:
	"""返回列 ``stock_code``, ``weight_norm``, ``aum``（``aum = daily_aum * weight_norm``）。"""
	if daily_aum <= 0:
		raise ValueError("daily_aum 必须 > 0")
	if not weights_path.is_file():
		raise FileNotFoundError(weights_path)
	days = trading_days_sorted(weights_path)
	constituents, _, _, _ = load_constituents_for_date(weights_path, trade_date, days)
	if constituents.height == 0:
		return pl.DataFrame(
			{"stock_code": pl.Series([], dtype=pl.Utf8), "weight_norm": pl.Series([], dtype=pl.Float64), "aum": pl.Series([], dtype=pl.Float64)}
		)
	w = constituents.get_column("weight").cast(pl.Float64)
	wsum = float(w.sum())
	if not (wsum > 0) or not (wsum == wsum):
		n = int(constituents.height)
		wn = pl.lit(1.0 / n) if n > 0 else pl.lit(0.0)
		out = constituents.select("stock_code").with_columns(
			wn.alias("weight_norm"),
			(pl.lit(float(daily_aum)) * wn).alias("aum"),
		)
		return out
	weight_norm = w / pl.lit(wsum)
	out = constituents.select("stock_code").with_columns(
		weight_norm.alias("weight_norm"),
		(pl.lit(float(daily_aum)) * weight_norm).alias("aum"),
	)
	return out


def build_aum_by_symbol(bundle_symbols: list[str], alloc_df: pl.DataFrame) -> dict[str, float]:
	"""``bundle_symbols`` 为 alpha / prepared 侧代码；不在权重表中的标的 ``aum`` 为 0。"""
	lookup: dict[str, float] = {}
	for row in alloc_df.iter_rows(named=True):
		lookup[str(row["stock_code"])] = float(row["aum"])
	out: dict[str, float] = {}
	for sym in bundle_symbols:
		sc = alpha_symbol_to_stock_code(sym)
		out[sym] = float(lookup.get(sc, 0.0))
	return out
