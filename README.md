# alpha_trade

本项目是一个 A 股日内回测流水线，核心链路为：

`alpha parquet` -> `eventstream(.npz)` -> `hftbacktest 策略回放` -> `分析报表`

当前可用主入口是：

- `main_legacy.py`：默认 `legacy` 策略
- `main_dynamic_hold.py`：默认 `dynamic_hold` 策略
- `main_gridtrading.py`：独立 grid trading 回测入口

---

## 1. 代码结构与职责

- `main_legacy.py`  
  总编排入口：调用 `data` 准备数据，调用 `strategy` 跑回测，调用 `analysis` 生成报表。
- `main_dynamic_hold.py`  
  与 `main_legacy.py` 编排一致，只是默认 `--strategy-mode dynamic_hold`。
- `main_gridtrading.py`  
  单独的网格策略入口（按 symbol 跑，输出在 `strategy_layer/gridtrading/`）。

- `data/`  
  数据准备层：定位 alpha parquet、生成/复用 eventstream、输出 `data_layer/manifest/`。
  - `data/universe.py`：数据准备主逻辑
  - `data/alpha_io.py`：alpha 文件定位与读取
  - `data/sse_eventstream.py` / `data/szse_eventstream.py`：交易所事件流转换

- `strategy/`  
  策略层与回测执行层：
  - `strategy/run.py`：逐股票执行策略并汇总
  - `strategy/legacy/engine.py`：固定持有策略
  - `strategy/dynamic_hold/engine.py`：动态持仓策略（含 TP/SL、TWAP、AUM 约束）
  - `strategy/gridtrading/engine.py`：网格策略
  - `strategy/aum/`：AUM 预算记账与裁剪
  - `strategy/twap.py`：TWAP 切片逻辑

- `analysis/`  
  报表层：生成 `overall_summary.json`、`report.html`、PNG 图表、个股图目录。

- `tool/`  
  独立分析工具：
  - `tool.index_filter`：按指数成分过滤回测结果
  - `tool.index_weights`：权重读取与 daily AUM 分摊
  - `tool.period_repo`：多日/多月区间汇总

---

## 2. 快速开始

### 2.1 单日运行（legacy）

```bash
python main_legacy.py --date YYYY-MM-DD --run-name YOUR_RUN
```

### 2.2 单日运行（dynamic_hold）

```bash
python main_dynamic_hold.py --date YYYY-MM-DD --run-name YOUR_RUN
```

### 2.3 单日运行（gridtrading）

```bash
python main_gridtrading.py --date YYYY-MM-DD --run-name YOUR_RUN --max-symbols 50
```

### 2.4 查看参数

```bash
python main_legacy.py --help
python main_dynamic_hold.py --help
python main_gridtrading.py --help
```

---

## 3. 批量运行脚本

仓库内提供三份动态策略批量脚本（按指数口径区分默认权重）：

- `run_dynamic_300.sh`
- `run_dynamic_500.sh`
- `run_dynamic_1000.sh`

示例：

```bash
bash run_dynamic_300.sh
```

常用可覆盖环境变量：

- `DAYS_FILE`：日期文件（每行一个 `YYYY-MM-DD`）
- `RUN_NAME`：实验名
- `ALPHA`：alpha 文件/目录
- `DAILY_AUM`、`DEFAULT_INDEX_WEIGHTS`：AUM 与权重源
- `THRESHOLD`、`NOTIONAL`、`TWAP_SLICE_NOTIONAL` 等策略参数

---

## 4. 输出目录说明

默认单日输出目录：

`/home/haoranyou/data/output/{run_name}/{date}/`

主要内容：

- `run_config.json`：本次运行所有关键参数快照
- `data_layer/manifest/`
  - `prepared_symbols.csv`
  - `skipped_symbols.csv`
- `strategy_layer/`
  - `symbol_summary.csv`
  - `all_trades.csv`
  - `strategy_failures.csv`
  - `trades/*.csv`
  - `aum_allocation.parquet`（启用 AUM 时）
- `report_layer/`
  - `overall_summary.json`
  - `report.html`
  - `cumulative_pnl.png`、`rank_*.png`、`pnl_distribution.png`
  - `symbol_charts/{symbol}/net_position.png`

`main_gridtrading.py` 额外输出到：

- `strategy_layer/gridtrading/`
  - `symbol_summary.csv`
  - `failures.csv`
  - `{symbol}_{date}_grid_points.csv`
  - `{symbol}_{date}_grid_summary.json`

---

## 5. 需要修改的数据位置（重点）

你后续“改数据源/改路径/改默认值”时，优先按下面顺序处理。

### 5.1 最推荐：用 CLI 参数改（不改代码）

- 改 alpha 源：`--alpha`
- 改输出目录：`--repo-dir`
- 改股票池：`--symbols` / `--max-symbols`
- 改权重文件：`--index-weights` 或 `--default-index-weights`
- 改 eventstream 根目录（grid 入口）：`--eventstream-root`

### 5.2 批量脚本里改（常用）

修改以下脚本顶部变量：

- `run_dynamic_300.sh`
- `run_dynamic_500.sh`
- `run_dynamic_1000.sh`

重点变量：

- `ALPHA`
- `DAYS_FILE`
- `DEFAULT_INDEX_WEIGHTS`
- `INDEX_FILTER_OUTPUT`
- `RUN_NAME`

### 5.3 改代码默认常量（最后手段）

- 输出根目录默认值：`data/universe.py`
  - `DEFAULT_OUTPUT_ROOT`
- shared eventstream 默认值：`data/universe.py`
  - `DEFAULT_SHARED_EVENTSTREAM_ROOT`
- legacy/dynamic 默认权重文件：`main_legacy.py`
  - `DEFAULT_INDEX_WEIGHTS_PATH`
- gridtrading 默认 eventstream 根路径：`main_gridtrading.py`
  - `DEFAULT_EVENTSTREAM_ROOT`
  - `FALLBACK_EVENTSTREAM_ROOT`

---

## 6. 功能总览

- `legacy`  
  阈值开仓 + 固定持有时长平仓。
- `dynamic_hold`  
  阈值开仓 + 相对 `alpha_0` 阶梯调仓 + 可选 TP/SL + 可选 TWAP。
- `AUM`  
  `--daily-aum > 0` 时按指数权重分配到各 symbol，限制增仓预算（多空独立记账）。
- `index_filter`  
  按指数成分过滤 `all_trades.csv`，生成日度 `summary.txt` 与图。
- `period_repo`  
  对多日结果做区间聚合（按月或 N 个月）并输出汇总报表。

---

## 7. 常用工具命令

```bash
# 仅跑数据准备层
python -m data --help

# 指数过滤
python -m tool.index_filter --help

# 区间汇总
python -m tool.period_repo --help
```

---

## 8. 注意事项

- 当前仓库中的路径默认值偏本地环境（如 `/data/...`、`/home/haoranyou/...`）。
- 迁移机器时，优先使用 CLI 参数或脚本环境变量覆盖，尽量不要直接改代码常量。
- `main_legacy.py` 与 `main_dynamic_hold.py` 才是当前主入口，若你的旧命令仍是 `main.py`，请改成上述两个文件之一。
