#!/usr/bin/env python3
"""区间汇总报表：基于日度 repo 生成月度/多月汇总 repo。

核心能力：
1. 从 repo_root 下自动发现按日期分目录的 all_trades.csv。
2. 按月或 N 个月分组，生成全市场 long/short/total 累计 PnL 曲线。
3. 生成区间收益率 Top10 / Bottom10 股票柱状图。
4. 为每个区间输出 summary.json（含组合收益率）、symbol_return_rank.csv、pnl_curve.csv 和 report.html。

组合收益率定义（与 symbol 级 return_rate 一致）：total_return_rate = sum(pnl)/sum(entry_notional)，
long_return_rate / short_return_rate 为各边分别用对应 entry_notional 作分母。
"""

from __future__ import annotations

import calendar
import html
import json
import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

import matplotlib

matplotlib.use("Agg")

import matplotlib.pyplot as plt
import numpy as np
import polars as pl


matplotlib.rcParams["font.family"] = "sans-serif"
matplotlib.rcParams["font.sans-serif"] = ["DejaVu Sans", "Arial"]
matplotlib.rcParams["axes.unicode_minus"] = False

DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


@dataclass(frozen=True)
class DateRepo:
	trade_date: str
	date_dir: Path
	all_trades_path: Path


def _month_index(dt: datetime) -> int:
	return dt.year * 12 + dt.month


def _index_to_year_month(index: int) -> tuple[int, int]:
	year = index // 12
	month = index % 12
	if month == 0:
		year -= 1
		month = 12
	return year, month


def _discover_date_repos(repo_root: Path, start_date: str | None, end_date: str | None) -> list[DateRepo]:
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


def _load_all_trades(date_repos: list[DateRepo]) -> pl.DataFrame:
	frames: list[pl.DataFrame] = []
	for item in date_repos:
		df = pl.read_csv(item.all_trades_path, schema_overrides={"symbol": pl.Utf8})
		if "symbol" not in df.columns or "pnl" not in df.columns or "entry_notional" not in df.columns:
			continue
		frames.append(df.with_columns(pl.lit(item.trade_date).alias("trade_date")))
	if not frames:
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
				"symbol": [],
				"trade_date": [],
			},
			schema_overrides={"symbol": pl.Utf8},
		)
	return pl.concat(frames, how="diagonal_relaxed")


def _plot_curve(period_df: pl.DataFrame, out_path: Path) -> pl.DataFrame:
	plot_df = (
		period_df.select(["exit_time", "side", "pnl"])
		.with_columns(pl.col("exit_time").str.strptime(pl.Datetime, format="%Y-%m-%d %H:%M:%S", strict=False).alias("exit_dt"))
		.drop_nulls(["exit_dt", "side", "pnl"])
		.sort("exit_dt")
	)
	if plot_df.height == 0:
		return pl.DataFrame({"exit_dt": [], "cum_pnl_long": [], "cum_pnl_short": [], "cum_pnl_total": []})

	exit_times = plot_df.get_column("exit_dt").to_list()
	sides = plot_df.get_column("side").to_list()
	pnl = plot_df.get_column("pnl").cast(pl.Float64).to_numpy()
	long_part = np.where(np.array(sides) == "long", pnl, 0.0)
	short_part = np.where(np.array(sides) == "short", pnl, 0.0)
	cum_long = np.cumsum(long_part)
	cum_short = np.cumsum(short_part)
	cum_total = np.cumsum(pnl)

	curve_df = pl.DataFrame(
		{
			"exit_dt": exit_times,
			"cum_pnl_long": cum_long,
			"cum_pnl_short": cum_short,
			"cum_pnl_total": cum_total,
		}
	)

	fig, ax = plt.subplots(figsize=(12, 6))
	ax.plot(exit_times, cum_long, color="#1d4ed8", label="Long", linewidth=1.8)
	ax.plot(exit_times, cum_short, color="#b91c1c", label="Short", linewidth=1.8)
	ax.plot(exit_times, cum_total, color="#0f766e", label="Total", linewidth=2.2)
	ax.axhline(0.0, color="#334155", linewidth=1.0)
	ax.set_title("Market Cumulative PnL (Long/Short/Total)")
	ax.set_xlabel("Exit Time")
	ax.set_ylabel("Cumulative PnL")
	ax.grid(alpha=0.25)
	ax.legend()
	fig.autofmt_xdate()
	fig.tight_layout()
	fig.savefig(out_path, dpi=180)
	plt.close(fig)
	return curve_df


def _symbol_return_table(period_df: pl.DataFrame) -> pl.DataFrame:
	return (
		period_df.select(["symbol", "pnl", "entry_notional"])
		.with_columns(
			pl.col("pnl").cast(pl.Float64),
			pl.col("entry_notional").cast(pl.Float64),
		)
		.group_by("symbol")
		.agg(
			pl.len().alias("n_trades"),
			pl.col("pnl").sum().alias("total_pnl"),
			pl.col("entry_notional").sum().alias("total_entry_notional"),
		)
		.with_columns(
			pl.when(pl.col("total_entry_notional") > 0)
			.then(pl.col("total_pnl") / pl.col("total_entry_notional"))
			.otherwise(None)
			.alias("return_rate")
		)
		.with_columns(
			pl.col("symbol").cast(pl.Utf8)
		)
	)


def _plot_top_bottom_return(symbol_ret_df: pl.DataFrame, top_n: int, out_path: Path) -> tuple[list[str], list[str]]:
	usable = symbol_ret_df.filter(pl.col("return_rate").is_not_null()).sort("return_rate")
	if usable.height == 0:
		print(f"警告: 没有有效的收益率数据,跳过绘图 {out_path}")
		fig, ax = plt.subplots(figsize=(14, 6))
		ax.text(0.5, 0.5, "No valid return rate data", ha='center', va='center', fontsize=16, color='gray')
		ax.set_title(f"Top {top_n} / Bottom {top_n} by Return Rate (No Data)")
		ax.axis('off')
		fig.tight_layout()
		fig.savefig(out_path, dpi=180)
		plt.close(fig)
		return [], []

	bottom = usable.head(top_n)
	top = usable.tail(top_n)
	plot_df = pl.concat([bottom, top], how="diagonal_relaxed").unique(subset=["symbol"], keep="last")
	labels = plot_df.get_column("symbol").to_list()
	values = plot_df.get_column("return_rate").cast(pl.Float64).to_list()

	colors = ["#dc2626" if v >= 0 else "#16a34a" for v in values]

	fig, ax = plt.subplots(figsize=(14, 6))
	ax.bar(labels, values, color=colors)
	ax.axhline(0.0, color="#334155", linewidth=1.0)
	ax.set_title(f"Top {top_n} / Bottom {top_n} by Return Rate")
	ax.set_ylabel("return_rate")
	ax.tick_params(axis="x", rotation=45)
	ax.grid(axis="y", alpha=0.25)
	fig.tight_layout()
	fig.savefig(out_path, dpi=180)
	plt.close(fig)

	return top.sort("return_rate", descending=True).get_column("symbol")[:top_n].to_list(), bottom.get_column("symbol")[:top_n].to_list()


def _write_html(period_dir: Path, summary: dict[str, object]) -> None:
	html_path = period_dir / "report.html"
	rows = "\n".join(
		f"<tr><th>{html.escape(str(k))}</th><td>{html.escape(json.dumps(v, ensure_ascii=False) if isinstance(v, (list, dict)) else str(v))}</td></tr>"
		for k, v in summary.items()
	)
	html_text = f"""
<!DOCTYPE html>
<html lang=\"zh-CN\">
<head>
  <meta charset=\"utf-8\">
  <title>period report</title>
  <style>
    body {{ font-family: 'DejaVu Sans', sans-serif; margin: 24px; background: #f8fafc; color: #0f172a; }}
    table {{ border-collapse: collapse; width: 860px; max-width: 100%; background: white; }}
    th, td {{ border: 1px solid #cbd5e1; padding: 10px 12px; text-align: left; }}
    th {{ width: 320px; background: #e2e8f0; }}
    img {{ max-width: 100%; height: auto; border: 1px solid #ddd; margin-top: 14px; }}
  </style>
</head>
<body>
  <h1>区间回测汇总</h1>
  <table>{rows}</table>
  <h2>全市场累计 PnL 曲线</h2>
  <img src=\"market_long_short_total_cumulative_pnl.png\" alt=\"market_long_short_total_cumulative_pnl\" />
  <h2>收益率 Top / Bottom 股票</h2>
  <img src=\"symbol_return_rank.png\" alt=\"symbol_return_rank\" />
</body>
</html>
""".strip()
	html_path.write_text(html_text, encoding="utf-8")


def _period_label(min_dt: datetime, max_dt: datetime) -> str:
	return f"{min_dt.strftime('%Y-%m-%d')}_to_{max_dt.strftime('%Y-%m-%d')}"


def _period_return_metrics(period_df: pl.DataFrame) -> dict[str, float | None]:
	if period_df.height == 0:
		return {
			"total_entry_notional": 0.0,
			"long_entry_notional": 0.0,
			"short_entry_notional": 0.0,
			"total_return_rate": None,
			"long_return_rate": None,
			"short_return_rate": None,
		}

	total_entry = float(period_df.get_column("entry_notional").cast(pl.Float64).sum())
	total_pnl = float(period_df.get_column("pnl").cast(pl.Float64).sum())

	long_df = period_df.filter(pl.col("side") == "long")
	short_df = period_df.filter(pl.col("side") == "short")
	long_entry = (
		float(long_df.get_column("entry_notional").cast(pl.Float64).sum()) if long_df.height > 0 else 0.0
	)
	short_entry = (
		float(short_df.get_column("entry_notional").cast(pl.Float64).sum()) if short_df.height > 0 else 0.0
	)
	long_pnl = float(long_df.get_column("pnl").cast(pl.Float64).sum()) if long_df.height > 0 else 0.0
	short_pnl = float(short_df.get_column("pnl").cast(pl.Float64).sum()) if short_df.height > 0 else 0.0

	return {
		"total_entry_notional": total_entry,
		"long_entry_notional": long_entry,
		"short_entry_notional": short_entry,
		"total_return_rate": (total_pnl / total_entry) if total_entry > 0 else None,
		"long_return_rate": (long_pnl / long_entry) if long_entry > 0 else None,
		"short_return_rate": (short_pnl / short_entry) if short_entry > 0 else None,
	}


def build_period_repo(
	repo_root: Path,
	period_months: int,
	top_n: int,
	output_subdir: str,
	start_date: str | None,
	end_date: str | None,
) -> Path:
	if period_months <= 0:
		raise ValueError("period_months 必须大于 0")
	if top_n <= 0:
		raise ValueError("top_n 必须大于 0")

	date_repos = _discover_date_repos(repo_root, start_date, end_date)
	if not date_repos:
		raise FileNotFoundError(f"未在 {repo_root} 找到可用的日度 strategy_layer/all_trades.csv")

	all_trades = _load_all_trades(date_repos)
	if all_trades.height == 0:
		raise RuntimeError("发现了日期目录，但 all_trades.csv 数据为空或缺少必要字段")

	all_trades = all_trades.with_columns(
		pl.col("exit_time").str.strptime(pl.Datetime, format="%Y-%m-%d %H:%M:%S", strict=False).alias("exit_dt")
	).drop_nulls(["exit_dt"])

	if all_trades.height == 0:
		raise RuntimeError("all_trades 的 exit_time 解析后为空，无法生成区间报表")

	all_trades = all_trades.with_columns(
		(
			(
				(pl.col("exit_dt").dt.year() * 12 + pl.col("exit_dt").dt.month())
			)
		).alias("month_index")
	)

	base_month = int(all_trades.get_column("month_index").min())
	all_trades = all_trades.with_columns(
		((pl.col("month_index") - base_month) // period_months).alias("period_id")
	)

	out_root = repo_root / output_subdir 
	out_root.mkdir(parents=True, exist_ok=True)

	for period_id in sorted(all_trades.get_column("period_id").unique().to_list()):
		period_df = all_trades.filter(pl.col("period_id") == period_id).sort("exit_dt")
		if period_df.height == 0:
			continue
		min_dt = period_df.get_column("exit_dt").min()
		max_dt = period_df.get_column("exit_dt").max()
		if not isinstance(min_dt, datetime) or not isinstance(max_dt, datetime):
			continue
		label = _period_label(min_dt, max_dt)
		period_dir = out_root / label
		period_dir.mkdir(parents=True, exist_ok=True)

		curve_df = _plot_curve(period_df, period_dir / "market_long_short_total_cumulative_pnl.png")
		curve_df.write_csv(period_dir / "pnl_curve.csv")

		symbol_ret_df = _symbol_return_table(period_df)
		symbol_ret_df = symbol_ret_df.with_columns(pl.col("symbol").cast(pl.Utf8))
		symbol_ret_df.sort("return_rate", descending=True, nulls_last=True).write_csv(period_dir / "symbol_return_rank.csv")
		top_symbols, bottom_symbols = _plot_top_bottom_return(symbol_ret_df, top_n, period_dir / "symbol_return_rank.png")

		total_pnl = float(period_df.get_column("pnl").cast(pl.Float64).sum()) if period_df.height > 0 else 0.0
		long_pnl = float(period_df.filter(pl.col("side") == "long").get_column("pnl").cast(pl.Float64).sum()) if period_df.height > 0 else 0.0
		short_pnl = float(period_df.filter(pl.col("side") == "short").get_column("pnl").cast(pl.Float64).sum()) if period_df.height > 0 else 0.0

		ret_metrics = _period_return_metrics(period_df)
		summary = {
			"repo_root": str(repo_root),
			"period_months": period_months,
			"period_label": label,
			"start_time": min_dt.strftime("%Y-%m-%d %H:%M:%S"),
			"end_time": max_dt.strftime("%Y-%m-%d %H:%M:%S"),
			"n_trades": int(period_df.height),
			"n_symbols": int(symbol_ret_df.height),
			"total_pnl": total_pnl,
			"long_total_pnl": long_pnl,
			"short_total_pnl": short_pnl,
			"total_entry_notional": ret_metrics["total_entry_notional"],
			"long_entry_notional": ret_metrics["long_entry_notional"],
			"short_entry_notional": ret_metrics["short_entry_notional"],
			"total_return_rate": ret_metrics["total_return_rate"],
			"long_return_rate": ret_metrics["long_return_rate"],
			"short_return_rate": ret_metrics["short_return_rate"],
			"top_symbols_by_return": top_symbols,
			"bottom_symbols_by_return": bottom_symbols,
		}
		(period_dir / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
		_write_html(period_dir, summary)

	return out_root
