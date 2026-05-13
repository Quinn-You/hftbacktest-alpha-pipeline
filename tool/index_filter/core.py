"""指数过滤、日度指标与 ``summary.txt`` 生成。"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

import matplotlib

matplotlib.use("Agg")

import matplotlib.pyplot as plt
import numpy as np
import polars as pl

from tool.index_weights import load_constituents_for_date
from tool.index_weights import trading_days_sorted

matplotlib.rcParams["font.family"] = "sans-serif"
matplotlib.rcParams["font.sans-serif"] = ["DejaVu Sans", "Arial"]
matplotlib.rcParams["axes.unicode_minus"] = False

DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def sanitize_index_label(label: str) -> str:
	"""目录名安全化：禁止路径分隔符，避免误嵌套或跳出 output-root。"""
	s = label.strip().replace("/", "_").replace("\\", "_")
	return s if s else "index"


@dataclass(frozen=True)
class DateRepo:
	trade_date: str
	date_dir: Path
	all_trades_path: Path


def discover_date_repos(repo_root: Path, start_date: str | None, end_date: str | None) -> list[DateRepo]:
	items: list[DateRepo] = []
	start = datetime.strptime(start_date, "%Y-%m-%d") if start_date else None
	end = datetime.strptime(end_date, "%Y-%m-%d") if end_date else None

	for child in sorted(repo_root.iterdir()):
		if not child.is_dir() or not DATE_RE.match(child.name):
			continue
		trade_dt = datetime.strptime(child.name, "%Y-%m-%d")
		if start and trade_dt < start:
			continue
		if end and trade_dt > end:
			continue
		all_trades = child / "strategy_layer" / "all_trades.csv"
		if all_trades.exists():
			items.append(DateRepo(trade_date=child.name, date_dir=child, all_trades_path=all_trades))
	return items


def trades_with_stock_code(trades: pl.DataFrame) -> pl.DataFrame:
	"""将 CSV 中的 6 位 ``symbol`` 映射为与指数表一致的 ``stock_code``（*.XSHG / *.XSHE）。"""
	return trades.with_columns(
		pl.when(pl.col("symbol").str.contains(r"\."))
		.then(pl.col("symbol"))
		.when(pl.col("symbol").str.len_chars() == 6)
		.then(
			pl.when(pl.col("symbol").str.starts_with("6"))
			.then(pl.col("symbol") + pl.lit(".XSHG"))
			.otherwise(pl.col("symbol") + pl.lit(".XSHE"))
		)
		.otherwise(pl.col("symbol"))
		.alias("stock_code")
	)


def filter_trades_by_constituents(trades: pl.DataFrame, constituents: pl.DataFrame) -> pl.DataFrame:
	if constituents.height == 0:
		return trades.head(0)
	keys = constituents.select("stock_code").unique()
	tagged = trades_with_stock_code(trades)
	return tagged.join(keys, on="stock_code", how="inner").drop("stock_code")


def daily_return_metrics(df: pl.DataFrame) -> dict[str, Any]:
	"""与 ``build_period_repo`` 一致：``sum(pnl)/sum(entry_notional)``，多空分边。"""
	if df.height == 0:
		return {
			"n_trades": 0,
			"total_pnl": 0.0,
			"long_pnl": 0.0,
			"short_pnl": 0.0,
			"total_entry_notional": 0.0,
			"long_entry_notional": 0.0,
			"short_entry_notional": 0.0,
			"total_return_rate": None,
			"long_return_rate": None,
			"short_return_rate": None,
			"n_traded_symbols": 0,
		}

	total_entry = float(df.get_column("entry_notional").cast(pl.Float64).sum())
	total_pnl = float(df.get_column("pnl").cast(pl.Float64).sum())
	long_df = df.filter(pl.col("side") == "long")
	short_df = df.filter(pl.col("side") == "short")
	long_entry = float(long_df.get_column("entry_notional").cast(pl.Float64).sum()) if long_df.height > 0 else 0.0
	short_entry = float(short_df.get_column("entry_notional").cast(pl.Float64).sum()) if short_df.height > 0 else 0.0
	long_pnl = float(long_df.get_column("pnl").cast(pl.Float64).sum()) if long_df.height > 0 else 0.0
	short_pnl = float(short_df.get_column("pnl").cast(pl.Float64).sum()) if short_df.height > 0 else 0.0
	n_syms = int(df.select(pl.col("symbol").n_unique()).item())

	return {
		"n_trades": int(df.height),
		"total_pnl": total_pnl,
		"long_pnl": long_pnl,
		"short_pnl": short_pnl,
		"total_entry_notional": total_entry,
		"long_entry_notional": long_entry,
		"short_entry_notional": short_entry,
		"total_return_rate": (total_pnl / total_entry) if total_entry > 0 else None,
		"long_return_rate": (long_pnl / long_entry) if long_entry > 0 else None,
		"short_return_rate": (short_pnl / short_entry) if short_entry > 0 else None,
		"n_traded_symbols": n_syms,
	}


def plot_intraday_cumulative_pnl(filtered: pl.DataFrame, out_path: Path) -> None:
	"""按 ``exit_time`` 排序，对 net ``pnl`` 做日内累计；Long / Short / Total 三子图。"""
	fig, axes = plt.subplots(3, 1, figsize=(12, 10), sharex=True)

	def _one(ax: plt.Axes, sub: pl.DataFrame, title: str, color: str) -> None:
		ax.set_title(title)
		ax.set_ylabel("Cumulative PnL")
		ax.axhline(0.0, color="#334155", linewidth=1.0)
		ax.grid(alpha=0.25)
		if sub.height == 0:
			ax.text(0.5, 0.5, "No trades", ha="center", va="center", transform=ax.transAxes, color="#64748b")
			return
		plot_df = (
			sub.select(["exit_time", "pnl"])
			.with_columns(pl.col("exit_time").str.strptime(pl.Datetime, format="%Y-%m-%d %H:%M:%S", strict=False).alias("exit_dt"))
			.drop_nulls(["exit_dt", "pnl"])
			.sort("exit_dt")
		)
		if plot_df.height == 0:
			ax.text(0.5, 0.5, "No valid exit_time", ha="center", va="center", transform=ax.transAxes, color="#64748b")
			return
		x = plot_df.get_column("exit_dt").to_list()
		y = np.cumsum(plot_df.get_column("pnl").cast(pl.Float64).to_numpy())
		ax.plot(x, y, color=color, linewidth=1.8)

	long_df = filtered.filter(pl.col("side") == "long")
	short_df = filtered.filter(pl.col("side") == "short")
	_one(axes[0], long_df, "Long intraday cumulative PnL", "#1d4ed8")
	_one(axes[1], short_df, "Short intraday cumulative PnL", "#b91c1c")
	_one(axes[2], filtered, "Total intraday cumulative PnL", "#0f766e")
	axes[2].set_xlabel("Exit time")
	fig.autofmt_xdate()
	fig.tight_layout()
	fig.savefig(out_path, dpi=180)
	plt.close(fig)


def _fmt_rate(v: float | None) -> str:
	if v is None:
		return "—"
	return f"{v * 100:.6f}%"


def write_summary_txt(
	out_dir: Path,
	trade_date: str,
	metrics: dict[str, Any],
	n_index_constituents: int,
	index_code: str | None,
	weights_path: Path,
	png_name: str,
	weights_parquet_date: str,
) -> None:
	"""写入 ``summary.txt``：人类可读的日度汇总（与旧版 HTML 表内容一致）。"""
	lines = [
		"指数过滤 — 日度汇总",
		"=" * 48,
		"",
		"[说明]",
		"  交易日 T：读取权重 parquet 中 date = 「T 的下一交易日」的行；下一交易日由权重表中去重后的 date 序列决定（跳过周末等）。",
		"",
		"[指数与数据]",
		f"  交易日 (目录名):     {trade_date}",
		f"  权重表选用 date:     {weights_parquet_date}",
		f"  指数权重文件:        {weights_path}",
		f"  指数代码 (权重表):   {index_code or '(无)'}",
		f"  成分股数量 (去重):  {n_index_constituents}",
		"",
		"[成交覆盖 (过滤后)]",
		f"  成交笔数:            {metrics['n_trades']}",
		f"  有成交的股票数:      {metrics['n_traded_symbols']}",
		"",
		"[盈亏 (pnl 已含手续费与印花税)]",
		f"  total_pnl:           {metrics['total_pnl']:.6f}",
		f"  long_pnl:            {metrics['long_pnl']:.6f}",
		f"  short_pnl:           {metrics['short_pnl']:.6f}",
		"",
		"[名义本金 entry_notional]",
		f"  total:               {metrics['total_entry_notional']:.6f}",
		f"  long:                {metrics['long_entry_notional']:.6f}",
		f"  short:               {metrics['short_entry_notional']:.6f}",
		"",
		"[日度收益率 = sum(pnl) / sum(entry_notional)]",
		f"  total_return_rate:   {_fmt_rate(metrics['total_return_rate'])}",
		f"  long_return_rate:    {_fmt_rate(metrics['long_return_rate'])}",
		f"  short_return_rate:   {_fmt_rate(metrics['short_return_rate'])}",
		"",
		"[导出]",
		"  过滤后成交明细:      strategy_layer/all_trades.csv",
		"",
		"[图表]",
		f"  当日日内累计 PnL 图: 同目录下 {png_name}",
		"  (Long / Short / Total 三子图)",
		"",
	]
	(out_dir / "summary.txt").write_text("\n".join(lines), encoding="utf-8")


def run_index_filter_repo(
	repo_root: Path,
	index_weights: Path,
	output_root: Path,
	index_label: str,
	start_date: str | None = None,
	end_date: str | None = None,
) -> Path:
	"""对每个交易日写一份日度包。

	成分对齐：交易日 ``T`` 使用权重表中 ``date`` 为 **T 的下一交易日** 的行（下一交易日 = 权重表里出现的 date 中第一个大于 T 的日期）。

	输出布局（规范）::

	    {output_root}/{index_label}/{YYYY-MM-DD}/
	        summary.txt
	        intraday_cum_pnl.png
	        strategy_layer/
	            all_trades.csv
	"""
	if not index_weights.is_file():
		raise FileNotFoundError(index_weights)

	index_label = sanitize_index_label(index_label)
	date_repos = discover_date_repos(repo_root, start_date, end_date)
	if not date_repos:
		raise FileNotFoundError(f"未在 {repo_root} 找到可用的 strategy_layer/all_trades.csv")

	trading_days = trading_days_sorted(index_weights)
	if not trading_days:
		raise RuntimeError(f"{index_weights} 中没有任何 date 列数据，无法确定下一交易日")

	out_base = output_root / index_label
	out_base.mkdir(parents=True, exist_ok=True)
	png_name = "intraday_cum_pnl.png"

	for item in date_repos:
		df = pl.read_csv(item.all_trades_path, schema_overrides={"symbol": pl.Utf8})
		if "symbol" not in df.columns or "pnl" not in df.columns or "entry_notional" not in df.columns:
			continue

		constituents, n_idx, idx_code, weights_date_used = load_constituents_for_date(
			index_weights, item.trade_date, trading_days
		)
		filtered = filter_trades_by_constituents(df, constituents)
		metrics = daily_return_metrics(filtered)

		day_dir = out_base / item.trade_date
		day_dir.mkdir(parents=True, exist_ok=True)
		strategy_dir = day_dir / "strategy_layer"
		strategy_dir.mkdir(parents=True, exist_ok=True)
		filtered.write_csv(strategy_dir / "all_trades.csv")
		plot_intraday_cumulative_pnl(filtered, day_dir / png_name)
		write_summary_txt(
			day_dir,
			item.trade_date,
			metrics,
			n_index_constituents=n_idx,
			index_code=idx_code,
			weights_path=index_weights,
			png_name=png_name,
			weights_parquet_date=weights_date_used,
		)

	return out_base
