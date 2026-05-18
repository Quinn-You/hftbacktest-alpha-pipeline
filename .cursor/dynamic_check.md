# Dynamic Hold Pipeline 逻辑检查清单

本文档用于回归验证 `dynamic_hold` 策略的完整流程与边界保护。每次重大修改后或发布前执行。

**适用范围**：`strategy/dynamic_hold/engine.py` + `rules.md` §2–§3.2 + AUM/TWAP 交互。

---

## 1. 数据准备层（data/universe.py + run_pipeline.sh）

- [ ] `resolve_alpha_path` 能正确处理 parquet 文件和 `alpha_monthly/` 目录
- [ ] 缺失 alpha 文件时，`run_pipeline.sh` 打印 warning 并跳过该日期（不中断整批）
- [ ] eventstream 生成失败的 symbol 记入 `skipped_symbols.csv`，不影响其他 symbol
- [ ] `max-symbols`、`symbols` 参数生效
- [ ] `force-regenerate` 能强制重刷 eventstream

---

## 2. 策略主循环基本流程（engine.py）

- [ ] `while True: hbt.elapse(step_ns)` 正常推进，`status != 0` 时退出
- [ ] `pending_entry` 成交 → 更新 `current`（新建或加权均价）、`used_long/short`、TWAP 目标
- [ ] `pending_exit` 成交 → 生成 `Trade`、更新 `current.shares`、清零时重置 TWAP 目标
- [ ] `exit_oid_traded` 防止同一平仓 oid 重复记 Trade
- [ ] 每帧按顺序：成交处理 → 超时撤单 → 盯市/TP/SL/到期 → 强平窗口 → alpha 消费 → TWAP 续挂

---

## 3. 挂单生命周期与超时保护（本次修改重点）

- [ ] **开仓单超时**：`pending_entry_submit_ts` + 60s（`EXIT_ORDER_STALE_NS`）未成交 → best-effort 撤单 + 从 `pending_entry`/`pending_entry_submit_ts` 移除
- [ ] **平仓单超时**：`pending_exit_submit_ts` + 60s → `cancel_exit_order_if_active` + 移除
- [ ] 强平来源的 `forced_flatten_order_id` 不被误撤
- [ ] 超时后 `pending_entry` 清空，空仓可继续消费 alpha，TWAP 可继续挂片

---

## 4. 空仓阶段门闩（本次修改重点）

- [ ] `current is None` 且 `pending_entry` 非空 → `continue`，不再消费新的空仓开仓 alpha
- [ ] 防止重复 `initial` 开仓或反向叠挂
- [ ] 门闩仅在空仓时生效，持仓阶段不受影响

---

## 5. TWAP + Alpha 交互（rules.md §3.2 R1–R4）

- [ ] R1（阶梯加）：`ld` 成交后才增 `twap_tgt_sh`，不改 `twap_rem_sh`
- [ ] R2（阶梯减超量）：`reduce_sh > twap_tgt_sh`（减前比较）→ 清 TWAP 目标 + oneshot aggressive 全平
- [ ] R3（信号反向）：清 TWAP 目标 + oneshot aggressive 全平
- [ ] R4（pending_exit 门闩）：`pending_exit` 非空时不消费 alpha，TWAP 目标修订延后
- [ ] `twap_clear_tgt_if_idle` 在无 tw/ld 在途且 `twap_rem_sh == 0` 时清零 `twap_tgt_sh`
- [ ] TWAP 续挂仅在 `not pending_entry` 且 `twap_rem_sh > 0` 时执行

---

## 6. AUM 约束

- [ ] `capped_entry_shares` 在开仓/加仓/TWAP 续挂前正确裁剪（`min(requested, aum - used_open)`）
- [ ] `open_ledger_increments` 仅在成交后按 `max(0, 增加股数)` 累加 `used_long/short`
- [ ] 平仓/减仓不冲减 ledger，不受 AUM 限制
- [ ] 裁剪后 `< lot_size` 跳过并计 `blocked_aum_count`
- [ ] `aum=0` 或极小值时几乎所有开仓被挡，流程不卡死

---

## 7. 强平窗口与兜底

- [ ] `now_ts >= force_flatten_ts_ns` → `flatten_triggered=True`，清 TWAP 目标，撤非强平平仓单，重挂强平全平
- [ ] `flatten_triggered=True` 后 alpha 消费分支被挡住（`while (not flatten_triggered)`）
- [ ] 晚到的 `pending_entry` 成交后建立 `current`，下一帧强平逻辑会挂全平
- [ ] `status != 0`（回放结束）时用 `_forced_flatten_price` 兜底剩余 `current`，生成最终 Trade 并统计 `unflattenable_after_1445`
- [ ] 强平窗口不主动撤 `pending_entry`（已知中风险，但不卡流程、不留仓）

---

## 8. 边界与异常场景

- [ ] 空 alpha（`alpha_records == []`）→ 直接返回空 trades
- [ ] symbol 失败 → 记 `strategy_failures.csv`，不中断 pipeline
- [ ] `step_ns` 极小/极大 → 流程可运行（用户自担配置风险）
- [ ] `twap_slice_notional` 极小 → 每片 `< lot_size` 被截断，`twap_rem_sh` 最终兜底清零
- [ ] `hold_minutes` 很小 → 开仓后立即到期触发全平
- [ ] 盘口一档量不足（`_is_tradable_for_side_l1_min1lot` 失败）→ 计 `blocked_*_l1_count`，不发单
- [ ] 强平阶段盘口仍不足 → 计 `blocked_forced_exit_l1_count`，最终兜底

---

## 9. 文档与代码一致性

- [ ] `rules.md` §2 “每步执行顺序” 中包含开仓/平仓 60s 超时撤单 + 空仓 `pending_entry` 门闩描述
- [ ] `rules.md` §3.2 表格与 `engine.py` 中 `twap_tgt_sh`/`twap_rem_sh` 修订逻辑一致
- [ ] `README.md` 提及 `dynamic_hold` + TWAP + AUM 行为
- [ ] 修改后重新运行 smoke 测试（`--max-symbols 8`）验证无崩溃

---

## 10. 回归执行建议

1. 运行 `bash run_pipeline.sh`（或单日 `main_dynamic_hold.py`）至少 1 个完整交易日
2. 检查 `strategy_layer/` 下：
   - `symbol_summary.csv` 无异常 NaN/负数
   - `all_trades.csv` 每笔有 entry/exit 时间、pnl
   - `strategy_failures.csv` 为空或仅记录预期跳过
   - `aum_allocation.parquet`（若启用 AUM）权重和 >0
3. 检查 `blocked_*_count`、`n_unflattenable_after_1445` 合理（非异常堆积）
4. 人工 spot check 1–2 只 symbol 的 `trades/*.csv`，确认 TWAP 片数、阶梯加减、反向全平逻辑

---

**维护说明**：本清单随 `engine.py` 或 `rules.md` 重大变更同步更新。每次 checklist 执行结果可追加到对应日期的 `run_config.json` 或单独记录。