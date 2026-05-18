# Grid trading 策略 pipeline（规格）

本文与仓库内参考实现 `example.py` 对齐，并补充 **alpha**、**指数权重底仓**、**与本项目 data/strategy/analysis 的衔接**。实现时新增 `strategy/gridtrading`（名称可微调）与入口 **`main_gridtrading.py`**，**不修改** `dynamic_hold` 与现有 `main.py` 行为。

---

## 1. 业务目标

- **目标净仓（底仓中心）**：按指数成分权重与总 AUM 得到每标的当日 **目标持股数**（可与「对称带宽」一起构成上下限）；日内围绕该中心做网格，**尾盘回到该中心**。
- **多空与上限**：**不设**「仅做多」开关。仿真 **初始净仓为 0**；目标净仓 `desired_inventory` 由权重 × 总 AUM 等得到（与「已有底仓」在数学上等价）。允许 **做空**，净空头规模（及增仓侧名义）以 **`markdown/rules.md`** 中 **AUM / `used_short`**（及对称的 **`used_long`**）为硬上限：**负仓位不得超出按 AUM 分摊得到的空头上限**，正仓同理。相对目标中心的 **对称带宽**（如 `inventory_span` 或等价股数）在带宽内网格交易；带宽与 AUM 裁剪可同时生效（发单前取更紧约束）。
- **尾盘强平**：**14:45 之后不再进行网格化交易**。在 **14:45–14:57** 进入强平阶段，目标是把净仓强制收敛到**初始仓位（即目标底仓）**；14:57 前持续重试，结束时仓位应尽量与初始仓位一致。
- **Alpha**：信号数据与全项目一致——**当日 parquet**，字段与 `strategy.backtest.load_alpha_records_grouped` / pipeline 一致（`symbol, time, alpha` → `(signal_ts_ns, alpha)` 列表按时间排序）。定价里使用 alpha 项，见第 4 节。

---

## 2. 技术栈（与 `example.py` 一致）

- **引擎**：`hftbacktest.ROIVectorMarketDepthBacktest` + `BacktestAsset`；订单类型 **LIMIT + GTC**（与当前 `strategy/backtest.py`、`dynamic_hold` 一致）。
- **网格**：**并非** hftbacktest 内置名为 `gridtrading` 的单独 API；参考实现为 **自研 numba 策略循环**：每步 `elapse` → 读盘口 → 算 `reservation_price` → 在买卖两侧各铺 **多层** 限价单，用 tick 或 bps 控制半边价差与层间距，并对不可再挂的订单 **撤单重挂**。
- **记录**：使用 `hftbacktest.Recorder`（或等价方式）按固定间隔写入 `position`、`balance`、`fee`、`num_trades` 等，供权益与仓位曲线使用（字段与示例对齐即可）。

---

## 3. 交易时间窗与撤单缓冲

以下与 `example.py` 中「交易所日内时钟」一致：用 **`clock_ns = current_timestamp % DAY_NS`** 判断是否在窗口内（`DAY_NS = 24h`）。

| 阶段 | 含义 | 日内时间（与示例常量一致） |
|------|------|------------------------------|
| 数据段 | eventstream 覆盖 | 09:15–11:30、13:00–15:00（由数据准备保证） |
| 上午可交易 | 允许发网格单 | **09:45 ≤ t < 11:30**（预留 `cutoff_buffer_ns`，见下） |
| 午休 | 强制撤单、不发新单 | 11:30–13:00 |
| 下午网格交易 | 允许发网格单 | **13:00 ≤ t < 14:45** |
| 尾盘强平阶段 | 停止网格，仅做强平 | **14:45 ≤ t < 14:57** |
| 日内结束 | 停止发单并做结果统计 | **14:57** |

- **`ORDER_LATENCY_NS` / `SESSION_CUTOFF_BUFFER_NS`**：在上午收市前、**14:45 强平切换前**、14:57 截止前预留缓冲，提前撤单，避免延迟导致跨阶段成交。
- **午休与盘前**：不发网格；对可撤订单执行撤单（与示例一致）。

---

## 4. 定价：底仓偏离 + Alpha（在示例上扩展）

参考实现（无 alpha）核心为：

- `mid_price = 0.5 * (best_bid + best_ask)`
- `deviation_in_lots = (position - desired_inventory) / lot_size`
- `reservation_price = mid_price - skew * tick_size * deviation_in_lots`

本 pipeline **在接入 alpha 后**采用（**skew1、skew2 参数化**；单位在实现中与 `tick_size` / alpha 量级一起标定，可配置「每 lot 偏移 tick 数」与「每单位 alpha 的价格偏移」）：

```text
reservation_price = mid_price - skew1 * f_pos(deviation) + skew2 * f_alpha(alpha)
```

- **`f_pos`**：默认与示例一致，**`f_pos = tick_size * deviation_in_lots`**（即等价于用 **相对目标底仓的偏差（手）** 把报价往「回补底仓」方向推）；若实现改为按 **净股数** 线性项，须在配置中写清系数定义。
- **`f_alpha`**：使用 **当前仿真步可用的 alpha 标量**。语义与初版需求一致：**alpha 越大（越看多），报价整体越高，更容易买入**；故在公式中取 **`+ skew2 * alpha`**（`skew2 > 0` 时 alpha 为正抬高 `reservation_price`）。若 alpha 已标准化，可为 `skew2 * g(alpha)`（`g` 默认恒等，可后加 clip）。
- **Alpha 时间序列**：与 `dynamic_hold` 一致，仅在有记录的 **`signal_ts_ns <= now_ts`** 上更新「当前 alpha」；**两点之间前向填充**（last-known），无历史点之前不参与 skew2 项（或视为 0，实现二选一拍板，**默认前向填充**）。
- **尾盘阶段**：`14:45` 起停止网格，不再按网格 reservation 价双边铺单；改走第 5 节强平流程（可使用 aggressive 限价与 extra ticks）。

随后在 `reservation_price` 两侧展开 **半边 spread** 与 **多层网格**（tick 模式或 bps 模式），并对价格做 tick / `grid_interval` 对齐；逻辑同 `example.py` 中 `bid_price`/`ask_price` 与 `new_bid_orders`/`new_ask_orders` 循环。

---

## 5. 尾盘强平流程（参考 `strategy/dynamic_hold/engine.py`）

`14:45` 进入强平模式后，策略行为切换为：

- **停止网格**：不再新发网格买卖单，不再消费 alpha 做网格调价。
- **先清障**：撤掉非强平来源的在途挂单（尤其是普通网格单与非当前强平单），避免与强平指令并发冲突。
- **强平目标**：令 `target_position = initial_position`（初始仓位/目标底仓）。若 `position != target_position`，按缺口方向提交 aggressive 限价单（可加 `force_flatten_extra_ticks`），直到缺口收敛。
- **在途互斥**：同一时刻仅保留一笔“当前强平单”在途；若超时未完全成交（参考 dynamic 的 `EXIT_ORDER_STALE_NS=60s` 语义），撤单后重挂下一笔。
- **流动性门控**：下单前做对手盘一档可交易检查（与 dynamic 的 `_is_tradable_for_side_l1_min1lot` 思路一致）；若被挡，记录 `blocked_forced_exit_l1_count`。
- **回放结束兜底**：`14:57` 后仍未完全回到初始仓位时，执行 fallback flatten 结算并输出未能在盘中平完的股数统计（如 `shares_unflattenable_after_1445` 语义）。

---

## 6. 库存约束与「对称上限」

- **`desired_inventory`（目标股数）**：由 **权重 × AUM**（如 1e8）÷ 参考价再整手得到，与现有权重工具链一致；**初始持仓为 0**，不在开盘瞬间物理建底仓，由日内成交逼近目标与尾盘收敛。
- **`inventory_span_lots`（或等价股数）**：相对目标净仓的 **对称** 带宽（手）：允许净仓 **`desired - span×lot` ≤ position ≤ desired + span×lot`** 内发单（实现上买侧受上界、卖侧受下界）；与 **AUM 上限**同时作用时，**净多不得超过 `used_long` 允许增多的上界、净空不得超过 `used_short` 允许增多的上界**（见 `rules.md` 发单前裁剪）。
- **已移除 `only_long`**：不再提供「仅非负净仓」模式；做空由 **AUM + 带宽** 约束，不做单独 A 股裸空开关。

---

## 7. 可调参数清单（实现侧建议与 CLI 对齐）

自 `example.py` 继承并扩展：

| 参数概念 | 说明 |
|----------|------|
| `step_ns` | 策略步长（`elapse`） |
| `recorder_interval_ns` | 曲线采样间隔 |
| `grid_num` | 单边网格层数 |
| `order_lots` | 每笔手数 |
| `inventory_span_lots` | 相对目标仓位的波动带宽（手） |
| `half_spread_ticks` / `grid_step_ticks` 或 bps 模式 | 半边价差与层间距 |
| `skew1` | 网格阶段位置项系数（09:45–14:45） |
| `skew2` | 网格阶段 alpha 项系数（09:45–14:45） |
| `force_flatten_hhmmss` | 强平开始时刻，默认 **14:45:00** |
| `force_flatten_end_hhmmss` | 强平结束时刻，默认 **14:57:00** |
| `force_flatten_extra_ticks` | 强平挂单额外激进 ticks（参考 dynamic） |
| `ORDER_LATENCY_NS` / `cutoff_buffer_ns` | 与示例一致 |

手续费、tick、lot、`roi_lb`/`roi_ub`、延迟等尽量与 **`strategy.backtest.build_hbt`** / `StrategyConfig` 对齐，便于与 `main_gridtrading.py` 共用配置源。

---

## 8. 产出与指标

- **仓位曲线**：日内 `position`（净股数）相对时间。
- **权益曲线**：`equity_wo_fee = balance + position * mark_price`，`equity = equity_wo_fee - fee`（`mark_price` 用 recorder 中成交价或 mid 的 ffill/bfill 规则与示例一致）。
- **未在目标时刻回仓的股数**：在 **14:57:00** 记录 `|position - initial_position|`（是否回到初始仓位/目标底仓）；并输出强平阻塞与兜底统计。
- **收益与收益率、夏普、回撤等**：与当前 **analysis / repo 报表** 对齐方式同 `main.py` pipeline；若 grid 先独立跑通，可复用 `generate_report` 的输入结构，在 `summary_df` 中增加 grid 专有列时保持向后兼容。

---

## 9. 全局限制

- **不改**现有 `dynamic_hold` 与 `main.py` 默认路径；新增 **`main_gridtrading.py`** 编排：数据准备 → 权重/AUM → 逐标的（或批量）回测 → 报表输出。
- **依赖**：参考示例使用 **`numba`** 编译策略循环；若 CI 环境无 numba，需文档说明或提供纯 Python 降级路径（可选，非首版必须）。

---

## 10. 修订记录

- 与初版相比：明确 **网格实现参照 `example.py`**（ROI + 自研多层限价），**修正**「使用 hftbacktest 内置 gridtrading 方法」的表述；补充 **alpha 前向填充**、**尾盘参数**、**Recorder 产出**、**与 rules.md 权重日一致** 及 **`main_gridtrading.py`** 入口。
- **移除 `only_long`**：默认 **初始净仓 0**、允许做空；空头（及多头）规模由 **AUM 分摊上限** 与 **相对目标的带宽** 共同约束；示例代码与规格一致删除该参数。
- **尾盘阶段改为强平**：`14:45` 后停止网格；`14:45–14:57` 参考 `dynamic_hold` 强平流程（撤单清障、在途互斥、超时撤单重挂、流动性门控、收盘 fallback）。
