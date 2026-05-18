---
name: Dynamic PnL exit attribution
overview: 说明与 legacy 相比 **PnL 差异的主要来源**（不仅是三种平仓），并把你关心的 **时间 / 止盈止损 / 信号反向** 三类在引擎中的位置与对 PnL 的作用写清楚；若需量化占比，再对成交打「退出原因」标签做归因。另支持**不改原代码**的独立对比脚本。
todos:
  - id: alpha-flip-threshold-symmetric
    content: dynamic_hold 信号反向与开仓对称（多 alpha<=-alpha_threshold，空 alpha>=alpha_threshold）；同步 rules / plan 文档
    status: completed
  - id: standalone-compare-script
    content: 在 tests/ 或 scripts/ 新增独立 Python 脚本：输入 legacy 与 dynamic 两次 run 的 repo 路径，只读 CSV/parquet 做汇总对比与启发式分桶（不修改 strategy/backtest 等源码）
    status: pending
  - id: optional-exit-reason
    content: （可选，需改源码）在 Trade 或 engine 侧增加 exit_reason 并在落盘列中输出，用于精确归因三种+强平+部分减+兜底
    status: pending
isProject: false
---

# Dynamic 与 legacy PnL 差异：成因与「三种平仓」

## 先澄清：dynamic 里不止三种平仓

在 [strategy/dynamic_hold/engine.py](strategy/dynamic_hold/engine.py) 中，**已实现盈亏**可能来自多条路径（都会生成 `Trade` 或影响持仓规模）：

| 路径 | 行为 | 与「三种」关系 |
|------|------|----------------|
| **持有到期** | `now_ts >= target_exit_ts_ns` 时全平 | 通常你说的「时间平仓」 |
| **止盈 / 止损** | 中间价 `mark` 相对 `avg_entry_px` 触发 | 一类，可拆成 TP / SL 两子类 |
| **信号反向** | 多仓 `alpha <= -alpha_threshold` 或空仓 `alpha >= alpha_threshold` 全平（与开仓对称） | 常称「信号平仓」 |
| **日末强平窗口** | `force_flatten_ts_ns` 后持续尝试全平 | 与 legacy 类似，但 dynamic 同时 **不再消费 alpha**（`flatten_triggered`） |
| **阶梯减仓** | 部分平仓 `partial`，多笔 `Trade` | **不是**全平「三种」之一，但会显著改变 PnL 轨迹与手续费 |
| **回放结束兜底** | `_forced_flatten_price` 强结 | 与 legacy 对齐，通常占比小除非大量未撮合平掉 |

你问的「三种」在文档语境里多半对应：**时间**、**止盈止损**、**信号反向**；下面按这三类说明**如何拉大与 legacy 的 PnL 差**。

---

## 与 legacy 的根本结构差异（往往比「三种平仓」更致命）

[strategy/legacy/engine.py](strategy/legacy/engine.py) 使用 **`open_positions` 列表**：每次阈值信号通过对手盘一档量检查就开 **新的一腿** `OpenPosition`，各腿 **独立** `target_exit_ts_ns`，**持仓期间不因 alpha 反向而平**，也无 TP/SL、无加仓合并均价。

[dynamic_hold/engine.py](strategy/dynamic_hold/engine.py) 是 **单标的至多一笔合并仓** `DynamicPosition`：**加仓改均价**、**阶梯减仓拆多笔 trade**、**alpha 反向全平**、**TP/SL 提前全平**。

因此同一天 PnL 差很大，常见原因包括：

1. **名义敞口路径不同**：dynamic 在 `delta` 正向时可反复 **加仓**（`adjust_notional`），legacy 是多次 **独立全仓** 叠加；总成交股数、均价、手续费都会偏离。  
2. **退出时点不同**：legacy 多数腿 **扛到各自 hold 到期或强平**；dynamic 可能在到期前被 **TP/SL 或 alpha_flip** 打掉，成交价分布完全不同。  
3. **阶梯减仓**：提前实现盈亏、剩余仓继续暴露，与 legacy「一笔进一笔出」的现金流形状不同。  
4. **`pending_exit` 门闩**：有未完成平仓单时 **暂停** alpha 调仓与反向全平，撮合节奏与 legacy 多腿并行挂单不同，会改变后续成交时刻。

---

## 三类平仓（加 TP/SL 拆开）对 PnL 的典型影响

### 1) 时间到期（`target_exit_ts_ns`）

- **作用**：与 legacy 的「到点挂平」理念最接近，但 dynamic 的均价可能是 **多次加仓后** 的 `avg_entry_px`，同一 `hold_minutes` 下 **平仓价相对入场价的分布**与 legacy 单腿 `entry_px` 不同。  
- **拉大差异的条件**：中途加减仓多、或到期前多次被对手盘一档挡 `blocked_timed_exit_l1_count` 导致实际平出时刻后移。

### 2) 止盈 / 止损（`take_profit_pct` / `stop_loss_pct`，默认关闭）

- **若未传参**（`None`）：**不参与** PnL 差异。  
- **若开启**：用 **中间价** `mark` 与均价比较，会在到期 **之前** 全平；对日内 PnL 往往非常敏感（尤其 `mark` 在价差大时与成交价路径不一致）。

### 3) 信号反向（alpha_flip）

- **作用**：持仓时 alpha **达到与开仓对称的阈值**才尝试全平：多仓须 **`alpha <= -alpha_threshold`**，空仓须 **`alpha >= alpha_threshold`**（与「多 `>=` 阈值开多、空 `<= -` 阈值开空」对称），**不是**仅符号穿越 0。  
- **与 legacy 对比**：legacy **不会在持仓内因 alpha 变号而平**，仍可继续开新腿；dynamic 会在反向**足够强**时 **提前结束**当前合并仓，后续信号再重新开仓。这是 **PnL 差异的常见主因之一**。

---

## 如何「知道是什么导致的」（建议）

当前 `Trade` 与 summary **没有**记录「本笔因何退出」（`_try_submit_full_exit` 的 reason 未写入 [Trade](strategy/backtest.py)）。要量化三种（及强平、部分减、兜底）各自贡献，需要 **轻量埋点**（任选其一）：

1. **扩展 `Trade` 或并行 list**：在 `engine.py` 每次 `_finalize_trade_chunk` / 全平成交路径写入 `exit_reason`（如 `time` / `tp` / `sl` / `alpha_flip` / `forced_flatten` / `partial_reduce` / `replay_fallback`）。  
2. **后处理近似**：仅用现有 `all_trades.csv` 按 `entry_ts_ns`/`exit_ts_ns`/股数与 alpha 时间对齐做启发式归因，**不可靠**，不推荐作为主结论。

若你确认要做 1），再在 `rules.md` 里约定 reason 枚举与报表列名即可。

---

## 不改原代码的 test / 分析脚本（你当前偏好）

**约束**：不修改 `strategy/`、`backtest.py`、`Trade` 等仓库内业务实现。

**能做**：

1. **两次 run 输出对比（推荐）**  
   - 用户分别用同一 `--date`、尽量相同成本参数，跑一遍 `main.py`（legacy）与一遍 `main_dynamic_hold.py`（或 `--strategy-mode dynamic_hold`）。  
   - 脚本只读两个 `repo_dir` 下已有产物（例如 `strategy_layer/symbol_summary.csv`、`strategy_layer/all_trades.csv`、`run_config.json`），输出：总 PnL、`n_trades`、各 `blocked_*`、`shares_unflattenable_after_1445` 等列的差分；按 `symbol` 对齐做 diff 表。  
   - **不依赖**引擎内部 reason，**零侵入**。

2. **启发式「平仓类型」分桶（弱、仅供粗看）**  
   - 仅用 `all_trades.csv` 中已有列（如 `entry_time`、`exit_time`、`shares`、`pnl` 等，以实际表头为准）：例如用持仓时长 vs `hold_min` 粗分「可能时间平仓」、用 `run_config.json` 是否含 TP/SL 与 pnl 符号做**非常粗**的猜测。  
   - **无法**可靠区分「信号反向」与「时间全平」与「强平窗口限价平」，因缺少 `exit_reason`。脚本须在输出中**明确声明**不确定性。

3. **pytest 形态**  
   - 将上述逻辑放在 `tests/test_run_output_compare.py`，用 fixture 指向两份**固定 fixture 路径**（小样本 repo 或脱敏 CSV），断言「对比脚本不抛错、diff 结构符合预期」。仍不改业务代码。

**不能做（在不改源码前提下）**：

- 对每一笔 `Trade` **精确**标注 `time` / `tp` / `sl` / `alpha_flip`；必须走上文「可选 exit_reason」改源码方案。

**交付物建议**：`scripts/compare_strategy_runs.py`（或 `tests/test_compare_legacy_dynamic_outputs.py`），CLI 形如 `--legacy-repo` `--dynamic-repo`，打印 markdown 表或 CSV 到 stdout。

---

## 小结（直接回答你的直觉）

- **差异大**：多半来自 **单仓合并 + 加减仓 + alpha 反向提前平** 与 legacy **多腿独立扛到期** 的结构不同，而不只是三种全平方式。  
- **三种平仓（时间 / 止盈止损 / 信号反向）**：其中 **信号反向** 与 **开启的 TP/SL** 最容易把平仓时刻从「到期附近」拉到盘中任意时刻，从而剧烈改变 PnL；**时间到期**在两边都有，但 **均价与仓位路径**不同导致结果仍可能差很多。
