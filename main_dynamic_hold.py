#!/usr/bin/env python3
"""与 main.py 相同的 data → strategy → analysis 编排；默认策略为 dynamic_hold。

仍可通过 ``--strategy-mode legacy`` 与 main.py 行为对齐做对比。等价写法：
``python main.py --strategy-mode dynamic_hold ...`` 与同参调用本脚本一致（除默认 strategy_mode）。
"""

from __future__ import annotations

from main import _build_parser
from main import run_pipeline


def main() -> None:
	run_pipeline(_build_parser(default_strategy_mode="dynamic_hold").parse_args())


if __name__ == "__main__":
	main()
