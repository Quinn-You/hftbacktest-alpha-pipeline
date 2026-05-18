# Dynamic-hold 专用 pipeline

## 目标

在仓库根提供与 `main.py` 相同的完整编排（data → strategy → analysis，可选 index_filter），但 **CLI 默认 `strategy_mode=dynamic_hold`**，减少日常漏传 `--strategy-mode` 的失误。

## 形态

- **`main.py`**：`run_pipeline(args)` + `_build_parser(default_strategy_mode="legacy")`；默认 legacy。
- **`main_dynamic_hold.py`**：调用 `_build_parser(default_strategy_mode="dynamic_hold")` 后 `run_pipeline`；其余参数与 `main.py` 完全一致。

等价命令：

```bash
python main.py --strategy-mode dynamic_hold --date YYYY-MM-DD --run-name NAME
# 与同参
python main_dynamic_hold.py --date YYYY-MM-DD --run-name NAME
```

（在未显式传 `--strategy-mode` 时，后者默认已是 dynamic_hold。）

## 用法示例

```bash
cd /path/to/alpha_trade
python main_dynamic_hold.py --date 2024-01-02 --run-name my_dynamic_run
```

Dynamic 相关参数与 `main.py` 相同：`--alpha-adjust-step`、`--adjust-notional`、`--take-profit-pct`、`--stop-loss-pct` 等。

## 验收

- `python main_dynamic_hold.py --help` 中 `--strategy-mode` 显示 `(default: dynamic_hold)`（使用 `ArgumentDefaultsHelpFormatter`）。
- `python main.py --help` 中该项为 `(default: legacy)`。
- `python -m py_compile main.py main_dynamic_hold.py` 通过。

## 实现说明（已完成）

1. 从 `main()` 抽出 `run_pipeline(args: argparse.Namespace)`。
2. `_build_parser(default_strategy_mode: str = "legacy")` 将默认值传给 `--strategy-mode`。
3. 新增薄入口 `main_dynamic_hold.py`，从 `main` 导入 `_build_parser` 与 `run_pipeline`。
4. `ArgumentDefaultsHelpFormatter`：便于 `--help` 中直接看到 `strategy_mode` 默认值差异。
