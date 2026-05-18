#!/usr/bin/env bash
# 在项目根执行：./run_pipeline.sh
# 交易日期从 DAYS_FILE 读取（默认项目根 days.txt），每行一个 YYYY-MM-DD；
# 空行、行首 # 注释、行尾 # 注释均忽略。参数见下方「调参约定」，可用环境变量覆盖。

set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$ROOT"

# -----------------------------------------------------------------------------
# 调参约定（所有下列变量均可被环境覆盖后再执行本脚本）
#
#   - ``${VAR:-def}``：未设置或为空时用默认值（用于必传给 CLI 的参数）。
#   - ``${VAR-def}``：仅未设置时用默认值；若执行 ``VAR= ./run_pipeline.sh`` 传空字符串，
#     则保持为空（用于「空 = 不向 CLI 传该 flag，交给 Python 默认」的可选项）。
#
# 例：``THRESHOLD=0.35 NOTIONAL=50000 ./run_pipeline.sh``
#     ``TAKE_PROFIT_PCT= STOP_LOSS_PCT= ./run_pipeline.sh``  # 关闭 TP/SL（不传参）
# -----------------------------------------------------------------------------
PIPELINE="${PIPELINE:-dynamic}" # main | dynamic

# -----------------------------------------------------------------------------
# 必填 / 常用
# -----------------------------------------------------------------------------
DAYS_FILE="${DAYS_FILE:-$ROOT/days.txt}"
RUN_NAME="${RUN_NAME:-dynamic_AUM_TAKER_index1000}"

ALPHA="${ALPHA:-/data/sihang/AlphaPROBETick/alpha_monthly/mid_hivol_hiliq_mid_hivol_loliq}"
THRESHOLD="${THRESHOLD:-0.4}"
HOLD_MIN="${HOLD_MIN:-5}"
NOTIONAL="${NOTIONAL:-100000}"
ORDER_SIZE_MODE="${ORDER_SIZE_MODE:-notional}" # notional | lots
ORDER_LOTS="${ORDER_LOTS:-1}"
LOT_SIZE="${LOT_SIZE:-100}"
AGGRESSIVE_TICKS="${AGGRESSIVE_TICKS:-3}"
ENTRY_LIQ="${ENTRY_LIQ:-taker}" # maker | taker
EXIT_LIQ="${EXIT_LIQ:-taker}"   # maker | taker
STEP_NS="${STEP_NS:-5000000000}" # 5s；与 TWAP 每步一片对齐
TICK_SIZE="${TICK_SIZE:-0.01}"
ORDER_LATENCY_NS="${ORDER_LATENCY_NS:-2000000000}"
ROI_LB="${ROI_LB:-0.1}"
ROI_UB="${ROI_UB:-2000.0}"
COMMISSION_RATE="${COMMISSION_RATE:-0.00015}"
STAMP_DUTY_RATE="${STAMP_DUTY_RATE:-0.0005}"
FORCE_FLATTEN_HHMMSS="${FORCE_FLATTEN_HHMMSS:-14:45:00}"
FORCE_FLATTEN_EXTRA_TICKS="${FORCE_FLATTEN_EXTRA_TICKS:-2}"

# 留空表示不传该参数（用 Python 默认）；仅未设置时默认空
REPO_DIR="${REPO_DIR-}"
SYMBOLS="${SYMBOLS-}"
MAX_SYMBOLS="${MAX_SYMBOLS-}"

FORCE_REGENERATE="${FORCE_REGENERATE:-0}" # 1 则追加 --force-regenerate

STRATEGY_MODE="${STRATEGY_MODE-}" # 留空：dynamic 入口用默认 dynamic_hold；main 用 legacy

# dynamic_hold
ALPHA_ADJUST_STEP="${ALPHA_ADJUST_STEP:-0.01}"
ADJUST_NOTIONAL="${ADJUST_NOTIONAL:-10000}"
# 仅未设置时给默认；显式 TAKE_PROFIT_PCT= 为空则不传参（关闭）
TAKE_PROFIT_PCT="${TAKE_PROFIT_PCT-0.05}"
STOP_LOSS_PCT="${STOP_LOSS_PCT-0.03}"

INDEX_WEIGHTS="${INDEX_WEIGHTS-}"
# daily-aum>0 且未传 INDEX_WEIGHTS 时，AUM 按该 parquet 分摊；与 main.py DEFAULT_INDEX_WEIGHTS_PATH 默认一致
DEFAULT_INDEX_WEIGHTS="${DEFAULT_INDEX_WEIGHTS-/data/index_weights_1000.parquet}"
INDEX_FILTER_OUTPUT="${INDEX_FILTER_OUTPUT:-/home/haoranyou/data/index_filter}"
INDEX_LABEL="${INDEX_LABEL-}"

# AUM：仅未设置时默认 1e8；DAILY_AUM= 为空则不传 --daily-aum（关闭）
DAILY_AUM="${DAILY_AUM-100000000}"
TRADE_UNIT_CLIP_FRAC="${TRADE_UNIT_CLIP_FRAC-}"

# TWAP：仅未设置时默认 10000；TWAP_SLICE_NOTIONAL= 为空则不传（关闭 TWAP）
TWAP_SLICE_NOTIONAL="${TWAP_SLICE_NOTIONAL-20000}"

# -----------------------------------------------------------------------------
# 从 days.txt 读日期并逐日跑（一般无需改下面）
# -----------------------------------------------------------------------------
if [[ ! -f "$DAYS_FILE" ]]; then
	echo "[run_pipeline] error: days file not found: $DAYS_FILE" >&2
	exit 1
fi

dates=()
while IFS= read -r raw || [[ -n "$raw" ]]; do
	[[ "$raw" =~ ^[[:space:]]*# ]] && continue
	line="${raw%%#*}"
	line="${line//$'\r'/}"
	line="${line#"${line%%[![:space:]]*}"}"
	line="${line%"${line##*[![:space:]]}"}"
	[[ -z "$line" ]] && continue
	dates+=("$line")
done <"$DAYS_FILE"

if ((${#dates[@]} == 0)); then
	echo "[run_pipeline] error: no dates in $DAYS_FILE (after skipping blanks/comments)" >&2
	exit 1
fi

skipped_dates=()

for DATE in "${dates[@]}"; do
	if RESOLVED_ALPHA="$(
		python - "$ALPHA" "$DATE" <<'PY'
from pathlib import Path
import sys

from data.alpha_io import resolve_alpha_path

alpha = Path(sys.argv[1])
trade_date = sys.argv[2]

try:
	print(resolve_alpha_path(alpha, trade_date))
except FileNotFoundError as exc:
	print(exc, file=sys.stderr)
	sys.exit(3)
except Exception as exc:
	print(f"[run_pipeline] resolve alpha failed: {exc}", file=sys.stderr)
	sys.exit(1)
PY
	)"; then
		:
	else
		rc=$?
		if [[ $rc -eq 3 ]]; then
			echo "[run_pipeline] warning: DATE=$DATE alpha missing, skip this day" >&2
			skipped_dates+=("$DATE")
			continue
		fi
		echo "[run_pipeline] error: DATE=$DATE alpha resolve failed" >&2
		exit "$rc"
	fi

	args=(
		--date "$DATE"
		--run-name "$RUN_NAME"
		--alpha "$RESOLVED_ALPHA"
		--threshold "$THRESHOLD"
		--hold-min "$HOLD_MIN"
		--notional "$NOTIONAL"
		--order-size-mode "$ORDER_SIZE_MODE"
		--order-lots "$ORDER_LOTS"
		--lot-size "$LOT_SIZE"
		--aggressive-ticks "$AGGRESSIVE_TICKS"
		--entry-liq "$ENTRY_LIQ"
		--exit-liq "$EXIT_LIQ"
		--step-ns "$STEP_NS"
		--tick-size "$TICK_SIZE"
		--order-latency-ns "$ORDER_LATENCY_NS"
		--roi-lb "$ROI_LB"
		--roi-ub "$ROI_UB"
		--commission-rate "$COMMISSION_RATE"
		--stamp-duty-rate "$STAMP_DUTY_RATE"
		--force-flatten-hhmmss "$FORCE_FLATTEN_HHMMSS"
		--force-flatten-extra-ticks "$FORCE_FLATTEN_EXTRA_TICKS"
		--alpha-adjust-step "$ALPHA_ADJUST_STEP"
		--adjust-notional "$ADJUST_NOTIONAL"
	)

	[[ -n "$REPO_DIR" ]] && args+=(--repo-dir "$REPO_DIR")
	[[ -n "$SYMBOLS" ]] && args+=(--symbols "$SYMBOLS")
	[[ -n "$MAX_SYMBOLS" ]] && args+=(--max-symbols "$MAX_SYMBOLS")
	[[ "$FORCE_REGENERATE" == "1" ]] && args+=(--force-regenerate)

	[[ -n "$STRATEGY_MODE" ]] && args+=(--strategy-mode "$STRATEGY_MODE")

	[[ -n "$TAKE_PROFIT_PCT" ]] && args+=(--take-profit-pct "$TAKE_PROFIT_PCT")
	[[ -n "$STOP_LOSS_PCT" ]] && args+=(--stop-loss-pct "$STOP_LOSS_PCT")

	[[ -n "$INDEX_WEIGHTS" ]] && args+=(--index-weights "$INDEX_WEIGHTS")
	args+=(--default-index-weights "$DEFAULT_INDEX_WEIGHTS")
	[[ -n "$INDEX_FILTER_OUTPUT" ]] && args+=(--index-filter-output "$INDEX_FILTER_OUTPUT")
	[[ -n "$INDEX_LABEL" ]] && args+=(--index-label "$INDEX_LABEL")

	[[ -n "$DAILY_AUM" ]] && args+=(--daily-aum "$DAILY_AUM")
	[[ -n "$TRADE_UNIT_CLIP_FRAC" ]] && args+=(--trade-unit-clip-frac "$TRADE_UNIT_CLIP_FRAC")
	[[ -n "$TWAP_SLICE_NOTIONAL" ]] && args+=(--twap-slice-notional "$TWAP_SLICE_NOTIONAL")

	if [[ "$PIPELINE" == "dynamic" ]]; then
		echo "[run_pipeline] DATE=$DATE main_dynamic_hold.py ${args[*]}"
		python main_dynamic_hold.py "${args[@]}"
	else
		echo "[run_pipeline] DATE=$DATE main.py ${args[*]}"
		python main.py "${args[@]}"
	fi
done

echo "[run_pipeline] done: ${#dates[@]} day(s) from $DAYS_FILE"
if ((${#skipped_dates[@]} > 0)); then
	echo "[run_pipeline] skipped ${#skipped_dates[@]} day(s) due to missing alpha: ${skipped_dates[*]}"
fi
