# Dynamic hold 策略规则（与实现对齐）

本文描述 `dynamic_hold` 的目标行为与实现，便于你改本文件作设计备忘，再在 `strategy/dynamic_hold/engine.py`（及 `strategy/run.py`、`main.py` CLI）中落地。**第 5.3 节**起为「按步取整 × 每步名义、每点仅用当前 `delta`、允许连发同 `delta` 重复」的阶梯口径，与当前 `engine.py` 一致。**TWAP 建仓未完成时**与 alpha 的交互以 **第 3.2 节（已定稿）** 为准；细设计与验收清单见 `.cursor/plan/twap_period_alpha_handling_9c2e4b1a.plan.md`。

**实现源文件**：`strategy/dynamic_hold/engine.py`（入口函数 `run_dynamic_hold_backtest`）。**阶梯加减仓**须先满足与第 3 节一致的**方向阈值**（见第 5.2 节），再按**当前** `alpha_val` 与 **`alpha_0`** 的偏离做**按步取整**的名义与股数（见第 5.3 节）；**不与历史 delta 比较**，连续两个 alpha 点若 `delta` 相同，允许**重复**按同一 `delta` 再发一笔（见第 5.3 节末）。

---

## AUM：多空分账、仅开仓计名义（写死）

以下约定适用于「单日总 **`daily_aum`** 按权重分到各标的 **`aum`**」功能（`legacy` 与 `dynamic_hold` 共用；设计见 `.cursor/plan/aum_weights_allocation_design_5fd5f9f6.plan.md`）。

- **权重日期语义（写死）**：对交易日 **`T`**，AUM 分摊与指数过滤**必须**读取权重表中 **`date = next_trading_day_after(T)`** 的行（按权重表自身 `date` 去重升序定义“下一交易日”）。例如：`2024-01-03` **必须**使用 `2024-01-04`。该规则为**硬编码语义**，**不提供**“同日权重”或“上一交易日权重”的切换开关。
- **类型**：**双 ledger + 仅增加敞口时累计**。每标的有 **`aum`**（多头与空头**各自**累计的上限，数值相同、**两桶独立**）。维护 **`used_long`**、**`used_short`**（仅当成交使「多头股数」或「空头股数」相对成交前**严格变大**时，各自增加 **`exec_price × 增加股数`**）。
- **平仓、减仓**：**不**增加 `used_long` / `used_short`；**不因平仓而减少** ledger（无冲减；日内多次开仓可各自累加直至触及上限）。
- **约束**：**`used_long ≤ aum`** 且 **`used_short ≤ aum`**（独立）。
- **发单前裁剪**（仅针对**会增加该侧敞口**的订单）：  
  - 增加多头：`effective = min(requested, aum - used_long)`（可选 `clip_frac × (aum - used_long)`）；再整手。  
  - 增加空头：`effective = min(requested, aum - used_short)`（同上）。  
  - **平仓、全平、兜底平仓**：**不按 AUM 裁剪**（不受 `used_*` 限制）。
- **增量口径**：`used_long += exec_price × max(0, 多头股数_after − 多头股数_before)`；`used_short` 同理。**不使用** `Trade` 的 entry/exit_notional 替代。

**配置侧**：`main` 注入 **`aum_by_symbol`**；引擎内维护该标的 **`used_long`、`used_short`**。裁剪与 ledger 增量见 **`strategy.aum`**（**`strategy.aum.budget`**）；权重表与分摊见 **`tool.index_weights`**（**`calendar`** / **`allocate`**）。

---

## 1. 总体假设

- 单标的、**至多一笔合并持仓**（`DynamicPosition`）：方向 `side`（+1 多 / -1 空）、总股数 `shares`、加权均价 `avg_entry_px`、**开仓时锚定 alpha** `alpha_0`（仅首笔开仓成交时写入，之后加仓**不**改 `alpha_0`）、首笔开仓时间 `first_entry_ts_ns`、目标平仓时刻 `target_exit_ts_ns`（由首笔成交时间 + `hold_minutes`，午休 11:30–13:00 不计入，与 legacy 共用 `hold_target_ts_excluding_lunch`）。
- 订单均为 **GTC 限价**；开/加仓用 `submit_entry_order`，平用 `_submit_close_shares`。
- 可交易性：开仓方向、平仓方向均需通过 **`_is_tradable_for_side_l1_min1lot`**（对手盘**第一档**量 ≥ 1 手）；不满足则跳过下单并计 `blocked_*_l1_count`。
- 成交价与撮合由 **hftbacktest** 与 `build_hbt` 参数决定；本策略只决定**何时、何价、何量**发单。

---

## 2. 每个仿真步内的执行顺序（重要）

对每一帧（`hbt.elapse(step_ns)` 之后），大致顺序为：

1. **处理开仓/加仓挂单成交**：更新 `current`（新仓或更新均价与股数）。
2. **处理平仓挂单成交**（全平或部分平）：生成 `Trade`；部分平则减少 `shares`，**均价不变**；同步维护 `pending_exit` / `pending_exit_submit_ts` 与强平挂单标记。
3. **开仓挂单超时**：自挂单时刻起超过 **60 秒**（同 `EXIT_ORDER_STALE_NS`）仍未完全成交的开仓单，执行 best-effort 撤单并从 `pending_entry` / `pending_entry_submit_ts` 移除，防止 TWAP 或空仓门闩长期卡住。
4. **平仓挂单超时**：自挂单时刻起超过 **60 秒**（`EXIT_ORDER_STALE_NS`，见 `strategy.backtest`）仍未完全成交的平仓单，调用 **`cancel_exit_order_if_active`** 撤单并从 `pending_exit` / `pending_exit_submit_ts` 中移除（**含**此前挂出的强平全平单），以便后续可再挂全平/部分平。
5. **止盈 / 止损 / 持有到期**：仅在 `current` 存在且 **`pending_exit` 为空** 时评估；触发则尝试挂**全平**单 `_try_submit_full_exit`。
6. **日末强平窗口**（`now_ts >= force_flatten_ts_ns`）：置 `flatten_triggered`；若有仓，**先撤掉非「当前强平全平」的未成交平仓单**，再在 **`pending_exit` 为空** 时挂强平全平（仍受对手盘一档量检查，失败计 `blocked_forced_exit_l1_count`）。强平成功挂出的那一笔在成交前不会被本步撤掉，避免每仿真步误撤刚挂的强平单。
7. **Alpha 信号消费**（仅当 **`not flatten_triggered`**）：空仓开仓、持仓时**反向达阈值**全平、或在**已过方向阈值**的前提下相对 `alpha_0` 的阶梯加减仓。若 **`pending_exit` 非空**，本步**不**处理 alpha 的平仓/调仓分支（避免与未完成平仓单叠加）。若空仓但 **`pending_entry` 非空**，本步也**不**再消费新的空仓开仓 alpha（防止重复/反向叠挂开仓）。**TWAP 建仓未完成时**是否消费阶梯/反向及如何修订 TWAP 目标，以 **第 3.2 节** 为已定稿目标（与当前 `engine.py` 可能不一致直至落地）。
8. **TWAP 开仓续挂**（可选，见第 3.1–3.2 节）：若启用 TWAP 且仍有未完成的开仓目标股数、且无 `pending_entry`、未进入「只平不开」，在本步 alpha 循环**之后**再尝试挂下一片开仓限价。

---

## 3. 开仓（空仓）

- 按时间顺序消费 `alpha_records`，条件：`signal_ts <= now_ts`。
- 若 `alpha >= alpha_threshold` → 做多；`alpha <= -alpha_threshold` → 做空；否则忽略。
- 股数：`compute_order_shares(entry_ref_px, notional, lot_size, order_size_mode, order_lots)`，与 legacy 一致（`order_size_mode` 为 `notional` 或 `lots`）。
- 限价：`get_aggressive_limit_price`（`aggressive_ticks`、`tick_size`）。
- 挂单记入 `pending_entry`，成交后建立 `current`，并设 `alpha_0 =` 该信号 alpha、`target_exit_ts_ns` 自该成交时刻起算。

### 3.1 TWAP 名义切片（可选）

- **配置**：`twap_slice_notional > 0`（如 `10_000`）且 **`order_size_mode == "notional"`** 时启用；`lots` 模式不拆单。CLI：`--twap-slice-notional`；默认 `None` 关闭。
- **时间粒度**：每帧一次 `hbt.elapse(step_ns)` 至多挂**一片**；默认 `step_ns = 5s` 即与「每 5 秒一片」对齐；若改步长，则「每步一片」仍成立，日历秒数随 `step_ns` 变。
- **开仓**：信号触发的目标总股数仍按原规则（`compute_order_shares` + AUM `capped_entry_shares`）一次算定，记入 **`twap_target_total_shares`** 与 **`twap_entry_remaining_shares`**（二者初始化一致；实现上须与「目标 − 当前仓 − 在途开仓」恒等式对齐）；之后每步在无未完成开仓挂单时，按 `min(twap_slice_notional, 剩余股×参考价)` 的名义换算股数（整手、再过 AUM 裁剪）挂下一片，直至剩余股数耗尽。建仓未完成前：**不消费**新的空仓开仓 alpha（同向重复信号跳过）。**阶梯加减仓、信号反向与 TWAP 目标联动** 以 **第 3.2 节** 为已定稿目标（`engine.py` 未落地前可能与该节不一致）。
- **全平类平仓**（持有到期、TP、SL、**非** 第 3.2 节所列之全平）：每步至多平「一片」：名义上限 `twap_slice_notional` 在当前价下换算股数（整手），不超过当前持仓；未平完则后续帧在 `pending_exit` 清空后再挂下一片。阶梯里的**部分减仓**仍按第 5.5 节单笔挂出（不拆 TWAP）。**第 3.2 节**规定的 **减仓超量** 与 **信号反向** 触发的全平为 **例外**：**一次性 aggressive 限价** 平掉全部持仓，**不按** `twap_slice_notional` 拆片出仓。
- **与强平**：进入强平窗口时**放弃**未完成的开仓 TWAP（`twap_rem_sh` 与 `twap_tgt_sh` 清零），只保留平仓。

### 3.2 TWAP 期间新 alpha（已定稿）

**实现**（`strategy/dynamic_hold/engine.py`）：总目标与剩余补仓为 **`twap_tgt_sh`**、**`twap_rem_sh`**（与下文表中 `twap_target_total_shares` / `twap_entry_remaining_shares` 语义一一对应）；`pending_entry` 每条 tuple 第五元为 **`tw` | `ld` | `op`**（TWAP 片 / 阶梯加 / 非 TWAP 单笔）。

本节约定 **`twap_slice_notional` 启用且建仓尚未完成**（`twap_entry_remaining_shares > 0` 或与之等价的「TWAP 未取消」状态）时，**新到达 alpha** 与 **TWAP 总目标** 的关系。实现须维护至少 **`twap_target_total_shares`**（计划建仓总股数，可修订）与 **`twap_entry_remaining_shares`**（尚未成交、尚未挂出的补仓股数），并与合并仓 `current.shares`、在途开仓挂单股数保持恒等一致。

| 规则 | 行为摘要 |
|------|----------|
| **R1 阶梯加仓** | 仍按第 5.2–5.4 节走阶梯加仓（`pending_entry`、`"add"`）。**仅在阶梯加仓限价单成交后**增加 TWAP 目标：按该笔 **`filled_sh`** 执行 `twap_target_total_shares += filled_sh`，并同步增加 `twap_entry_remaining_shares`（**挂单瞬间不调目标**，避免在途与 TWAP 补挂 double-count）。 |
| **R2 阶梯减仓** | 仍按第 5.2–5.5 节走部分平。非超量时：在**成交后**按 `filled_sh` 下调 `twap_target_total_shares` 与 `twap_entry_remaining_shares`（下界 0，与恒等式对齐）。**超量（已定稿）**：若本 alpha 点算出的 **`reduce_sh`（减前、整手等处理之后、实际将挂出的股数）** 满足 **`reduce_sh > twap_target_total_shares`**（比较对象为**减前一刻**的当前总目标），则：**停止** TWAP 后续下单（如 `twap_entry_remaining_shares = 0` 并取消/清零 TWAP 目标语义），并对当前合并仓发起 **一次性 aggressive 限价全平**（**不按** `twap_slice_notional` 拆片出仓）。 |
| **R3 信号反向** | 仍按第 5.1 节：达阈值则全平（`alpha_flip`）。同时 **停止** TWAP 后续下单（与 R2 超量类似清零 remaining / 取消 TWAP）。**全平方式**：与 R2 超量相同，**一次性 aggressive 限价** 满仓出清，**不按** TWAP 出仓切片。 |
| **R4 与 `pending_exit` 门闩** | 与第 2 节一致：若 **`pending_exit` 非空**，本步**不**叠新的 alpha 平仓/阶梯（避免与未完成平仓单并发）；TWAP 目标修订可在无冲突时应用，或实现为平仓完成后再处理积压 alpha（与 `.cursor/plan/twap_period_alpha_handling_9c2e4b1a.plan.md` §R4 一致）。 |

**说明**：「减仓超量」**不**采用「累计减仓相对初始 TWAP 目标」判定，仅采用 **单笔 `reduce_sh` 与当前 `twap_target_total_shares`（减前）** 比较。

---

## 4. 平仓与减仓触发

以下多条**并行设计**，但受 **`pending_exit` 互斥**约束（见第 2 节）；**超过 60 秒未成交的平仓单会被撤单并移出 `pending_exit`**，强平步会撤除非强平来源的挂单（见第 2 节第 5 步）。

| 类型 | 条件 | 动作 |
|------|------|------|
| **持有到期** | `now_ts >= target_exit_ts_ns` | 全平（`_try_submit_full_exit("time")`） |
| **止盈 TP** | 见下「盯市与 TP/SL」 | 全平 |
| **止损 SL** | 同上 | 全平 |
| **信号反向** | 多仓且 `alpha <= -alpha_threshold`，或空仓且 `alpha >= alpha_threshold`（与开仓对称） | 全平（`alpha_flip`） |
| **日末强平** | `now_ts >= force_flatten_ts_ns` 起持续尝试 | 全平（受对手盘一档量检查） |
| **阶梯加减仓** | 须先满足方向阈值（第 5.2 节）；名义由第 5.3 节按步取整 | 加仓见第 5.4 节；**部分平**或全减见第 5.5 节 |
| **回放结束兜底** | `status != 0` | 用 `_forced_flatten_price` 对剩余仓强制结算一笔 `Trade`，清空仓；统计 14:45 后「平不掉」见 summary |

### 4.1 盯市价格与止盈止损

- **盯市价 `mark`**：买卖中间价 `(bid+ask)/2`；若无效则用 eventstream 内**最后一笔成交价**；再不行退回 `tick_size`（`_mid_mark`）。
- **比较基准**：持仓均价 `avg_entry_px`。
- **`take_profit_pct` / `stop_loss_pct`**：`None` 或 `<= 0` 表示**关闭**该侧。
- **多头**：止盈 `mark >= avg * (1 + tp)`；止损 `mark <= avg * (1 - sl)`。
- **空头**：止盈 `mark <= avg * (1 - tp)`；止损 `mark >= avg * (1 + sl)`。

TP/SL 与「持有到期」在同一代码块内判断，**到期优先于** TP/SL 分支（先判断时间）。

### 4.2 全平挂单 `_try_submit_full_exit`

- 若已有 `pending_exit`，直接返回（**同一时刻**不叠两笔全平；超时或强平逻辑会先清障，见第 2 节）。
- 需对手盘第一档量 ≥ 1 手；否则 `blocked_timed_exit_l1_count` 增加且**本步不平**（无追价逻辑）。
- 否则以 aggressive 限价挂当前全部 `shares` 平仓。

---

## 5. 持仓中的 Alpha：反向全平与阶梯加减仓

在 **`flatten_triggered` 为假** 且 **`pending_exit` 为空** 时，对每个已到达的 alpha 点：

### 5.1 信号反向（优先于阶梯）

- 多仓且 **`alpha_val <= -alpha_threshold`**，或空仓且 **`alpha_val >= alpha_threshold`** → **全平**（与第 3 节开仓条件对称，须达到阈值幅度，而非仅符号穿越 0）。

### 5.2 相对 `alpha_0` 的阶梯（加减仓均须先过阈值）

在计算 `delta`、判断加减仓之前，须先满足与**第 3 节开仓**一致的方向阈值；**不满足则本 alpha 点不做任何阶梯加减仓**（既不因 `delta` 加仓，也不因 `delta` 减仓）：

- **多头持仓**：仅当 **`alpha_val >= alpha_threshold`** 时，才进入下面的 `delta` 与按步名义。
- **空头持仓**：仅当 **`alpha_val <= -alpha_threshold`** 时，才进入下面的 `delta` 与按步名义。

通过上述门控后，定义**当前点**偏离（与历史 alpha **无关**，不对上一时刻的 `delta` 做差分）：

- **多头**：`delta = alpha_val - alpha_0`
- **空头**：`delta = |alpha_val| - |alpha_0|`

**说明**：信号反向全平（第 5.1 节）仍优先于本节；多仓若已 **`alpha_val <= -alpha_threshold`**，先走全平而不会进入阶梯。

### 5.3 按步取整的阶梯名义（线性：每步一档名义）

在**当前 alpha 点**上，仅用 **`delta`** 与参数 **`alpha_adjust_step`**、**`adjust_notional`** 决定**本点**阶梯调仓的**目标名义金**（再经第 5.4 / 5.5 节换成股数并受 AUM / 整手等约束）：

- **`alpha_adjust_step`**：alpha 空间上的**一步宽度**（与第 7 节 CLI `--alpha-adjust-step` 一致）。
- **`adjust_notional`**：**每一个完整步**对应的**名义金**（与第 7 节 CLI `--adjust-notional` 一致）；**不是**与 `alpha_adjust_step` 相乘得到步长，而是与「步数」相乘得到名义。

**按步取整（向下取整到整步）**：

- **加仓侧**（多头：`delta > 0` 且强化多向；空头：`delta > 0` 且强化空向——与上式 `delta` 定义一致）：若 **`delta >= alpha_adjust_step`**，则  
  `k_add = floor(delta / alpha_adjust_step)`，**本点加仓名义** `N_add = k_add × adjust_notional`。  
  若 **`delta < alpha_adjust_step`**，则 **`k_add = 0`**，本点不因加仓侧发单。
- **减仓侧**（多头：`delta < 0` 表示相对 `alpha_0` 走弱）：若 **`delta <= -alpha_adjust_step`**，则  
  `k_red = floor((-delta) / alpha_adjust_step)`，**本点减仓名义** `N_red = k_red × adjust_notional`。  
  空头对称：当 **`delta <= -alpha_adjust_step`** 时，`k_red = floor((-delta) / alpha_adjust_step)`，`N_red = k_red × adjust_notional`。  
  若 **`delta > -alpha_adjust_step`**，则 **`k_red = 0`**，本点不因减仓侧发单。

**本点互斥**：正常情形下 `delta` 不会同时满足「加仓侧 `≥ step`」与「减仓侧 `≤ -step`」；若 **`|delta| < alpha_adjust_step`**，则 **`k_add = k_red = 0`**，本点阶梯不调仓。

**与历史无关、接受连发重复**：**不**维护「上一 alpha 的 `delta`」或「已累计步数」来扣减本点名义；**每一个**到达的 alpha 点都**单独**用**当前** `alpha_val` 与**固定** `alpha_0` 算 `delta` 再算 `k_add` / `k_red`。因此若 alpha 序列上**连续两点**的 `alpha_val`（及门控后得到的 `delta`）**完全相同**，则 **`N_add` 或 `N_red` 也会相同地算两次**，策略**允许**因此连续发两笔同向阶梯单（实现上仍受 `pending_exit` 互斥、对手盘一档量、AUM 等约束）。

### 5.4 加仓股数

- 令 **`N_add`** 来自第 5.3 节；若 **`N_add ≤ 0`**，跳过。
- 用 **`N_add`** 作为名义金，**`order_size_mode="notional"`** 调用 `compute_order_shares(entry_ref_px, N_add, lot_size, "notional", order_lots)`（与 CLI 的 `--order-size-mode` / `--order-lots` **无关**；`order_lots` 在该调用中不参与 notional 分支）。
- 结果再经 **`capped_entry_shares`**（AUM、`trade_unit_clip_frac`）与整手约束；若 `< lot_size` 则跳过发单。
- 限价与挂单方式同现有实现（`get_aggressive_limit_price`、`submit_entry_order`、`pending_entry` 标记 `"add"`）。

### 5.5 减仓股数

- 令 **`N_red`** 来自第 5.3 节；若 **`N_red ≤ 0`**，跳过。
- 用 **`N_red`** 作为名义金，同样以 **`order_size_mode="notional"`** 调用 `compute_order_shares` 得到 **`step_sh`**。
- `reduce_sh = min(step_sh, current.shares)`，再向下取整到 **`lot_size` 整数倍**。
- 若取整后 `< lot_size` 且仍有持仓，则 **`reduce_sh = current.shares`**（一次性减光剩余仓，可能不足一手时由该分支处理）。
- 挂 **部分平仓** `pending_exit[oid] = ("partial", reduce_sh)`；成交后产生一笔 `Trade`，`alpha_signal` 仍为 **`alpha_0`**。

### 5.6 加仓成交后状态

- 更新 `avg_entry_px` 为加权平均；**`alpha_0` 不变**。

---

## 6. 日末「只平不开」

- 当 `now_ts >= force_flatten_ts_ns`（由 `force_flatten_hhmmss` 与交易日解析）时，`flatten_triggered = True`。
- 之后 **Alpha 消费循环整段跳过**（不再开仓、不再阶梯、不再信号反向）。
- 仍尝试在强平窗口内对未平仓位挂全平（见第 2 节第 4 步）。

---

## 7. 参数与入口对应（改策略时对照）

| 含义 | `run_dynamic_hold_backtest` 形参 | `StrategyConfig` / CLI（典型） |
|------|----------------------------------|--------------------------------|
| 开仓阈值 | `alpha_threshold` | `--threshold` |
| 持有分钟（含午休剔除） | `hold_minutes` | `--hold-min` |
| 首笔开仓规模 | `notional`, `order_size_mode`, `order_lots`, `lot_size` | `--notional`, `--order-size-mode`, `--order-lots`, `--lot-size` |
| 阶梯：每完整 alpha 步对应名义 | `adjust_notional` | `--adjust-notional`（与步数 `floor(|delta|/step)` 相乘得本点 `N_add`/`N_red`） |
| 阶梯：alpha 上一步宽 | `alpha_adjust_step` | `--alpha-adjust-step` |
| 止盈 / 止损（相对均价比例） | `take_profit_pct`, `stop_loss_pct` | `--take-profit-pct`, `--stop-loss-pct`；不传为关闭 |
| 日末强平时刻 | `force_flatten_hhmmss` | `--force-flatten-hhmmss` |
| 强平额外激进 tick | `force_flatten_extra_ticks` | `--force-flatten-extra-ticks`（仅强平窗口且 `exit_liq=taker` 生效；在 `aggressive_ticks` 基础上再加） |
| 限价偏移 | `aggressive_ticks`, `tick_size` | `--aggressive-ticks`, `--tick-size` |
| 撮合步长等 | `step_ns`, `order_latency_ns`, `roi_lb`, `roi_ub` | 同名 CLI |
| TWAP 每步名义切片（开+全平类） | `twap_slice_notional` | `--twap-slice-notional`；`None` 关闭；仅 `notional` 模式 |

**硬编码在引擎内**：`stuck_check_cutoff_ts_ns` 固定为交易日 **14:45:00**（用于回放结束兜底时的「平不掉」统计）；与 `force_flatten_hhmmss` 可独立配置。

---

## 8. 产出摘要中与风控相关的字段（节选）

- 启用 AUM 时：`symbol_summary` 含 **`aum`**、**`used_long`** / **`used_short`**（仅开仓型成交累计）、**`blocked_aum_count`**（因 AUM/整手等未发增仓单）。
- `blocked_entry_l1_count`：开仓/加仓/减仓发单前被**对手盘一档**量检查挡掉的次数（减仓分支里复用该计数器名）。
- `blocked_timed_exit_l1_count`：定时全平发单被挡。
- `blocked_forced_exit_l1_count`：强平窗口内全平发单被挡。
- `n_fallback_*_after_1445` / `shares_unflattenable_after_1445`：回放结束兜底时，若已过 14:45 且仍不满足对手盘一档可交易约束，统计「平不掉」笔数与股数。

---

## 9. 修改本 `rules.md` 之后

1. 以本节为**目标行为**，改 `engine.py` 中对应分支。  
2. 若新增/重命名参数，同步 `strategy/run.py` 的 `StrategyConfig` 与 `run_universe_strategy` 传参，以及 `main.py` / `run_pipeline.sh` 的 CLI 或变量。  
3. 与 legacy 对齐的 summary 字段若有变化，同步报表或下游消费逻辑。
