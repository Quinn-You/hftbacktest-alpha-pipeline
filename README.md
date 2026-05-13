# alpha_trade

单日回测流水线：`alpha parquet` → `event stream(.npz)` → `hftbacktest 策略撮合` → `报表与图表`。  
主入口是 `main.py`，负责串联 `data/`、`strategy/`、`analysis/`。

## 项目结构

- `main.py` / `main_dynamic_hold.py`：单日 pipeline 入口（后者默认 `dynamic_hold`）。
- `run_pipeline.sh`：按 `days.txt` 批量跑多天。
- `data/`：读取 alpha、生成/复用 SSE/SZSE event stream、写 `data_layer/manifest/`。
- `strategy/`：回测引擎与策略逻辑（`legacy`、`dynamic_hold`、`aum`、`twap`）。
- `analysis/`：汇总指标、生成 `report.html` 和图表。
- `tool/`：独立工具（指数过滤、按权分摊 AUM、区间汇总）。
- `rules.md`：`dynamic_hold` + TWAP + AUM 行为规格（实现对齐文档）。

## 快速开始

在仓库根目录执行：

```bash
python main.py --date YYYY-MM-DD --run-name YOUR_RUN
```

常用变体：

```bash
# 直接使用 dynamic_hold 默认模式
python main_dynamic_hold.py --date YYYY-MM-DD --run-name YOUR_RUN

# 等价写法
python main.py --date YYYY-MM-DD --run-name YOUR_RUN --strategy-mode dynamic_hold
```

查看完整参数：

```bash
python main.py --help
```

## 批量跑多天

```bash
bash run_pipeline.sh
```

- `run_pipeline.sh` 默认读取 `days.txt`（每行一个 `YYYY-MM-DD`）。
- 大多数参数都可用环境变量覆盖（如 `RUN_NAME`、`THRESHOLD`、`NOTIONAL`、`DAILY_AUM`、`TWAP_SLICE_NOTIONAL`）。
- `PIPELINE=dynamic` 时走 `main_dynamic_hold.py`，否则走 `main.py`。

## 关键功能

- `legacy`：固定持有时长。
- `dynamic_hold`：阈值开仓 + 相对 `alpha_0` 阶梯加减仓 + 可选 TP/SL + 可选 TWAP。
- `AUM`（`--daily-aum > 0`）：按权重分配到单标的 `aum`，只限制增仓名义，平仓不受限。
- `TWAP`（dynamic_hold）：`--twap-slice-notional > 0` 且 `--order-size-mode notional` 时启用，每步最多挂一片。

## 输出目录

默认单日输出目录：`.../output/{run_name}/{date}/`，主要包含：

- `run_config.json`
- `data_layer/manifest/`：`prepared_symbols.csv`、`skipped_symbols.csv`
- `strategy_layer/`：`symbol_summary.csv`、`all_trades.csv`、`strategy_failures.csv`、`trades/*.csv`
- `report_layer/`：`overall_summary.json`、`report.html`、各类 PNG 图

默认 event stream 在共享目录（与单日输出解耦），路径由 `data/universe.py` 的默认常量控制。

## 其他入口

- 仅跑数据层：`python -m data --help`
- 指数过滤：`python -m tool.index_filter --help`
- 区间汇总：`python -m tool.period_repo --help`

## 备注

项目中的默认路径多为本地环境约定。迁移环境时，优先通过 CLI 参数或 `run_pipeline.sh` 环境变量覆盖。
