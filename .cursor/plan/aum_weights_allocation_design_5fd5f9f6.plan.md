---
name: AUM weights allocation design
overview: 每标的 `aum`（权重分摊）；多空各一条「仅开仓累计」名义 ledger。开仓成交增加对应 ledger；平仓、减仓成交不增加。约束 used_long ≤ aum 且 used_short ≤ aum（独立）。发单前按方向用 remaining 裁剪；可选 trade_unit_clip_frac；超预算未发单计 blocked_count。底仓+T+1 语义在文档层说明。
todos:
  - id: tool-weights-calendar
    content: tool/index_weights/calendar.py；index_filter 从 tool.index_weights 复用（已完成则保持）
    status: completed
  - id: tool-aum-allocate
    content: tool/index_weights/allocate.py 分摊 daily_aum → aum（已完成则保持）
    status: completed
  - id: main-cli-budget
    content: main --daily-aum / aum_by_symbol / run_config（已完成则保持）
    status: completed
  - id: strategy-dual-ledger-open-only
    content: 修订 strategy.aum + legacy + dynamic_hold：used_long / used_short 仅在「增加该方向敞口」的完全成交后 += exec_price×exec_qty；平仓/减该方向敞口不加；裁剪与 clip_frac 按方向用 remaining_long / remaining_short；summary 输出 used_long、used_short、blocked_aum_count；README/rules 与口径对齐
    status: completed
  - id: rules-aum-doc
    content: rules.md、README 与 plan 口径一致（双 ledger、仅开仓）
    status: completed
isProject: false
---

# 单日 AUM + 权重分摊（修订口径：多空分账、仅开仓计名义）

## 业务意图（文档层）

- 模拟 **有底仓**、A 股 **T+1** 下日内可回转的规模：空头侧可卖量受「底仓」约束；多头侧为 **对称建模**，对多头方向也使用同一标度 **`aum`** 作为**单独一条**累计上限（与空头累计**不共用一桶**）。
- **成交预算**：从开仓到平仓在预算上视为 **开仓计一次名义**；**平仓不再增加**任何 `used_*`。

## 命名

| 名 | 含义 |
|----|------|
| **`daily_aum`** | 单日全市场总预算（CLI `--daily-aum`） |
| **`aum`** | 单标的当日上限（`daily_aum × weight_norm`）；**多头与空头各用同一数值 `aum` 作为各自累计的上限**（两条独立不等式，不是把 `aum` 拆成两半）。 |
| **`used_long`** | 单标的当日 **仅因「增加多头敞口」的成交** 而累计的名义之和（见下「开仓判定」）。 |
| **`used_short`** | 单标的当日 **仅因「增加空头敞口」的成交** 而累计的名义之和。 |
| **`remaining_long`** | `aum - used_long` |
| **`remaining_short`** | `aum - used_short` |
| **`requested`** | 发单前该笔意图名义（未裁剪） |
| **`effective_notional`** | 对该笔所属方向：`min(requested, remaining_dir)`；可选再受 `trade_unit_clip_frac × remaining_dir` 限制 |
| **`aum_by_symbol`** | `dict[symbol, float]`，由 main 注入 |
| **`exec_price` / `exec_qty`（或 exec_shares）** | fill 成交价、成交股数 |
| **`blocked_count`（实现中可用 `blocked_aum_count`）** | 因预算裁剪导致本笔未发单（或无法凑满一手等）的次数 |

## 开仓判定（写死，实现须一致）

在 **一笔完全成交** 后，比较成交前后该标的 **多头股数**、**空头股数**（legacy：单腿 `pos.side` + `shares`；dynamic：合并仓 `side` + `shares`）：

1. **计入 `used_long`**：当且仅当 **`max(多头股数, 0)` 相对成交前严格变大**（买开、买加多；不含卖平多、不含仅买平空）。
2. **计入 `used_short`**：当且仅当 **`max(空头股数, 0)` 相对成交前严格变大**（卖开、卖加空；不含买平空、不含仅卖平多）。
3. **不计入任一侧**：仅 **减少** 某侧敞口或 **平到零** 的成交（卖平多、买平空、部分减仓等）。

单笔增量：**`+= float(exec_price) × int(该笔成交股数)`**（整笔 fill 记在「增加」的那一侧；若一笔同时涉及两侧语义歧义，以「成交后相对成交前哪一侧 max(·,0) 变大」为准，单腿引擎下不存在双边同时增加）。

> **明确不要求**：不再使用「开+平都加」的单一 `used_notional` 单调桶。

## 约束（写死）

- **`used_long ≤ aum`** 且 **`used_short ≤ aum`**（**独立**，互不挤占）。
- 发单前：  
  - 意图 **增加多头** 的订单：`effective = min(requested, remaining_long)`（再整手、再 `clip_frac` 若启用）。  
  - 意图 **增加空头** 的订单：`effective = min(requested, remaining_short)`。

## 权重与 tool / main（与旧版一致部分）

- **`daily_aum`** 按权重分到各成分股 → **`aum`**（`stock_code` 对齐、`aum_by_symbol`）。
- 权重文件默认 `/data/index_weights_300.parquet`；**日期语义**同 `tool.index_weights.calendar.load_constituents_for_date`（与 index_filter 一致）。

## 落点（修订）

| 层 | 职责 |
|----|------|
| **tool** | **`tool.index_weights`**：`calendar` + `allocate`（不变） |
| **main** | `aum_by_symbol`（不变） |
| **strategy** | 包 **`strategy.aum`**（`budget`）；引擎维护 **`used_long` / `used_short`**；**仅在开仓型 fill** 上累加；发单前按方向 **`remaining_*`** 裁剪；summary 暴露 **`used_long`、`used_short`、`blocked_aum_count`**（或等价命名） |

## CLI（不变）

- `--daily-aum`、`--index-weights`（或默认权重路径）、可选 `--trade-unit-clip-frac`。

## 边界

- 无成分或映射失败：`aum = 0` → 两侧 remaining 均为 0，**不得再开仓**（除非实现另有「无 AUM 模式」开关）。
- **legacy** / **dynamic_hold** 均须遵守同一套开仓判定与双 ledger。

## 小结（验收口径）

| 项 | 要求 |
|----|------|
| 平仓是否加 used | **否** |
| 多空 | **两条**累计：`used_long`、`used_short` |
| 上限 | **`used_long ≤ aum` 且 `used_short ≤ aum`** |
| 单笔成交增量 | **`exec_price × exec_qty`**，仅记在「增加该侧敞口」时 |
