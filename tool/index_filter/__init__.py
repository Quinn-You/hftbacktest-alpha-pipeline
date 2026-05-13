"""按指数成分过滤日度成交。

输出规范：``{output_root}/{index_label}/{YYYY-MM-DD}/`` 下放 ``summary.txt``、``intraday_cum_pnl.png``、
``strategy_layer/all_trades.csv``。参见 ``python -m tool.index_filter --help``（在项目根目录下执行）。
"""

from .core import run_index_filter_repo

__all__ = ["run_index_filter_repo"]
