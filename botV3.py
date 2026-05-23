"""
==============================================================================
          QUANTITATIVE ALPHA ENGINE V3 - "SOVEREIGN"                         
          Mempool Oracle x Binance Spot | PhD-Level Architecture              
                                                                             
  ARCHITECTURE:                                                              
  +----------------------------------------------------------+               
  |  C-ENGINE FEED  ->  MEMPOOL CLUSTER ANALYZER             |               
  |  BINANCE FEED   ->  MULTI-TF TECHNICAL SUITE             |  -> CSS       
  |  ORDERBOOK      ->  MICROSTRUCTURE ANALYZER              |               
  +----------------------------------------------------------+               
                                                                             
  CSS (Composite Signal Score) drives ALL execution decisions.                
  Position sizing via fractional Kelly Criterion.                            
  Dynamic ATR-based stops replace fixed percentages.                          
==============================================================================
"""

import time
import sys
import threading
import math
import json
import logging
import collections
import ccxt
import requests
from dataclasses import dataclass, field
from typing import Optional

# ---------------------------------------------
# LOGGING - Timestamped, structured
# ---------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%H:%M:%S",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler("sovereign_trades.log", mode='a', encoding='utf-8')
    ]
)
log = logging.getLogger("SOVEREIGN")

# ---------------------------------------------
# CONNECTION CONSTANTS
# ---------------------------------------------
ORACLE_API_KEY   = "YOUR_API_KEY_HERE"
BINANCE_API_KEY  = "YOUR_BINANCE_KEY_HERE"
BINANCE_SECRET   = "YOUR_BINANCE_SECRET_HERE"
SYMBOL           = "BTC/USDT"

# ---------------------------------------------
# RISK & SIZING CONSTANTS
# ---------------------------------------------
MAX_PORTFOLIO_RISK_PCT  = 0.02     # Risk max 2% of equity per trade
KELLY_FRACTION          = 0.25     # Use 25% of full Kelly (conservative)
MAX_POSITION_BTC        = 0.10     # Hard cap
MIN_WHALE_BTC           = 15.0
PANIC_FEE_SAT_VB        = 150.0

# ---------------------------------------------
# THRESHOLDS (Hyper-Sensitive)
# ---------------------------------------------
CSS_BUY_THRESHOLD       = 0.15     
CSS_SELL_THRESHOLD      = -0.15    

# ---------------------------------------------
# ATR STOP PARAMETERS
# ---------------------------------------------
ATR_STOP_MULTIPLIER     = 1.5      # Stop = entry - 1.5 * ATR
ATR_TP_MULTIPLIER       = 0.5      # TP = entry + 0.5 * ATR 

# ---------------------------------------------
# MEMPOOL CLUSTER WINDOW
# ---------------------------------------------
CLUSTER_WINDOW_SEC      = 120      # Rolling 2-minute event cluster
CLUSTER_DECAY_HALF_LIFE = 45       # Score halves every 45 seconds

# ===================================================================
#  SHARED STATE - Thread-safe via RLock
# ===================================================================
lock = threading.RLock()

state = {
    # Market
    "price":          0.0,
    "bid_depth":      0.0,
    "ask_depth":      0.0,
    # Position
    "inventory_btc":  0.0,
    "entry_price":    0.0,
    "highest_price":  0.0,
    "usdt_spent":     0.0,
    # Dynamic stops (set on entry)
    "stop_loss":      0.0,
    "take_profit":    0.0,
    # Technical indicators
    "rsi_1m":         50.0,
    "rsi_5m":         50.0,
    "ema_fast":       0.0,        
    "ema_slow":       0.0,        
    "macd":           0.0,        
    "macd_signal":    0.0,        
    "atr_14":         0.0,        
    "bb_pct":         0.0,        
    "adx":            0.0,        
    "vwap":           0.0,        
    # Microstructure
    "ob_imbalance":   0.0,        
    # Control
    "is_running":     True,
    "processed_txids": set(),
    # Trade journal for Kelly
    "trade_history":  [],         
    # Mempool event cluster
    "mempool_events": collections.deque(),   
}

# ===================================================================
#  EXCHANGE INITIALIZATION
# ===================================================================
log.info("Booting SOVEREIGN V3...")
try:
    exchange = ccxt.binance({
        "apiKey":          BINANCE_API_KEY,
        "secret":          BINANCE_SECRET,
        "enableRateLimit": True,
        "options":         {"defaultType": "spot"},
        "timeout":         10000,
    })
    exchange.set_sandbox_mode(True)

    balance = exchange.fetch_balance()
    usdt_free = balance["free"].get("USDT", 0)
    btc_free   = balance["free"].get("BTC", 0)
    state["inventory_btc"] = btc_free

    log.info(f"Bankroll: ${usdt_free:.2f} USDT | Inventory: {btc_free:.4f} BTC")

    if btc_free > 0.001:
        ticker = exchange.fetch_ticker(SYMBOL)
        state["entry_price"]   = ticker["last"]
        state["highest_price"] = ticker["last"]
        state["usdt_spent"]    = btc_free * ticker["last"]
        log.info(f"Existing position detected. Approx entry: ${state['entry_price']:.2f}")

except Exception as e:
    log.critical(f"Binance init failed: {e}")
    sys.exit(1)


# ===================================================================
#  MATHEMATICS ENGINE
# ===================================================================

def calc_rsi(closes: list, period: int = 14) -> float:
    if len(closes) < period + 1:
        return 50.0
    deltas = [closes[i] - closes[i-1] for i in range(1, len(closes))][-period:]
    gains  = sum(d for d in deltas if d > 0) / period
    losses = sum(-d for d in deltas if d < 0) / period
    if losses == 0:
        return 99.0
    rs = gains / losses
    return 100.0 - (100.0 / (1.0 + rs))

def calc_ema(values: list, period: int) -> list:
    k = 2.0 / (period + 1)
    ema = [values[0]]
    for v in values[1:]:
        ema.append(v * k + ema[-1] * (1 - k))
    return ema

def calc_macd(closes: list) -> tuple:
    if len(closes) < 35:
        return 0.0, 0.0
    ema12 = calc_ema(closes, 12)
    ema26 = calc_ema(closes, 26)
    macd_line = [ema12[i] - ema26[i] for i in range(len(ema12))]
    signal    = calc_ema(macd_line[-9:], 9)
    return macd_line[-1], signal[-1]

def calc_atr(highs: list, lows: list, closes: list, period: int = 14) -> float:
    if len(highs) < period + 1:
        return closes[-1] * 0.005   
    trs = []
    for i in range(1, len(closes)):
        tr = max(highs[i] - lows[i], abs(highs[i] - closes[i-1]), abs(lows[i]  - closes[i-1]))
        trs.append(tr)
    atr = sum(trs[:period]) / period
    for tr in trs[period:]:
        atr = (atr * (period - 1) + tr) / period
    return atr

def calc_bb_pct(closes: list, period: int = 20) -> float:
    if len(closes) < period:
        return 0.5
    recent = closes[-period:]
    sma    = sum(recent) / period
    std    = math.sqrt(sum((x - sma)**2 for x in recent) / period)
    if std == 0:
        return 0.5
    upper = sma + 2 * std
    lower = sma - 2 * std
    return (closes[-1] - lower) / (upper - lower)

def calc_adx(highs: list, lows: list, closes: list, period: int = 14) -> float:
    if len(closes) < period * 2:
        return 25.0
    dmp, dmm, tr_list = [], [], []
    for i in range(1, len(closes)):
        up_move   = highs[i]  - highs[i-1]
        down_move = lows[i-1] - lows[i]
        tr = max(highs[i] - lows[i], abs(highs[i] - closes[i-1]), abs(lows[i] - closes[i-1]))
        tr_list.append(tr)
        dmp.append(up_move   if up_move > down_move and up_move > 0   else 0)
        dmm.append(down_move if down_move > up_move and down_move > 0 else 0)

    def wilder_smooth(data, p):
        s = sum(data[:p])
        result = [s]
        for v in data[p:]:
            s = s - s/p + v
            result.append(s)
        return result

    atr_s = wilder_smooth(tr_list,  period)
    dmp_s = wilder_smooth(dmp,      period)
    dmm_s = wilder_smooth(dmm,      period)

    dx_list = []
    for i in range(len(atr_s)):
        if atr_s[i] == 0:
            continue
        pdi = 100 * dmp_s[i] / atr_s[i]
        mdi = 100 * dmm_s[i] / atr_s[i]
        dx  = 100 * abs(pdi - mdi) / (pdi + mdi) if (pdi + mdi) > 0 else 0
        dx_list.append(dx)

    if not dx_list:
        return 25.0
    return sum(dx_list[-period:]) / min(period, len(dx_list))

def calc_vwap(ohlcv: list) -> float:
    cum_vol  = 0.0
    cum_pv   = 0.0
    for candle in ohlcv:
        typical = (candle[2] + candle[3] + candle[4]) / 3.0
        vol     = candle[5]
        cum_pv  += typical * vol
        cum_vol += vol
    return cum_pv / cum_vol if cum_vol > 0 else 0.0

def calc_ob_imbalance(bids: list, asks: list, depth_pct: float = 0.01) -> float:
    price = (bids[0][0] + asks[0][0]) / 2.0 if bids and asks else 1.0
    bid_vol = sum(amt for p, amt in bids if p >= price * (1 - depth_pct))
    ask_vol = sum(amt for p, amt in asks if p <= price * (1 + depth_pct))
    total   = bid_vol + ask_vol
    if total == 0:
        return 0.0
    return (bid_vol - ask_vol) / total

# ===================================================================
#  KELLY CRITERION POSITION SIZER
# ===================================================================

def kelly_position_btc(equity_usdt: float, price: float, atr: float) -> float:
    with lock:
        history = state["trade_history"][-50:]

    if len(history) < 10:
        risk_usdt = equity_usdt * 0.01
        stop_dist = atr * ATR_STOP_MULTIPLIER
        btc_qty   = risk_usdt / stop_dist if stop_dist > 0 else 0.001
        return round(min(btc_qty, MAX_POSITION_BTC, 0.01), 4)

    wins    = [t for t in history if t["won"]]
    losses  = [t for t in history if not t["won"]]
    W       = len(wins)  / len(history)
    L       = len(losses)/ len(history)
    avg_win = (sum(t["rr"] for t in wins)   / len(wins))   if wins   else 1.0
    avg_los = (sum(t["rr"] for t in losses) / len(losses)) if losses else 1.0
    R       = avg_win / avg_los if avg_los > 0 else 1.0

    full_kelly = (W * R - L) / R if R > 0 else 0.0
    frac_kelly = max(0.0, full_kelly * KELLY_FRACTION)

    risk_per_trade = equity_usdt * min(frac_kelly, MAX_PORTFOLIO_RISK_PCT)
    stop_distance  = atr * ATR_STOP_MULTIPLIER

    btc_qty = risk_per_trade / (stop_distance if stop_distance > 0 else price * 0.005)
    btc_qty = round(min(btc_qty, MAX_POSITION_BTC), 4)

    log.info(f"[KELLY] W={W:.2f} R={R:.2f} f*={full_kelly:.3f} -> {btc_qty:.4f} BTC")
    return max(btc_qty, 0.001)

# ===================================================================
#  MEMPOOL CLUSTER ANALYZER
# ===================================================================

def mempool_cluster_score() -> float:
    now = time.time()
    cutoff = now - CLUSTER_WINDOW_SEC

    with lock:
        while state["mempool_events"] and state["mempool_events"][0][0] < cutoff:
            state["mempool_events"].popleft()
        events = list(state["mempool_events"])

    if not events:
        return 0.0

    score = 0.0
    for ts, raw_score in events:
        age     = now - ts
        decay   = math.exp(-age * math.log(2) / CLUSTER_DECAY_HALF_LIFE)
        score  += raw_score * decay

    return math.tanh(score / 3.0)

def classify_mempool_tx(payload: dict) -> Optional[float]:
    if "data" in payload:
        d           = payload["data"]
        event_type  = payload.get("event", "")
        size_btc    = d.get("size_btc",             0.0)
        rbf         = d.get("rbf_flag",             False)
        fee_density = d.get("fee_density_sats_vb",  0.0)
        bump_count  = d.get("rbf_bump_count",       0)
        txid        = d.get("txid",                 "")
        ip          = d.get("origin",               "UNKNOWN")
    else:
        event_type  = payload.get("event_type", "")
        size_btc    = payload.get("size_btc",             0.0)
        rbf         = payload.get("rbf_enabled",          False)
        fee_density = payload.get("fee_sats_per_byte",    0.0)
        bump_count  = payload.get("rbf_bump_count",       0)
        txid        = payload.get("txid",                 "")
        ip          = payload.get("node_ip",              "UNKNOWN")

    with lock:
        if txid and txid in state["processed_txids"]:
            return None
        if txid:
            state["processed_txids"].add(txid)
            if len(state["processed_txids"]) > 1000:
                state["processed_txids"].clear()

    if payload.get("status") == "MESH_SYNCHRONIZING":
        return None

    is_significant = (size_btc >= MIN_WHALE_BTC or
                      bump_count > 0           or
                      fee_density > PANIC_FEE_SAT_VB or
                      event_type in ("WHALE", "RBF_PANIC", "GHOST_RESOLVE"))
    if not is_significant:
        return None

    size_score = math.log1p(size_btc) / math.log1p(500)   

    if event_type == "RBF_PANIC" or bump_count > 0:
        urgency = min(fee_density / PANIC_FEE_SAT_VB, 3.0)
        event_score = -(size_score * urgency)
        log.warning(f"[MEMPOOL] RBF_PANIC | {size_btc:.1f} BTC | Fee: {fee_density:.1f} Sats/vB | IP: {ip}")
    elif rbf and size_btc > 50:
        event_score = -(size_score * 0.7)
    elif not rbf and size_btc >= MIN_WHALE_BTC:
        event_score = size_score * 0.8
        log.info(f"[MEMPOOL] SUPPLY_SHOCK | {size_btc:.1f} BTC | Fee: {fee_density:.1f} Sats/vB | IP: {ip}")
    elif event_type == "GHOST_RESOLVE":
        event_score = size_score * 0.5
    else:
        event_score = 0.0

    if fee_density > PANIC_FEE_SAT_VB and not rbf:
        event_score -= 0.3

    return event_score

# ===================================================================
#  COMPOSITE SIGNAL SCORE ENGINE
# ===================================================================

@dataclass
class SignalBundle:
    mempool_score: float   
    rsi_score:     float   
    macd_score:    float   
    ob_score:      float   
    bb_score:      float   
    adx_bonus:     float   
    vwap_score:    float   

# ---------------------------------------------
# WEIGHTS
# ---------------------------------------------
WEIGHTS = {
    "mempool": 0.40,  # Leading: Raw on-chain supply shock
    "ob": 0.20,       # Leading: Orderbook imbalance (Immediate liquidity)
    "macd": 0.15,     # Lagging: Momentum confirmation
    "vwap": 0.10,     # Lagging: Institutional fair-value anchor
    "rsi": 0.10,      # Lagging: Overbought/Oversold state
    "bb": 0.05,       # Lagging: Volatility/Standard deviation bounds
}

def compute_css(bundle: SignalBundle) -> float:
    raw = (
        bundle.mempool_score * WEIGHTS["mempool"] +
        bundle.rsi_score     * WEIGHTS["rsi"]     +
        bundle.macd_score    * WEIGHTS["macd"]    +
        bundle.ob_score      * WEIGHTS["ob"]      +
        bundle.bb_score      * WEIGHTS["bb"]      +
        bundle.vwap_score    * WEIGHTS["vwap"]
    )
    adx_mult = 1.0 + (bundle.adx_bonus * 0.5)
    return max(-1.0, min(1.0, raw * adx_mult))

def build_signal_bundle() -> SignalBundle:
    with lock:
        rsi_1m    = state["rsi_1m"]
        rsi_5m    = state["rsi_5m"]
        macd      = state["macd"]
        macd_sig  = state["macd_signal"]
        bb_pct    = state["bb_pct"]
        adx       = state["adx"]
        vwap      = state["vwap"]
        price     = state["price"]
        ob_imb    = state["ob_imbalance"]

    avg_rsi    = (rsi_1m + rsi_5m) / 2.0
    rsi_score  = -(avg_rsi - 50.0) / 50.0      
    rsi_score  = max(-1.0, min(1.0, rsi_score))

    macd_diff   = macd - macd_sig
    macd_score  = math.tanh(macd_diff / (price * 0.0005))  

    bb_score    = 1.0 - 2.0 * bb_pct   

    adx_bonus   = min(0.06, adx / 50.0 * 0.06)

    if vwap > 0:
        vwap_dev  = (price - vwap) / vwap
        vwap_score = -math.tanh(vwap_dev / 0.01)    
    else:
        vwap_score = 0.0

    mempool_score = mempool_cluster_score()

    return SignalBundle(
        mempool_score = mempool_score,
        rsi_score     = rsi_score,
        macd_score    = macd_score,
        ob_score      = ob_imb,
        bb_score      = bb_score,
        adx_bonus     = adx_bonus,
        vwap_score    = vwap_score,
    )

# ===================================================================
#  EXECUTION ENGINE
# ===================================================================

def execute_buy(reason: str, css: float):
    with lock:
        price    = state["price"]
        atr      = state["atr_14"] or price * 0.005
        inventory = state["inventory_btc"]

    if inventory >= MAX_POSITION_BTC:
        log.info("[EXEC] Buy skipped - max inventory reached")
        return

    try:
        balance  = exchange.fetch_balance()
        usdt_bal = balance["free"].get("USDT", 0)
        btc_qty  = kelly_position_btc(usdt_bal + inventory * price, price, atr)

        if btc_qty < 0.001:
            log.info("[EXEC] Buy skipped - position size too small")
            return

        order      = exchange.create_market_buy_order(SYMBOL, btc_qty)
        fill_price = order.get("average") or order.get("price") or price
        cost       = btc_qty * fill_price

        stop_loss   = fill_price - ATR_STOP_MULTIPLIER * atr
        take_profit = fill_price + ATR_TP_MULTIPLIER   * atr

        with lock:
            prev_cost  = state["usdt_spent"]
            prev_btc   = state["inventory_btc"]
            state["usdt_spent"]    = prev_cost + cost
            state["inventory_btc"] = prev_btc + btc_qty
            state["entry_price"]   = state["usdt_spent"] / state["inventory_btc"]
            state["highest_price"] = fill_price
            state["stop_loss"]     = stop_loss
            state["take_profit"]   = take_profit

        rr_target = (take_profit - fill_price) / (fill_price - stop_loss) if fill_price > stop_loss else 0
        log.info(
            f"[BUY] {btc_qty:.4f} BTC @ ${fill_price:.2f} | CSS={css:+.3f} | "
            f"SL: ${stop_loss:.2f}  TP: ${take_profit:.2f}  R:R={rr_target:.2f}  "
            f"Reason: {reason}"
        )

    except Exception as e:
        log.error(f"[EXEC] Buy failed: {e}")

def execute_sell(reason: str, css: float):
    with lock:
        inventory = state["inventory_btc"]
        entry     = state["entry_price"]
        price     = state["price"]

    if inventory < 0.001:
        return

    try:
        order      = exchange.create_market_sell_order(SYMBOL, inventory)
        fill_price = order.get("average") or order.get("price") or price
        pnl_pct    = ((fill_price - entry) / entry * 100) if entry > 0 else 0

        won = fill_price > entry
        rr  = abs(fill_price - entry) / (entry * 0.005) if entry > 0 else 1.0
        with lock:
            state["trade_history"].append({"won": won, "rr": rr})
            state["inventory_btc"]  = 0.0
            state["entry_price"]    = 0.0
            state["highest_price"]  = 0.0
            state["usdt_spent"]     = 0.0
            state["stop_loss"]      = 0.0
            state["take_profit"]    = 0.0

        outcome = "[WIN]" if won else "[LOSS]"
        log.info(
            f"[SELL {outcome}] {inventory:.4f} BTC @ ${fill_price:.2f} | "
            f"PnL: {pnl_pct:+.2f}% | CSS={css:+.3f} | Reason: {reason}"
        )

    except Exception as e:
        log.error(f"[EXEC] Sell failed: {e}")

# ===================================================================
#  THREAD 1: MARKET MANAGER
# ===================================================================

def market_worker():
    log.info("[MARKET WORKER] Started")
    tick = 0

    while state["is_running"]:
        try:
            ticker    = exchange.fetch_ticker(SYMBOL)
            orderbook = exchange.fetch_order_book(SYMBOL, limit=50)
            bids      = orderbook["bids"]
            asks      = orderbook["asks"]

            current_price = ticker["last"]
            ob_imb        = calc_ob_imbalance(bids, asks, depth_pct=0.01)

            with lock:
                state["price"]        = current_price
                state["bid_depth"]    = sum(a for _, a in bids[:20])
                state["ask_depth"]    = sum(a for _, a in asks[:20])
                state["ob_imbalance"] = ob_imb

            if tick % 5 == 0:
                ohlcv_1m = exchange.fetch_ohlcv(SYMBOL, "1m", limit=60)
                if len(ohlcv_1m) >= 20:
                    c1 = [x[4] for x in ohlcv_1m]
                    with lock:
                        state["rsi_1m"] = calc_rsi(c1)

            if tick % 15 == 0:
                ohlcv_5m = exchange.fetch_ohlcv(SYMBOL, "5m", limit=80)
                if len(ohlcv_5m) >= 40:
                    h5 = [x[2] for x in ohlcv_5m]
                    l5 = [x[3] for x in ohlcv_5m]
                    c5 = [x[4] for x in ohlcv_5m]

                    rsi5  = calc_rsi(c5)
                    macd, macd_sig = calc_macd(c5)
                    atr   = calc_atr(h5, l5, c5)
                    bb    = calc_bb_pct(c5)
                    adx   = calc_adx(h5, l5, c5)
                    vwap  = calc_vwap(ohlcv_5m[-48:])   
                    ema9  = calc_ema(c5, 9)[-1]
                    ema21 = calc_ema(c5, 21)[-1]

                    with lock:
                        state["rsi_5m"]      = rsi5
                        state["macd"]        = macd
                        state["macd_signal"] = macd_sig
                        state["atr_14"]      = atr
                        state["bb_pct"]      = bb
                        state["adx"]         = adx
                        state["vwap"]        = vwap
                        state["ema_fast"]    = ema9
                        state["ema_slow"]    = ema21

            with lock:
                inventory = state["inventory_btc"]
                entry     = state["entry_price"]
                high      = state["highest_price"]
                sl        = state["stop_loss"]
                tp        = state["take_profit"]
                atr_now   = state["atr_14"] or current_price * 0.005

            if inventory >= 0.001 and entry > 0:
                with lock:
                    if current_price > state["highest_price"]:
                        state["highest_price"] = current_price
                        new_trail = current_price - ATR_STOP_MULTIPLIER * atr_now
                        if new_trail > state["stop_loss"]:
                            state["stop_loss"] = new_trail

                pnl = (current_price - entry) / entry * 100

                sys.stdout.write(
                    f"\r[RADAR] ${current_price:.2f} | PnL: {pnl:+.2f}% | "
                    f"RSI1m:{state['rsi_1m']:.0f} | MACD:{state['macd']:+.1f} | "
                    f"OB:{ob_imb:+.2f} | SL:${sl:.0f} TP:${tp:.0f}   "
                )
                sys.stdout.flush()

                bundle = build_signal_bundle()
                css    = compute_css(bundle)

                if current_price >= tp:
                    print()
                    execute_sell("TAKE_PROFIT - ATR target hit", css)
                elif current_price <= sl:
                    print()
                    execute_sell("STOP_LOSS - ATR stop triggered", css)
                elif css < CSS_SELL_THRESHOLD:
                    print()
                    execute_sell(f"CSS_EXIT - composite score {css:+.3f}", css)

            else:
                sys.stdout.write(
                    f"\r[SNIPER] ${current_price:.2f} | RSI1m:{state['rsi_1m']:.0f} | "
                    f"RSI5m:{state['rsi_5m']:.0f} | MACD:{state['macd']:+.1f} | "
                    f"OB:{ob_imb:+.2f} | MPool:{mempool_cluster_score():+.3f}   "
                )
                sys.stdout.flush()

                bundle = build_signal_bundle()
                css    = compute_css(bundle)

                if css >= CSS_BUY_THRESHOLD:
                    print()
                    log.info(
                        f"[CSS] Score={css:+.3f} | MP:{bundle.mempool_score:+.2f} "
                        f"RSI:{bundle.rsi_score:+.2f} MACD:{bundle.macd_score:+.2f} "
                        f"OB:{bundle.ob_score:+.2f} BB:{bundle.bb_score:+.2f}"
                    )
                    execute_buy(f"CSS_ENTRY - composite score {css:+.3f}", css)

            tick += 1
            time.sleep(1.0)

        except Exception as e:
            log.error(f"[MARKET WORKER] Error: {e}")
            time.sleep(2.0)

# ===================================================================
#  THREAD 2: SECURE HTTP ORACLE WORKER
# ===================================================================

def oracle_worker():
    log.info("[ORACLE WORKER] Connecting to Secure Cloud Alpha Feed...")
    feed_url = f"https://feed.mempool-alpha-oracle.com/?api_key={ORACLE_API_KEY}"

    try:
        # Stream the encrypted HTTPS response in real-time, timeout extended to 120s
        with requests.get(feed_url, stream=True, timeout=120) as response:
            
            if response.status_code == 401:
                log.error("[ORACLE] Access Denied. The Bouncer rejected your API key.")
                state["is_running"] = False
                return
            elif response.status_code != 200:
                log.error(f"[ORACLE] Connection failed. HTTP {response.status_code}")
                state["is_running"] = False
                return

            log.info("[ORACLE] SSL Handshake Verified. Stream active.")

            # Read the matrix code line by line as it arrives
            for line in response.iter_lines():
                if not state["is_running"]:
                    break
                
                if line:
                    decoded_line = line.decode('utf-8').strip()
                    if not decoded_line.startswith("{"):
                        continue
                    
                    try:
                        payload = json.loads(decoded_line)
                        
                        # -------------------------------------------
                        print(f"\n[ORACLE INGEST] {json.dumps(payload, indent=2)}")
                        # -------------------------------------------
                        
                        event_score = classify_mempool_tx(payload)
                        
                        if event_score is not None:
                            with lock:
                                state["mempool_events"].append((time.time(), event_score))

                            # Emergency override: extreme panic -> immediate sell
                            if event_score < -0.85:
                                with lock:
                                    inventory = state["inventory_btc"]
                                    price     = state["price"]
                                if inventory >= 0.001:
                                    log.warning("[ORACLE] EMERGENCY DUMP SIGNAL - force liquidation")
                                    execute_sell("ORACLE_EMERGENCY", -1.0)

                    except json.JSONDecodeError:
                        pass

    except Exception as e:
        log.error(f"[ORACLE] Connection lost: {e}")
        state["is_running"] = False


# ===================================================================
#  MAIN
# ===================================================================

if __name__ == "__main__":
    log.info("=================================================================")
    log.info(" SOVEREIGN V3 | Multi-Factor Quantitative Engine")
    log.info(f" Signal weights: {WEIGHTS}")
    log.info(f" CSS thresholds: BUY>{CSS_BUY_THRESHOLD:.2f}  SELL<{CSS_SELL_THRESHOLD:.2f}")
    log.info(f" Kelly fraction: {KELLY_FRACTION*100:.0f}%  |  Max risk: {MAX_PORTFOLIO_RISK_PCT*100:.0f}%/trade")
    log.info(f" ATR stop: {ATR_STOP_MULTIPLIER}xATR  |  ATR TP: {ATR_TP_MULTIPLIER}xATR")
    log.info("=================================================================")

    market_thread = threading.Thread(target=market_worker, daemon=True, name="MarketWorker")
    market_thread.start()

    try:
        oracle_worker()
    except KeyboardInterrupt:
        print()
        log.info("Shutdown requested - closing gracefully.")
        state["is_running"] = False

    market_thread.join(timeout=5)

    with lock:
        history = state["trade_history"]
    if history:
        wins = sum(1 for t in history if t["won"])
        log.info(f"Session: {len(history)} trades | Win rate: {wins/len(history)*100:.1f}%")
    log.info("SOVEREIGN V3 offline.")

