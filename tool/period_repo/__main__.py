"""CLI：``python -m tool.period_repo``（在项目根目录下执行）。"""

from __future__ import annotations

import argparse
from pathlib import Path

from .core import build_period_repo


def _build_parser() -> argparse.ArgumentParser:
	p = argparse.ArgumentParser(description="生成月度/多月区间汇总 repo")
	p.add_argument(
		"--repo-root",
		type=Path,
		default=Path("/home/haoranyou/data/output/alpha0.4_1w_5s_3ticks"),
		help="日度 repo 根目录（目录结构: repo_root/YYYY-MM-DD/strategy_layer/all_trades.csv）",
	)
	p.add_argument("--period-months", type=int, default=1, help="区间长度（月），1=按月，3=按季度近似，6=半年度等")
	p.add_argument("--top-n", type=int, default=10, help="收益率排名展示股票数量")
	p.add_argument("--output-subdir", type=str, default="/home/haoranyou/data/period_repo", help="输出子目录名（相对 repo-root）")
	p.add_argument("--start-date", type=str, default=None, help="可选，起始日期 YYYY-MM-DD")
	p.add_argument("--end-date", type=str, default=None, help="可选，结束日期 YYYY-MM-DD")
	return p


def main() -> None:
	args = _build_parser().parse_args()
	out_root = build_period_repo(
		repo_root=args.repo_root,
		period_months=args.period_months,
		top_n=args.top_n,
		output_subdir=args.output_subdir,
		start_date=args.start_date,
		end_date=args.end_date,
	)
	print("=" * 72)
	print("区间汇总 repo 生成完成")
	print(f"repo_root         : {args.repo_root}")
	print(f"period_months     : {args.period_months}")
	print(f"output_root       : {out_root}")
	print("=" * 72)


if __name__ == "__main__":
	main()
