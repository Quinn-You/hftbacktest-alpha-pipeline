---
name: TWAP 期间 alpha 交互规则
overview: 规定 **TWAP 建仓尚未完成**（存在 TWAP 目标/剩余量）时，新 alpha 触发的 **阶梯加仓 / 阶梯减仓 / 信号反向** 如何更新「TWAP 总目标」、何时 **终止 TWAP 续挂** 并 **走全平**。供后续改 `strategy/dynamic_hold/engine.py` 时对照实现与验收。
todos:
  - id: state-tw-target
    content: 在引擎中显式维护「TWAP 总目标股数」及与 twap_entry_remaining_shares 的关系（初始建仓信号写入；阶梯加/减按规则修订）
    status: pending
  - id: alpha-ladder-add-bumps-target
    content: TWAP 期间阶梯加仓：仅在加仓单成交后增加 twap_target_total_shares / remaining（成交后调目标）
    status: pending
  - id: alpha-ladder-red-reduce-target
    content: TWAP 期间阶梯减仓：减少总目标；超量判定为单笔 reduce_sh > 当前 twap_target_total_shares 时停 TWAP + 一次性全平
    status: pending
  - id: alpha-flip-stops-twap-and-exit
    content: TWAP 期间信号反向超阈值：twap_entry_remaining 清零（或等价停止续挂）+ _try_submit_full_exit(alpha_flip)
    status: pending
  - id: doc-rules-md
    content: 将摘要同步到 rules.md 或 dynamic_hold 相关说明（与实现一并提交）
    status: completed
isProject: false
---

# TWAP 期间新 alpha 处理规则（设计稿）

## 1. 背景与术语

- **TWAP 建仓**：`twap_by_notional_enabled` 为真时，首笔开仓名义拆成多片限价单；引擎用 `twap_entry_remaining_shares`、`twap_entry_side`、`twap_entry_alpha` 等驱动 `_try_submit_twap_entry_continue()`。
- **TWAP 期间**：`twap_entry_remaining_shares > 0`（或实现上与之等价的「TWAP 未宣告结束」标志）。**有仓**后仍可能处于该期间（边有合并仓、边继续挂 TWAP 片）。
- **阶梯调仓**：相对 `current.alpha_0` 的 `delta` → `k_add` / `k_red`，名义 `k × adjust_notional`，加仓走 `pending_entry`（`add`），减仓走 `pending_exit`（`partial`）。
- **信号反向**：与开仓对称，多仓 `alpha <= -alpha_threshold`、空仓 `alpha >= alpha_threshold` 时 **全平**（现有 `alpha_flip` 语义）。

当前代码在有仓且 `twap_entry_remaining_shares > 0` 时 **整段跳过** alpha（含阶梯与反向），与本计划 **冲突**；实现本计划时需删除或替换该门闩，并按下列规则分支。

---

## 2. 需新增或对齐的状态（实现前提）

为避免「总目标」与「剩余未挂出股数」语义混淆，建议显式区分：

| 状态量（建议名） | 含义 |
|------------------|------|
| `twap_target_total_shares` | TWAP 计划建仓的 **总目标股数**（可随阶梯加/减修订）。 |
| `twap_entry_remaining_shares` | 距离目标仍 **未成交、未挂出** 的剩余股数（与现有字段对齐：成交一片则减少）。 |

初始化：首笔信号决定目标 `T` 后，设 `twap_target_total_shares = T`，`twap_entry_remaining_shares = T`（或按现有逻辑等价：首片挂出后剩余为 `T - 已挂`/`已成交`，与现实现一致即可）。

**阶梯加仓**（增加目标）：**已决议 — 仅在加仓限价单成交后**调目标：按该笔成交股数 `filled_sh` 令  
`twap_target_total_shares += filled_sh`，并同步增加 `twap_entry_remaining_shares`（与「目标 − 当前仓 − 在途」恒等式对齐；挂单未成交前不调目标，避免在途 double-count）。

**阶梯减仓**（减少目标）：令  
`twap_target_total_shares -= Δ_red`（下界 0）；`twap_entry_remaining_shares` 同步减少，**不得为负**（负则裁 0 并触发下面「超量」分支）。

若暂时不想拆字段，也可用 **单变量 + 注释** 推导目标，但验收时容易歧义，**推荐双变量**。

---

## 3. 规则汇总（产品）

### R1 — TWAP 期间：阶梯 **加仓**

- **行为**：照常走阶梯加仓路径（挂单 / 成交后与现有一致合并均价、AUM ledger）。
- **对 TWAP 的影响**：**增加 TWAP 总目标股数**（见 §2），使后续 `_try_submit_twap_entry_continue` 仍会继续补挂，直到 **新的** 总目标被「当前仓 + 已成交 + 在途开仓」填满或 remaining 归零。
- **并发**：若与「仅允许一条开仓挂单」冲突，需在实现里约定优先级（例如：阶梯单与 TWAP 片串行，或允许多 `pending_entry` —— 与撮合器假设一致即可）。

### R2 — TWAP 期间：阶梯 **减仓**

- **行为**：照常走阶梯减仓（部分平，`pending_exit`），成交后缩仓、均价规则与现有一致。
- **对 TWAP 的影响**：**减少 TWAP 总目标股数**（§2）；`twap_entry_remaining_shares` 同步下调。
- **超量（关键）**：**已决议 — 单笔判定、相对当前目标**：若本 alpha 点算出的 **`reduce_sh > twap_target_total_shares`（减前一刻的当前总目标）**，则：
  1. **停止 TWAP 后续下单**：`twap_entry_remaining_shares = 0`（并清 `twap_target_total_shares` 或标记 TWAP **已取消**）；
  2. **进入平仓**：对 **当前合并仓** 发起 **全平**；**已决议**：与 R3 一致，采用 **一次性 aggressive 限价全平**（不走 TWAP 出仓切片），见 §7。

> 注：非超量时，减仓仍照常挂 `partial`；**目标下调**可在成交后按 `filled_sh` 与 §2 对齐。超量时 **结果语义**：TWAP 停 + 一次性全平。

### R3 — TWAP 期间：信号 **反向**（超阈值）

- **行为**：与现有一致 —— **全平**（`alpha_flip`）。
- **对 TWAP 的影响**：**立即停止 TWAP 后续下单**（`twap_entry_remaining_shares = 0`，TWAP 目标标记取消），不再调用续挂逻辑；全平流程优先于继续补 TWAP。

### R4 — 与 `pending_exit` 门闩的关系

现有逻辑：**存在未完成平仓单**时，alpha 阶梯/反向可能被 `pending_exit` 整块跳过。TWAP 期间若 R2/R3 触发全平，会进入 `pending_exit`；需在计划中约定：

- **要么**：全平意图与阶梯减仓挂单 **队列化**（先完成当前 `pending_exit` 再处理新 alpha）—— 与现状一致；
- **要么**：R2 超量 / R3 允许 **插队** 撤旧挂新 —— 需改引擎，超出本设计稿默认范围。

**默认建议**：保持 **不在有 `pending_exit` 时叠新 alpha 平仓/阶梯**（与模块头注释一致），仅 **修订 TWAP 目标与 remaining** 可在「无冲突」时提交；若 alpha 在 `pending_exit` 期间到达，**延迟到平仓完成** 后再应用 R1–R3（实现为「待处理队列」或简单「下帧再算」）。

---

## 4. 与方案 A 的关系

- **方案 A（仅删 `twap_entry_remaining > 0` 的 continue）**：只解决「阶梯不被无脑跳过」，**不**包含「调 TWAP 总量 / 超量全停 / 反向停 TWAP」。
- **本计划**：在方案 A 之上，为 TWAP 期间 alpha 增加 **显式状态机 + R1–R4**，实现后再跑回归日与 legacy/dynamic 对比。

---

## 5. 验收清单（建议）

1. TWAP 进行中触发 **阶梯加仓** → 总目标上升，后续仍见 TWAP 片（或 documented 串行行为），最终仓量不低于原 TWAP 目标 + 加仓量（受 AUM/一手约束除外）。
2. TWAP 进行中 **阶梯减仓** 未超量 → 目标下降，TWAP 可能在仓已够时自然停 remaining。
3. TWAP 进行中 **阶梯减仓超量** → 不再挂 TWAP 片，且触发 **全平**（或等价清仓路径），无隔夜仓。
4. TWAP 进行中 **alpha 反向超阈值** → TWAP 停 + **全平**（与现 `alpha_flip` 一致）。
5. `flatten_triggered` / 强平窗口下，仍以现有「只平不开」为准，TWAP 不再续挂。

---

## 6. 背景说明（问题 1、2 的两种口径；**已定稿见 §7**）

### 6.1 加仓调目标：挂单瞬间 vs 成交后再调？

你在调的是两个数：**总目标** `twap_target_total_shares` 和 **还要补多少** `twap_entry_remaining_shares`。阶梯加仓会再挂一张 **买/卖开仓单**（股数记为 `step_sh`），这张单在一段时间内处于 **在途**（已挂出、可能未完全成交）。

- **挂单瞬间就把目标加上去**  
  - 含义：一调用 `submit_entry_order(step_sh)`，就立刻执行  
    `twap_target_total_shares += step_sh`，并且通常也要让 **remaining** 多出「还要通过 TWAP 补挂」的那一份（与现有「目标 − 当前仓 − 在途」恒等式对齐）。  
  - 直觉：**计划**里已经承认「要多建这么多仓」，后续 TWAP 续挂会按新目标继续拆片。  
  - 风险：若订单 **撤单 / 长期不成交 / 部分成交**，你要么在撤单时把目标 **再减回去**，要么 remaining 会 **double-count**（同一笔在途既占了「加仓单」又占了「TWAP 还要买的量」）—— 所以实现上必须和 **成交回报、撤单** 同一套 bookkeeping。

- **成交后再把目标加上去**  
  - 含义：只有 `is_order_filled`（或实际成交股数 `filled_sh`）落袋后，才  
    `twap_target_total_shares += filled_sh`，并同步调 remaining。  
  - 直觉：**没成交就不算**你多要了 TWAP 目标。  
  - 好处：**不会在途 double-count**（在挂单未成交前，TWAP 目标不变）。  
  - 代价：从发信号到成交之间，**续挂的 TWAP 片仍按旧目标算**；若你希望「一点加仓信号就立刻多挂 TWAP」，会晚半拍。

**一句话**：挂单瞬间调 = 跟「意图」同步，但要小心在途与撤单；成交后调 = 跟「实仓」同步，不会重复计数，但反应晚一拍。**工程上更省心的是「成交后再调目标」**；若选挂单瞬间，必须在 plan 实现里写清撤单/部分成交如何回滚目标。

---

### 6.2「减仓超过 TWAP 总量」：单笔还是累计？相对谁？

这里的「TWAP 总量」指你维护的 **`twap_target_total_shares`（当前修订后的总目标）**，不是「初始信号那一笔」除非你永远不改它。

- **按单笔 `reduce_sh` 判定**  
  - 含义：**这一笔**阶梯要减的股数，若 **大于此刻的 `twap_target_total_shares`**（或你规定的「仍归在 TWAP 名下的可减上限」），就触发 **停 TWAP + 全平**。  
  - 例子：当前 TWAP 目标只剩 1000 股概念上的「量」，这一笔阶梯算出来要减 3000 股 → **单笔就超** → 触发。  
  - 特点：简单、**与历史减了多少无关**，只看这一锤子是否「大过当前 TWAP 目标」。

- **按累计减仓相对「初始 TWAP 目标」判定**  
  - 含义：从建仓开始，把所有阶梯减仓（或所有减仓）**加起来**，和 **第一次**设 TWAP 时的 `T_initial` 比；若 **累计减仓 > T_initial**（或类似不等式）就触发。  
  - 例子：初始 TWAP 目标 5000；已分三笔累计减了 4500；这一笔再减 800 → 累计 5300 > 5000 → 触发。  
  - 特点：**和「一开始打算建多少」绑定**；中间若 R1 加过仓抬了目标，还要约定累计是和 **当前目标** 比还是和 **初始** 比，否则会绕。

**一句话**：**单笔** = 看「这一刀是不是大过（当前）TWAP 目标」；**累计** = 看「从头到尾减的总量是不是大过（初始或当前）目标」。两者语义不同，选错验收会对不上。

---

## 7. 已决议与设计补充

1. **加仓调目标（问题 1）**：**成交后**再增加 `twap_target_total_shares` 与 `twap_entry_remaining_shares`（见 §2）。
2. **减仓超量判定（问题 2）**：**单笔** —— 本点 **`reduce_sh > twap_target_total_shares`（当前、减前）** 即触发 R2 超量分支；**不**采用「累计减仓相对初始 TWAP 目标」判定。
3. **全平方式（问题 3）**：TWAP 期间因 **R2 超量** 或 **R3 反向** 触发的 **全平**，采用 **一次性 aggressive 限价** 全平（与 `_compute_exit_close_shares_this_step` 在 **非** `_use_twap` 时「一次满仓股数」语义一致；**不按** `twap_slice_notional` 拆片出仓）。若 `_try_submit_full_exit` 在 `_use_twap` 下仍走切片，实现时需为上述路径增加 **强制非 TWAP 出仓** 分支或参数。

**与 `rules.md` 同步**：以上三条及 R1–R4 摘要见仓库根目录 **`rules.md` 第 3.2 节**。

---

## 8. 代码锚点（当前仓库）

- TWAP 续挂：[`strategy/dynamic_hold/engine.py`](../strategy/dynamic_hold/engine.py) 内 `_try_submit_twap_entry_continue`（`pending_entry` 早退）。
- 有仓 alpha 门闩：`assert current` 后 `if twap_entry_remaining_shares > 0: continue`（待替换为本计划分支）。
- 反向全平：`_try_submit_full_exit("alpha_flip")`（全平方式见 §7：一次性 aggressive，非 TWAP 出仓切片）。
- 阶梯：`k_add` / `k_red` 块与 `pending_entry` / `pending_exit`。
