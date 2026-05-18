import numpy as np
import pandas as pd
import matplotlib.pyplot as plt
from numba import njit, uint64, float64
from numba.typed import Dict
from hftbacktest import GTC, LIMIT
from hftbacktest import BacktestAsset, ROIVectorMarketDepthBacktest, Recorder

# 回放数据路径，换标的时优先改这里
DATA_PATH = "/home/haoranyou/data/eventstream_szse/000858_2026-03-25.npz"

# 时间常量，单位都是纳秒
DAY_NS = 24 * 60 * 60 * 1_000_000_000
MINUTE_NS = 60 * 1_000_000_000
TRADING_SESSION_NS = 4 * 60 * MINUTE_NS

# 分析收益用的时间点
target_time = pd.to_datetime("2026-03-25 14:57:00")

# 订单单边延迟，决定撤单和停止发单需要预留的安全边界
ORDER_LATENCY_NS = 50_000_000
SESSION_CUTOFF_BUFFER_NS = 2 * ORDER_LATENCY_NS

# 交易时间窗口设置
MORNING_GRID_START_NS = (9 * 60 + 45) * MINUTE_NS
MORNING_END_NS = (11 * 60 + 30) * MINUTE_NS
AFTERNOON_START_NS = 13 * 60 * MINUTE_NS
AFTERNOON_CLOSEOUT_START_NS = (14 * 60 + 45) * MINUTE_NS
AFTERNOON_END_METHOD3_NS = (14 * 60 + 57) * MINUTE_NS


# 构造回测资产：手续费、延迟、tick、lot 等基础市场配置
def build_asset(data_path=DATA_PATH):
    return (
        BacktestAsset()
        .data(data_path)
        .linear_asset(1.0)
        .constant_order_latency(50_000_000, 50_000_000)
        .power_prob_queue_model(2.0)
        .trading_value_fee_model(0.0003, 0.0013)
        .tick_size(0.01)
        .lot_size(100)
        .roi_lb(50.0)
        .roi_ub(150.0)
    )


# 回测结果可视化：展示仓位曲线和权益曲线
def plot_intraday(record, title):
    df = pd.DataFrame.from_records(record)
    df["timestamp"] = pd.to_datetime(df["timestamp"], unit="ns")
    df["mark_price"] = pd.Series(df["price"]).ffill().bfill()
    df["equity_wo_fee"] = df["balance"] + df["position"] * df["mark_price"]
    df["equity"] = df["equity_wo_fee"] - df["fee"]

    _, axes = plt.subplots(2, 1, figsize=(14, 8), sharex=True)
    axes[0].plot(df["timestamp"], df["position"], lw=1.2, color="#0f766e")
    axes[0].set_title(f"{title} - Net Position")
    axes[0].set_ylabel("Net position (shares)")
    axes[0].grid(alpha=0.3)

    axes[1].plot(
        df["timestamp"],
        df["equity"],
        lw=1.2,
        label="Equity incl. fees (CNY)",
        color="#1d4ed8",
    )
    axes[1].plot(
        df["timestamp"],
        df["equity_wo_fee"],
        lw=1.2,
        label="Equity excl. fees (CNY)",
        color="#dc2626",
        alpha=0.85,
    )
    axes[1].set_title(f"{title} - Account Equity")
    axes[1].set_ylabel("Account equity (CNY)")
    axes[1].set_xlabel("Time")
    axes[1].grid(alpha=0.3)
    axes[1].legend(loc="upper left")

    plt.xticks(rotation=20)
    plt.tight_layout()
    plt.show()
    return df


# 风险指标补充：按 initial_notional 作为资金基准计算 Sharpe 和百分比回撤
def add_risk_metrics(result, initial_notional, recorder_interval_ns):
    result = result.copy()
    result["sharpe_from_initial_notional"] = np.nan
    result["max_drawdown_pct_from_initial_notional"] = np.nan

    if initial_notional <= 0.0 or result.empty:
        return result

    pnl_return = result["equity"].diff().fillna(0.0) / initial_notional
    expanding_mean = pnl_return.expanding(min_periods=2).mean()
    expanding_std = pnl_return.expanding(min_periods=2).std(ddof=0)
    periods_per_day = max(TRADING_SESSION_NS / recorder_interval_ns, 1.0)
    annualizer = 1   #不需要年化因子，期间夏普就行了
    sharpe = np.where(expanding_std > 0.0, expanding_mean / expanding_std * annualizer, np.nan)

    equity_curve = initial_notional + result["equity"]
    running_peak = equity_curve.cummax()
    drawdown_pct = ((running_peak - equity_curve) / running_peak).where(running_peak > 0.0, np.nan)

    result["sharpe_from_initial_notional"] = sharpe
    result["max_drawdown_pct_from_initial_notional"] = drawdown_pct.cummax()
    return result


# 成交时间摘要：快速看首笔、末笔和成交次数
def summarize_trade_window(record, title):
    df = pd.DataFrame.from_records(record)
    df["timestamp"] = pd.to_datetime(df["timestamp"], unit="ns")
    trade_mask = df["num_trades"].diff().fillna(df["num_trades"]).gt(0)
    trades = df.loc[trade_mask, ["timestamp", "position", "num_trades"]]
    if trades.empty:
        print(f"{title}: no fills")
        return trades
    print(
        f"{title}: first fill = {trades['timestamp'].iloc[0]}, last fill = {trades['timestamp'].iloc[-1]}, fills = {len(trades)}"
    )
    return trades


# 时间辅助函数：把交易所时钟判断单独拆出来，便于 numba 编译
@njit
def china_clock_ns(timestamp):
    return timestamp % DAY_NS


@njit
def in_time_window(clock_ns, start_ns, end_ns):
    return start_ns <= clock_ns < end_ns

@njit
def is_trading_session(clock_ns, afternoon_end_ns, cutoff_buffer_ns):
    morning_cutoff = MORNING_END_NS - cutoff_buffer_ns
    afternoon_cutoff = afternoon_end_ns - cutoff_buffer_ns
    in_morning = in_time_window(clock_ns, MORNING_GRID_START_NS, morning_cutoff)
    in_afternoon = in_time_window(clock_ns, AFTERNOON_START_NS, afternoon_cutoff)
    return in_morning or in_afternoon

@njit
def should_force_cancel(clock_ns, afternoon_end_ns, cutoff_buffer_ns):
    in_lunch_break = MORNING_END_NS <= clock_ns < AFTERNOON_START_NS
    in_morning_cutoff = MORNING_END_NS - cutoff_buffer_ns <= clock_ns < MORNING_END_NS
    in_afternoon_cutoff = afternoon_end_ns - cutoff_buffer_ns <= clock_ns < afternoon_end_ns
    after_close = clock_ns >= afternoon_end_ns
    before_open = clock_ns < MORNING_GRID_START_NS
    return before_open or in_morning_cutoff or in_lunch_break or in_afternoon_cutoff or after_close

# bps 辅助函数：把 bps 换成绝对价格偏移
@njit
def bps_to_absolute_offset(price, bps):
    return price * bps * 1e-4


# 策略主逻辑：处理建仓、常规网格、尾盘收敛和撤单控制
@njit
def run_grid_strategy(
    hbt,
    recorder,
    initial_notional,    # 目标净仓对应的名义（人民币）；0 表示目标净仓为 0，初始持仓为 0，可双向交易
    base_skew,           # 常规 skew 强度；越大越会把报价往目标仓位方向推
    closeout_skew,       # 尾盘强平阶段使用的 skew 强度
    afternoon_end_ns,    # 下午交易截止时间，示例默认 14:57
    closeout_start_ns,   # 尾盘强 skew 开始时间
    closeout_end_ns,     # 尾盘强 skew 结束时间
    grid_num=8,          # 单边挂单层数；越大挂单越密、覆盖价格范围越宽
    order_lots=2,        # 每笔订单手数；1 手 = 100 股
    inventory_span_lots=40,  # 允许围绕目标仓位波动的库存带宽，单位手
    half_spread_ticks=0,     # tick 模式下，报价相对中间价偏移的半边 spread，单位 tick
    grid_step_ticks=1,       # tick 模式下，相邻两层网格价差，单位 tick
    use_bps=False,           # True 表示用 bps 控制 spread 和 interval；False 表示沿用 tick
    half_spread_bps=0.0,     # bps 模式下的半边 spread，1 bps = 0.01%
    grid_step_bps=0.0,       # bps 模式下的相邻网格间距，1 bps = 0.01%
    step_ns=100_000_000,     # 策略刷新频率；越小撤挂越勤，成交和噪声都会上升
    cutoff_buffer_ns=SESSION_CUTOFF_BUFFER_NS,  # 收盘/午休前的安全撤单缓冲，防止越界成交
):
    asset_no = 0
    closeout_buy_order_id = uint64(9_000_000_002)
    closeout_sell_order_id = uint64(9_000_000_003)
    initial_inventory = 0.0
    inventory_initialized = initial_notional <= 0.0

    while hbt.elapse(step_ns) == 0:
        hbt.clear_inactive_orders(asset_no)

        depth = hbt.depth(asset_no)
        orders = hbt.orders(asset_no)
        position = hbt.position(asset_no)
        best_bid = depth.best_bid
        best_ask = depth.best_ask

        if not np.isfinite(best_bid) or not np.isfinite(best_ask) or best_bid <= 0.0 or best_ask <= 0.0:
            recorder.record(hbt)
            continue

        tick_size = depth.tick_size
        lot_size = depth.lot_size
        order_qty = order_lots * lot_size
        inventory_span = inventory_span_lots * lot_size
        aggressive_offset_ticks = 20.0
        mid_price = 0.5 * (best_bid + best_ask)
        if use_bps:
            half_spread = max(bps_to_absolute_offset(mid_price, half_spread_bps), 0.0)
            grid_interval = max(bps_to_absolute_offset(mid_price, grid_step_bps), tick_size)
        else:
            half_spread = max(half_spread_ticks, 0) * tick_size
            grid_interval = max(grid_step_ticks * tick_size, tick_size)
        clock_ns = china_clock_ns(hbt.current_timestamp)

        if not inventory_initialized:
            initial_inventory = max(np.round(initial_notional / mid_price / lot_size), 1.0) * lot_size
            inventory_initialized = True

        desired_inventory = initial_inventory
        skew = base_skew
        in_closeout_window = (
            closeout_end_ns > closeout_start_ns
            and closeout_start_ns <= clock_ns < closeout_end_ns
            and initial_inventory > 0.0
        )
        if in_closeout_window:
            # 方法3的目标不是清仓，而是在尾盘回到初始 100 万底仓。
            desired_inventory = initial_inventory
            skew = closeout_skew

        if should_force_cancel(clock_ns, afternoon_end_ns, cutoff_buffer_ns):
            order_values = orders.values()
            while order_values.has_next():
                order = order_values.get()
                if order.cancellable:
                    hbt.cancel(asset_no, order.order_id, False)
            recorder.record(hbt)
            continue

        if in_closeout_window and np.abs(position - desired_inventory) >= lot_size:
            order_values = orders.values()
            while order_values.has_next():
                order = order_values.get()
                if order.cancellable and order.order_id != closeout_buy_order_id and order.order_id != closeout_sell_order_id:
                    hbt.cancel(asset_no, order.order_id, False)

            inventory_gap = desired_inventory - position

            if inventory_gap > 0.0:
                closeout_buy_qty = np.floor(inventory_gap / lot_size) * lot_size
                if closeout_buy_qty >= lot_size:
                    aggressive_buy = best_ask + aggressive_offset_ticks * tick_size
                    if closeout_buy_order_id not in orders:
                        hbt.submit_buy_order(
                            asset_no,
                            closeout_buy_order_id,
                            aggressive_buy,
                            closeout_buy_qty,
                            GTC,
                            LIMIT,
                            False,
                        )
            else:
                closeout_sell_qty = np.floor((-inventory_gap) / lot_size) * lot_size
                outstanding_sell_qty = 0.0
                closeout_orders = orders.values()
                while closeout_orders.has_next():
                    existing_order = closeout_orders.get()
                    if existing_order.side == -1:
                        outstanding_sell_qty += existing_order.qty
                sell_headroom = max(position - outstanding_sell_qty, 0.0)
                closeout_sell_qty = min(closeout_sell_qty, np.floor(sell_headroom / lot_size) * lot_size)
                if closeout_sell_qty >= lot_size:
                    aggressive_sell = max(best_bid - aggressive_offset_ticks * tick_size, tick_size)
                    if closeout_sell_order_id not in orders:
                        hbt.submit_sell_order(
                            asset_no,
                            closeout_sell_order_id,
                            aggressive_sell,
                            closeout_sell_qty,
                            GTC,
                            LIMIT,
                            False,
                        )
            recorder.record(hbt)
            continue

        active_session = is_trading_session(clock_ns, afternoon_end_ns, cutoff_buffer_ns)
        if not active_session:
            recorder.record(hbt)
            continue

        deviation_in_lots = (position - desired_inventory) / max(lot_size, 1.0)
        reservation_price = mid_price - skew * tick_size * deviation_in_lots

        bid_price = min(reservation_price - half_spread, best_bid)
        ask_price = max(reservation_price + half_spread, best_ask)
        bid_price = np.floor(bid_price / grid_interval) * grid_interval
        ask_price = np.ceil(ask_price / grid_interval) * grid_interval

        # 相对目标净仓对称带宽；空头不超过 desired - span，多头不超过 desired + span（与 AUM 裁剪在 pipeline 层叠加）
        upper_position = desired_inventory + inventory_span
        lower_position = desired_inventory - inventory_span

        new_bid_orders = Dict.empty(key_type=uint64, value_type=float64)
        if position < upper_position and np.isfinite(bid_price):
            current_bid = bid_price
            for _ in range(grid_num):
                bid_tick = uint64(np.round(current_bid / tick_size))
                new_bid_orders[bid_tick] = current_bid
                current_bid -= grid_interval

        new_ask_orders = Dict.empty(key_type=uint64, value_type=float64)
        if position > lower_position and np.isfinite(ask_price):
            current_ask = ask_price
            for _ in range(grid_num):
                ask_tick = uint64(np.round(current_ask / tick_size))
                new_ask_orders[ask_tick] = current_ask
                current_ask += grid_interval

        order_values = orders.values()
        while order_values.has_next():
            order = order_values.get()
            if not order.cancellable:
                continue

            keep_order = False
            if order.side == 1 and order.order_id in new_bid_orders:
                keep_order = True
            elif order.side == -1 and order.order_id in new_ask_orders:
                keep_order = True

            if not keep_order:
                hbt.cancel(asset_no, order.order_id, False)

        for order_id, order_price in new_bid_orders.items():
            if order_id not in orders:
                hbt.submit_buy_order(asset_no, order_id, order_price, order_qty, GTC, LIMIT, False)

        for order_id, order_price in new_ask_orders.items():
            if order_id not in orders and position > lower_position:
                hbt.submit_sell_order(asset_no, order_id, order_price, order_qty, GTC, LIMIT, False)

        recorder.record(hbt)

    return True


# 统一回测入口：按参数运行一次策略并返回图和记录
def run_case(
    title,
    initial_notional=0.0,
    base_skew=1.0,
    closeout_skew=1.0,
    afternoon_end_ns=AFTERNOON_END_METHOD3_NS,
    closeout_start_ns=0,
    closeout_end_ns=0,
    recorder_interval_ns=100_000_000,
    cutoff_buffer_ns=SESSION_CUTOFF_BUFFER_NS,
    **strategy_kwargs,
):
    hbt = ROIVectorMarketDepthBacktest([build_asset()])
    recorder = Recorder(1, recorder_interval_ns)
    run_grid_strategy(
        hbt,
        recorder.recorder,
        initial_notional=initial_notional,
        base_skew=base_skew,
        closeout_skew=closeout_skew,
        afternoon_end_ns=afternoon_end_ns,
        closeout_start_ns=closeout_start_ns,
        closeout_end_ns=closeout_end_ns,
        cutoff_buffer_ns=cutoff_buffer_ns,
        **strategy_kwargs,
    )
    hbt.close()
    record = recorder.get(0)
    result = plot_intraday(record, title)
    result = add_risk_metrics(result, initial_notional, recorder_interval_ns)
    return result, record


# 示例 3：带 100 万底仓，尾盘用更强 skew 往目标仓位收敛
method3_df, method3_record = run_case(
    title="Method 3: 1m CNY target inventory, strong skew from 14:45 to 14:57",
    initial_notional=1_000_000.0,   # 目标净仓对应约 100 万市值股数（由首帧 mid 换算）
    base_skew=0.8,                  # 日内常规 skew，通常会比尾盘强平 skew 小很多
    closeout_skew=10.0,           # 14:45-14:57 的强 skew；越大越激进地往目标仓位推
    afternoon_end_ns=AFTERNOON_END_METHOD3_NS,   # 下午交易到 14:57 截止
    closeout_start_ns=AFTERNOON_CLOSEOUT_START_NS, # 14:45 开始进入强 skew 收敛阶段
    closeout_end_ns=AFTERNOON_END_METHOD3_NS,    # 14:57 结束强 skew 阶段
    use_bps=False,                  # False 用 tick；True 用 bps
    half_spread_ticks=5,            # tick 模式半边 spread
    grid_step_ticks=3,              # tick 模式网格间距
    half_spread_bps=5.0,            # bps 模式半边 spread
    grid_step_bps=1.5,              # bps 模式网格间距
    grid_num=5,                     # 单边挂 8 层网格
    order_lots=1,                   # 每笔 1 手，即 100 股
    inventory_span_lots=30,         # 允许围绕目标仓位上下浮动 30 手
    step_ns=100_000_000,            # 每 100ms 刷新一次策略
    recorder_interval_ns=100_000_000,  # 记录频率，影响曲线采样密度
 )


summarize_trade_window(method3_record, "Method 3")
ide = method3_df["timestamp"].searchsorted(target_time)
method3_df[["timestamp", "position", "equity", "equity_wo_fee", "sharpe_from_initial_notional","max_drawdown_pct_from_initial_notional"]].iloc[ide-3:ide+3]