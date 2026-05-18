"""CLI：``python -m tool.index_filter``（在项目根目录 / 已将当前目录加入 PYTHONPATH 时执行）。

输出目录规范::

    {output-root}/{index-label}/{YYYY-MM-DD}/
        summary.txt
        intraday_cum_pnl.png
        strategy_layer/all_trades.csv

``--index-label`` 默认等于权重文件名（不含扩展名），例如 ``index_weights_300.parquet`` → ``index_weights_300``。
请固定使用同一 ``--output-root``，用不同 ``--index-label``（或不同权重文件）区分 CSI300/500/1000。
"""

from __future__ import annotations

import argparse
from pathlib import Path

from .core import run_index_filter_repo


def _build_parser() -> argparse.ArgumentParser:
	p = argparse.ArgumentParser(
		description=(
			"按指数成分过滤日度 all_trades，写入 "
			"output-root/index-label/日期/summary.txt、intraday_cum_pnl.png、strategy_layer/all_trades.csv"
		),
		formatter_class=argparse.RawDescriptionHelpFormatter,
		epilog=(
			"目录示例:\n"
			"  /home/you/data/index_filter/index_weights_300/2024-01-03/summary.txt\n"
			"  /home/you/data/index_filter/index_weights_300/2024-01-03/strategy_layer/all_trades.csv"
		),
	)
	p.add_argument(
		"--repo-root",
		type=Path,
		default=Path("/home/haoranyou/data/output/alpha0.4_1w_5s_3ticks"),
		help="日度 repo 根目录：repo_root/YYYY-MM-DD/strategy_layer/all_trades.csv",
	)
	p.add_argument(
		"--index-weights",
		type=Path,
		required=True,
		help="指数权重 parquet（列含 date, code, stock_code, weight）",
	)
	p.add_argument(
		"--output-root",
		type=Path,
		default=Path("/home/haoranyou/data/output/alpha0.4_1w_5s_3ticks_index_filter"),
		help="输出根目录（建议固定一处）；完整路径为 output-root/index-label/YYYY-MM-DD/",
	)
	p.add_argument(
		"--index-label",
		type=str,
		default='index_weights_300',
		help="该次运行使用的子目录名（如 index_weights_300）；默认取权重文件名不含扩展名",
	)
	p.add_argument("--start-date", type=str, default=None, help="可选 YYYY-MM-DD")
	p.add_argument("--end-date", type=str, default=None, help="可选 YYYY-MM-DD")
	return p


def main() -> None:
	args = _build_parser().parse_args()
	label = args.index_label or args.index_weights.stem
	out = run_index_filter_repo(
		repo_root=args.repo_root.resolve(),
		index_weights=args.index_weights.resolve(),
		output_root=args.output_root.resolve(),
		index_label=label,
		start_date=args.start_date,
		end_date=args.end_date,
	)
	print("完成。")
	print("  指数子目录 (index-label):", label)
	print("  日度包路径模板:            ", out / "{YYYY-MM-DD} /")
	print("  本次 output-root 下根路径: ", out)


if __name__ == "__main__":
	main()
