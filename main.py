import sys
import os
import json
import ccxt
import pandas as pd
import pandas_ta as ta
import time

# Windows Terminal Encoding Fix
if sys.stdout.encoding != 'utf-8':
    sys.stdout.reconfigure(encoding='utf-8')

# ==========================================
# 1. BOT CONFIGURATIONS (Edit Here)
# ==========================================
API_KEY = 'GUvJANUc1tEtIhvNgRqLnyjLh4DH1APCOdjKok028yULTVWswCiGlAOLgM3r1S7TGQCZLKHhEc9YJqSxDYPIw'        
SECRET_KEY = 'wByStzD3OT6JO5M5FZZW8lyBEjWAbdObLG2c37B5EZ2ClybfWSTuEUpqpZmkKpRWN2024uqzMn820APquqVJw' 

SYMBOLS = ['RESOLV-USDT', 'SOL-USDT', 'XRP-USDT' ]

TIMEFRAME = '15m'
LEVERAGE = 10

# Margin & Risk Settings
MARGIN_PERCENT = 10.0     # Wallet ka kitna % per trade use karna hai
USE_SL_CAP = True        # True = SL 1% se bada hua toh reject nahi karega, cap kar dega
MAX_SL_PCT = 4.0         # Max allowed SL % (Cap Limit)

# --- NEW VWAP ANCHOR SETTINGS ---
USE_VWAP_FILTER = False   # True = VWAP k upar hi Long lega, aur niche hi Short lega
VWAP_ANCHOR = 'Session'   # Yahan type karein: 'Session', 'Weekly', ya 'Monthly'

# Indicator Settings
RSI_LEN = 50
SMA_LEN = 40
SWING_LEN = 5
HMA_LEN = 25              # Runner ko track karne ke liye

# --- RSI vs SMA DIFFERENCE FILTER ---
MIN_RSI_SMA_DIFF = 0.5    # Signal execute hone ke liye RSI aur SMA mein kam se kam itna gap hona zaroori hai

# --- SPLIT & SCALING CONFIG ---
ENABLE_SPLIT_ENTRY = True  # Risk > 1% hone par split
MARKET_PROPORTION = 0.3    # 30% Market
LIMIT_PROPORTION = 0.7     # 70% Limit
NO_SPLIT_SCALED_MARGIN = 0.5 # Split OFF aur Risk > 1% par margin scaling (50%)

# --- 4-STAGE TP & SMART TRAIL SETTINGS ---
ENABLE_PARTIAL_CLOSING = True  
ENABLE_HALF_TRAIL = True   # True = 1:1.5 par SL 50% trail hoga, False = Disable

# Default Targets (Bot memory mein save honge taaki fail hone par badal sakein)
PARTIAL_1_RR = 1.0         # 1:1 RR 
PARTIAL_1_PCT = 30.0       

TRAIL_HALF_RR = 1.5        # 1:1.5 RR (Smart Trail)

PARTIAL_2_RR = 2.0         # 1:2 RR 
PARTIAL_2_PCT = 30.0

PARTIAL_3_RR = 3.0         # 1:3 RR 
PARTIAL_3_PCT = 20.0       

# Bot Memory
last_traded_candles = {sym: None for sym in SYMBOLS}
active_trades = {}
STATE_FILE = "bot_state.json"

# ==========================================
# 2. EXCHANGE SETUP & RECOVERY
# ==========================================
print("[SYSTEM] Connecting to BingX API...")
exchange = ccxt.bingx({
    'apiKey': API_KEY,
    'secret': SECRET_KEY,
    'enableRateLimit': True,
    'options': {'defaultType': 'swap', 'positionMode': True, 'adjustForTimeDifference': True}
})

try:
    exchange.load_markets()
    for sym in SYMBOLS:
        try: exchange.set_leverage(LEVERAGE, sym)
        except Exception: pass 
    print("[SYSTEM] All leverages checked successfully.")
except Exception as e:
    print(f"[ERROR] Connection failed: {e}")

def save_state():
    try:
        with open(STATE_FILE, 'w') as f: json.dump(active_trades, f)
    except: pass

def recover_active_positions():
    global active_trades
    print("🔍 [RECOVERY] Syncing memory with Exchange...")
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, 'r') as f: active_trades = json.load(f)
        except: pass

    actual_live_on_exchange = []
    try:
        positions = exchange.fetch_positions()
        for p in positions:
            contracts = float(p.get('contracts', 0))
            if contracts > 0:
                side = p['info'].get('positionSide', '').upper() or str(p.get('side', '')).upper()
                actual_live_on_exchange.append(f"{p['symbol']}_{side}")
    except: pass

    active_trades = {k: v for k, v in active_trades.items() if k in actual_live_on_exchange}
    save_state()
    print(f"✅ [RECOVERY] Done. Monitoring {len(active_trades)} Active Trades.")

# ==========================================
# 3. DYNAMIC MARGIN CALCULATION
# ==========================================
def get_qty(symbol, margin_pct, price):
    try:
        balance = exchange.fetch_balance()
        available_usdt = float(balance['USDT']['free'])
        calculated_margin = available_usdt * (margin_pct / 100.0)
        raw_qty = (calculated_margin * LEVERAGE) / price
        return float(exchange.amount_to_precision(symbol, raw_qty))
    except Exception as e:
        print(f"[ERROR] Failed to calculate qty for {symbol}: {e}")
        return 0

# ==========================================
# 4. HEDGE TRADE EXECUTION
# ==========================================
def execute_hedge_trade(symbol, position_side, entry_price, sl_price, is_split):
    try:
        m_margin = MARGIN_PERCENT * (MARKET_PROPORTION if (ENABLE_SPLIT_ENTRY and is_split) else (NO_SPLIT_SCALED_MARGIN if is_split else 1.0))
        l_margin = MARGIN_PERCENT * LIMIT_PROPORTION if (ENABLE_SPLIT_ENTRY and is_split) else 0

        risk = abs(entry_price - sl_price)
        e_side, x_side = ('buy', 'sell') if position_side == 'LONG' else ('sell', 'buy')

        m_qty = get_qty(symbol, m_margin, entry_price)
        
        l_id, l_qty, mid_p = None, 0, 0
        if l_margin > 0:
            mid_p = float(exchange.price_to_precision(symbol, entry_price - (risk*0.5) if position_side == 'LONG' else entry_price + (risk*0.5)))
            l_qty = get_qty(symbol, l_margin, mid_p)

        if m_qty <= 0: return

        print(f"\n⚡ [EXECUTE] Opening {position_side} on {symbol}...")

        exchange.create_order(symbol, 'MARKET', e_side, m_qty, params={'positionSide': position_side})
        print(f"   ∟ Market Entry: {m_qty} Qty @ {entry_price} (Margin Used: {m_margin}%)")
        
        if l_qty > 0:
            l_id = exchange.create_order(symbol, 'LIMIT', e_side, l_qty, mid_p, params={'positionSide': position_side})['id']
            print(f"   ∟ Limit Order: {l_qty} Qty @ {mid_p} (Margin Reserved: {l_margin}%)")

        sl_id = exchange.create_order(symbol, 'STOP_MARKET', x_side, m_qty, params={'stopPrice': sl_price, 'triggerPrice': sl_price, 'positionSide': position_side})['id']
        print(f"   ∟ Stop Loss Placed @ {sl_price:.4f}")

        active_trades[f"{symbol}_{position_side}"] = {
            'symbol': symbol, 'side': position_side, 'exit_side': x_side, 'entry_price': entry_price,
            'initial_qty': m_qty, 'current_qty': m_qty, 'sl_price': sl_price, 'sl_order_id': sl_id,
            'limit_order_id': l_id, 'limit_price': mid_p, 
            'target_0_5': entry_price + (risk * TRAIL_HALF_RR) if position_side == 'LONG' else entry_price - (risk * TRAIL_HALF_RR), 
            'sl_trailed_half': False,
            'target_1': entry_price + (risk * PARTIAL_1_RR) if position_side == 'LONG' else entry_price - (risk * PARTIAL_1_RR), 
            'target_2': entry_price + (risk * PARTIAL_2_RR) if position_side == 'LONG' else entry_price - (risk * PARTIAL_2_RR), 
            'tp2_pct': PARTIAL_2_PCT,  
            'target_3': entry_price + (risk * PARTIAL_3_RR) if position_side == 'LONG' else entry_price - (risk * PARTIAL_3_RR),
            'tp3_pct': PARTIAL_3_PCT,  
            'stage': 0
        }
        save_state()
    except Exception as e: print(f"❌ [EXEC ERROR] {symbol}: {e}")

# ==========================================
# 5. ACTIVE TRADE MANAGER (TP & Runner Engine)
# ==========================================
def manage_active_trades(df_dict):
    global active_trades
    keys_to_remove = []
    
    for key, trade in active_trades.items():
        symbol = trade['symbol']
        if symbol not in df_dict: continue 
        
        try:
            positions = exchange.fetch_positions([symbol])
            open_pos = next((p for p in positions if p['info'].get('positionSide', '').upper() == trade['side'] and float(p.get('contracts', 0)) > 0), None)
            
            if not open_pos:
                print(f"🛑 [CLOSED/SL HIT] Position finished for {symbol} {trade['side']}")
                if trade['limit_order_id']:
                    try: exchange.cancel_order(trade['limit_order_id'], symbol)
                    except: pass
                keys_to_remove.append(key)
                continue
            
            curr_p = df_dict[symbol]['close'].iloc[-1]
            curr_qty = float(open_pos.get('contracts', 0))
            avg_ep = float(open_pos.get('entryPrice', trade['entry_price']))
            
            # --- Live PnL Tracking ---
            diff = curr_p - avg_ep if trade['side'] == 'LONG' else avg_ep - curr_p
            pnl_usdt = diff * curr_qty
            pnl_pct = (diff / avg_ep) * 100 * LEVERAGE
            status_icon = "💹" if pnl_usdt > 0 else "🔻"
            print(f"[{trade['side']} ACTIVE] {symbol} | Price: {curr_p:.4f} | Entry: {avg_ep:.4f} | PnL: {status_icon} ${pnl_usdt:.2f} ({pnl_pct:.2f}%)")

            # --- Check Limit Fill ---
            if trade['limit_order_id'] and curr_qty > (trade['current_qty'] + 0.0001):
                print(f"🔔 [LIMIT FILLED] {symbol} Mid-Level order hit! Adjusting Targets & Stop Loss.")
                trade['limit_order_id'] = None
                trade['current_qty'] = curr_qty
                trade['entry_price'] = avg_ep
                
                risk = abs(avg_ep - trade['sl_price'])
                trade['target_0_5'] = avg_ep + (risk * TRAIL_HALF_RR) if trade['side'] == 'LONG' else avg_ep - (risk * TRAIL_HALF_RR)
                trade['sl_trailed_half'] = False
                trade['target_1'] = avg_ep + (risk * PARTIAL_1_RR) if trade['side'] == 'LONG' else avg_ep - (risk * PARTIAL_1_RR)
                trade['target_2'] = avg_ep + (risk * PARTIAL_2_RR) if trade['side'] == 'LONG' else avg_ep - (risk * PARTIAL_2_RR)
                trade['target_3'] = avg_ep + (risk * PARTIAL_3_RR) if trade['side'] == 'LONG' else avg_ep - (risk * PARTIAL_3_RR)
                
                if trade['sl_order_id']:
                    try: exchange.cancel_order(trade['sl_order_id'], symbol)
                    except: pass
                    trade['sl_order_id'] = None
                try:
                    trade['sl_order_id'] = exchange.create_order(symbol, 'STOP_MARKET', trade['exit_side'], curr_qty, params={'stopPrice': trade['sl_price'], 'triggerPrice': trade['sl_price'], 'positionSide': trade['side']})['id']
                except Exception as e:
                    print(f"   ∟ ⚠️ Could not update SL after Limit Fill: {e}")
                save_state()

            # --- STAGE 0: TP 1 (Book OR REDISTRIBUTE) ---
            if trade['stage'] == 0:
                is_tp1 = (trade['side'] == 'LONG' and curr_p >= trade['target_1']) or (trade['side'] == 'SHORT' and curr_p <= trade['target_1'])
                if is_tp1:
                    print(f"⭐ [TP1 HIT - 1:1 RR] {symbol}!")
                    
                    if trade.get('limit_order_id'):
                        try: exchange.cancel_order(trade['limit_order_id'], symbol)
                        except: pass
                        trade['sl_price'] = trade['limit_price']
                        print(f"   ∟ Trailing SL up to pending Limit Price: {trade['sl_price']:.4f}")
                        trade['limit_order_id'] = None
                    else:
                        print(f"   ∟ SL remains unchanged.")
                    
                    if ENABLE_PARTIAL_CLOSING:
                        try:
                            raw_qty = trade['initial_qty'] * (PARTIAL_1_PCT / 100.0)
                            c_qty = float(exchange.amount_to_precision(symbol, raw_qty))
                            if c_qty <= 0:
                                raise Exception(f"Quantity completely rounded to Zero by Exchange")
                                
                            exchange.create_order(symbol, 'MARKET', trade['exit_side'], c_qty, params={'positionSide': trade['side']})
                            trade['current_qty'] -= c_qty
                            print(f"   ∟ Booked {PARTIAL_1_PCT}% profit successfully.")
                        except Exception as e:
                            half_failed_pct = PARTIAL_1_PCT / 2.0
                            trade['tp2_pct'] = trade.get('tp2_pct', PARTIAL_2_PCT) + half_failed_pct
                            trade['tp3_pct'] = trade.get('tp3_pct', PARTIAL_3_PCT) + half_failed_pct
                            print(f"   ∟ ⚠️ Partial skipped ({e}). Redistributing {half_failed_pct}% to TP2 and {half_failed_pct}% to TP3!")
                    
                    if trade['sl_order_id']:
                        try: exchange.cancel_order(trade['sl_order_id'], symbol)
                        except: pass
                        trade['sl_order_id'] = None
                    try:
                        trade['sl_order_id'] = exchange.create_order(symbol, 'STOP_MARKET', trade['exit_side'], trade['current_qty'], params={'stopPrice': trade['sl_price'], 'triggerPrice': trade['sl_price'], 'positionSide': trade['side']})['id']
                    except Exception as e:
                        print(f"   ∟ ⚠️ Error replacing SL at TP1: {e}")
                    
                    trade['stage'] = 1
                    save_state()

            # --- STAGE 1: Check 1:1.5 Trail & Hit TP 2 ---
            elif trade['stage'] == 1:
                # 1. Half-Trail Check
                if ENABLE_HALF_TRAIL and not trade.get('sl_trailed_half', False):
                    is_1_5 = (trade['side'] == 'LONG' and curr_p >= trade.get('target_0_5', 0)) or (trade['side'] == 'SHORT' and curr_p <= trade.get('target_0_5', 0))
                    if is_1_5:
                        midpoint_sl = (trade['entry_price'] + trade['sl_price']) / 2.0
                        midpoint_sl = float(exchange.price_to_precision(symbol, midpoint_sl))

                        print(f"🛡️ [SL TRAIL] {symbol} reached 1:1.5 RR! Trailing SL to Midpoint ({midpoint_sl:.4f})")
                        if trade['sl_order_id']:
                            try: exchange.cancel_order(trade['sl_order_id'], symbol)
                            except: pass
                            trade['sl_order_id'] = None
                        try:
                            trade['sl_order_id'] = exchange.create_order(symbol, 'STOP_MARKET', trade['exit_side'], trade['current_qty'], params={'stopPrice': midpoint_sl, 'triggerPrice': midpoint_sl, 'positionSide': trade['side']})['id']
                            trade['sl_price'] = midpoint_sl
                        except Exception as e:
                            print(f"   ∟ ⚠️ Error Trailing SL to Half: {e}")
                        
                        trade['sl_trailed_half'] = True
                        save_state()

                # 2. TP 2 Logic
                is_tp2 = (trade['side'] == 'LONG' and curr_p >= trade['target_2']) or (trade['side'] == 'SHORT' and curr_p <= trade['target_2'])
                if is_tp2:
                    current_tp2_pct = trade.get('tp2_pct', PARTIAL_2_PCT)
                    print(f"🌟 [TP2 HIT - 1:2 RR] {symbol}! Trailing SL to BE ({trade['entry_price']:.4f})")
                    if ENABLE_PARTIAL_CLOSING:
                        try:
                            raw_qty = trade['initial_qty'] * (current_tp2_pct / 100.0)
                            c_qty = float(exchange.amount_to_precision(symbol, raw_qty))
                            if c_qty <= 0:
                                raise Exception(f"Quantity completely rounded to Zero")
                                
                            exchange.create_order(symbol, 'MARKET', trade['exit_side'], c_qty, params={'positionSide': trade['side']})
                            trade['current_qty'] -= c_qty
                            print(f"   ∟ Booked {current_tp2_pct}% profit successfully.")
                        except Exception as e:
                            print(f"   ∟ ⚠️ Partial skipped ({e}). Kept as Runner.")
                    
                    if trade['sl_order_id']:
                        try: exchange.cancel_order(trade['sl_order_id'], symbol)
                        except: pass
                        trade['sl_order_id'] = None
                    try:
                        trade['sl_order_id'] = exchange.create_order(symbol, 'STOP_MARKET', trade['exit_side'], trade['current_qty'], params={'stopPrice': trade['entry_price'], 'triggerPrice': trade['entry_price'], 'positionSide': trade['side']})['id']
                        trade['sl_price'] = trade['entry_price']
                    except Exception as e:
                        print(f"   ∟ ⚠️ Error Trailing SL to BE: {e}")
                    
                    trade['stage'] = 2
                    save_state()

            # --- STAGE 2: TP 3 ---
            elif trade['stage'] == 2:
                is_tp3 = (trade['side'] == 'LONG' and curr_p >= trade['target_3']) or (trade['side'] == 'SHORT' and curr_p <= trade['target_3'])
                if is_tp3:
                    if ENABLE_PARTIAL_CLOSING:
                        current_tp3_pct = trade.get('tp3_pct', PARTIAL_3_PCT)
                        print(f"🔥 [TP3 HIT - 1:3 RR] {symbol}! Booking {current_tp3_pct}%. Remaining is Runner!")
                    else:
                        current_tp3_pct = 80.0
                        print(f"🔥 [TP3 HIT - 1:3 RR] {symbol}! Partial OFF Mode: Booking MASSIVE 80%!")

                    try:
                        raw_qty = trade['initial_qty'] * (current_tp3_pct / 100.0)
                        c_qty = float(exchange.amount_to_precision(symbol, raw_qty))
                        if c_qty <= 0:
                            raise Exception(f"Quantity completely rounded to Zero")
                            
                        exchange.create_order(symbol, 'MARKET', trade['exit_side'], c_qty, params={'positionSide': trade['side']})
                        trade['current_qty'] -= c_qty
                        print(f"   ∟ Booked {current_tp3_pct}% profit successfully.")
                    except Exception as e:
                        print(f"   ∟ ⚠️ Partial skipped ({e}). Kept as Runner.")
                    
                    if trade['sl_order_id']:
                        try: exchange.cancel_order(trade['sl_order_id'], symbol)
                        except: pass
                        trade['sl_order_id'] = None
                    try:
                        trade['sl_order_id'] = exchange.create_order(symbol, 'STOP_MARKET', trade['exit_side'], trade['current_qty'], params={'stopPrice': trade['sl_price'], 'triggerPrice': trade['sl_price'], 'positionSide': trade['side']})['id']
                    except Exception as e:
                        print(f"   ∟ ⚠️ Error updating SL after TP3: {e}")
                    
                    trade['stage'] = 3
                    save_state()

            # --- STAGE 3: HMA Runner ---
            elif trade['stage'] == 3:
                live_hma = df_dict[symbol]['hma'].iloc[-1]
                last_hma = df_dict[symbol]['hma'].iloc[-2]
                
                long_reversal = trade['side'] == 'LONG' and live_hma < last_hma
                short_reversal = trade['side'] == 'SHORT' and live_hma > last_hma
                
                if long_reversal or short_reversal:
                    print(f"🚀 [HMA REVERSAL] {symbol} Trend Changed! Closing final Runner Position.")
                    try:
                        exchange.create_order(symbol, 'MARKET', trade['exit_side'], trade['current_qty'], params={'positionSide': trade['side']})
                    except Exception as e: 
                        print(f"   ∟ ⚠️ Final close error: {e}")
                    
                    if trade['sl_order_id']:
                        try: exchange.cancel_order(trade['sl_order_id'], symbol)
                        except: pass
                    keys_to_remove.append(key)
                    continue

        except Exception as e: 
            pass 

    for k in keys_to_remove: del active_trades[k]
    if keys_to_remove: save_state()

# ==========================================
# 6. STRATEGY & LOGIC EVALUATION
# ==========================================
def fetch_data_and_check_signal(symbol):
    global last_traded_candles 
    
    try:
        bars = exchange.fetch_ohlcv(symbol, timeframe=TIMEFRAME, limit=1000)
        df = pd.DataFrame(bars, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
        
        df['rsi'] = ta.rsi(df['close'], length=RSI_LEN)
        df['sma'] = ta.sma(df['rsi'], length=SMA_LEN)
        df['hma'] = ta.hma(df['close'], length=HMA_LEN)
        
        df['sma_prev'] = df['sma'].shift(1)
        df['is_green'] = df['sma'] > df['sma_prev']
        df['is_red'] = df['sma'] < df['sma_prev']
        
        df['recent_low'] = df['low'].rolling(window=SWING_LEN).min().shift(1)
        df['recent_high'] = df['high'].rolling(window=SWING_LEN).max().shift(1)
        
        df['datetime'] = pd.to_datetime(df['timestamp'], unit='ms')
        
        # 🔥 ANCHORED VWAP LOGIC 🔥
        if VWAP_ANCHOR.upper() == 'WEEKLY':
            group_key = df['datetime'].dt.strftime('%Y-%V') # Year-Week format
        elif VWAP_ANCHOR.upper() == 'MONTHLY':
            group_key = df['datetime'].dt.strftime('%Y-%m') # Year-Month format
        else:
            group_key = df['datetime'].dt.date # Default Session (Daily)
            
        df['typical_price'] = (df['high'] + df['low'] + df['close']) / 3
        df['tp_vol'] = df['typical_price'] * df['volume']
        
        df['cum_tp_vol'] = df.groupby(group_key)['tp_vol'].cumsum()
        df['cum_vol'] = df.groupby(group_key)['volume'].cumsum()
        df['vwap'] = df['cum_tp_vol'] / df['cum_vol']
        
        # 3 Candles History for Delayed Entry check
        last_closed = df.iloc[-2]      # The candle that just closed
        prev_closed = df.iloc[-3]      # The one before it
        prev_prev_closed = df.iloc[-4] # The one before that
        
        current_price = last_closed['close']
        current_vwap = last_closed['vwap']
        current_candle_time = last_closed['timestamp'] 
        
        # 🔥 RSI/SMA DIFFERENCE CHECK (THE NEW FILTER) 🔥
        rsi_sma_diff = abs(last_closed['rsi'] - last_closed['sma'])
        is_diff_valid = rsi_sma_diff >= MIN_RSI_SMA_DIFF

        prev_rsi_sma_diff = abs(prev_closed['rsi'] - prev_closed['sma'])

        # --- LONG CONDITIONS ---
        # 1. Immediate Cross
        rsi_cross_up = (prev_closed['rsi'] <= prev_closed['sma']) and (last_closed['rsi'] > last_closed['sma'])
        
        # 2. Delayed Cross (1-Candle Tolerance) - Agar pichli me fail hua par ab gap clear hai
        prev_cross_up = (prev_prev_closed['rsi'] <= prev_prev_closed['sma']) and (prev_closed['rsi'] > prev_closed['sma'])
        delayed_long_cross = prev_cross_up and (prev_rsi_sma_diff < MIN_RSI_SMA_DIFF) and (last_closed['rsi'] > last_closed['sma'])

        # Combining both crosses
        effective_cross_up = rsi_cross_up or delayed_long_cross
        
        long_trigger_1 = last_closed['is_green'] and effective_cross_up
        
        sma_turns_green = last_closed['is_green'] and not prev_closed['is_green']
        long_trigger_2 = sma_turns_green and (last_closed['rsi'] > last_closed['sma'])
        
        # Final Long condition: Hybrid check + Momentum Gap check
        long_condition = (long_trigger_1 or long_trigger_2) and is_diff_valid and (last_closed['rsi'] > last_closed['sma'])

        # --- SHORT CONDITIONS ---
        # 1. Immediate Cross
        rsi_cross_down = (prev_closed['rsi'] >= prev_closed['sma']) and (last_closed['rsi'] < last_closed['sma'])
        
        # 2. Delayed Cross (1-Candle Tolerance)
        prev_cross_down = (prev_prev_closed['rsi'] >= prev_prev_closed['sma']) and (prev_closed['rsi'] < prev_closed['sma'])
        delayed_short_cross = prev_cross_down and (prev_rsi_sma_diff < MIN_RSI_SMA_DIFF) and (last_closed['rsi'] < last_closed['sma'])

        # Combining both crosses
        effective_cross_down = rsi_cross_down or delayed_short_cross
        
        short_trigger_1 = last_closed['is_red'] and effective_cross_down
        
        sma_turns_red = last_closed['is_red'] and not prev_closed['is_red']
        short_trigger_2 = sma_turns_red and (last_closed['rsi'] < last_closed['sma'])
        
        # Final Short condition: Hybrid check + Momentum Gap check
        short_condition = (short_trigger_1 or short_trigger_2) and is_diff_valid and (last_closed['rsi'] < last_closed['sma'])
        
        logic_name = "HYBRID"

        vwap_long_ok = (not USE_VWAP_FILTER) or (current_price > current_vwap)
        vwap_short_ok = (not USE_VWAP_FILTER) or (current_price < current_vwap)
        
        is_long_active = f"{symbol}_LONG" in active_trades
        is_short_active = f"{symbol}_SHORT" in active_trades

        # --- SIGNAL EXECUTION ---
        # Note: Added a print statement to log when a signal is rejected due to the gap filter
        if (long_trigger_1 or long_trigger_2) and not is_diff_valid:
            print(f"[REJECT] {symbol} Long ignored. RSI/SMA gap ({rsi_sma_diff:.2f}) is less than required {MIN_RSI_SMA_DIFF}")
            
        elif (short_trigger_1 or short_trigger_2) and not is_diff_valid:
            print(f"[REJECT] {symbol} Short ignored. RSI/SMA gap ({rsi_sma_diff:.2f}) is less than required {MIN_RSI_SMA_DIFF}")

        # --- CHECK LONG ---
        if long_condition:
            if is_long_active:
                print(f"[SKIP] {symbol} Already running a LONG trade.")
            elif last_traded_candles[symbol] == current_candle_time:
                print(f"[SKIP] {symbol} Already traded LONG on this candle.")
            else:
                sl_price = last_closed['recent_low']
                risk_pct = ((current_price - sl_price) / current_price) * 100
                
                if not vwap_long_ok:
                    print(f"[FILTER] {logic_name} Long Rejected on {symbol}. Price is BELOW {VWAP_ANCHOR} VWAP.")
                else:
                    if USE_SL_CAP and risk_pct > MAX_SL_PCT:
                        print(f"[ADJUST] {symbol} Long SL was {risk_pct:.2f}%. Capping to {MAX_SL_PCT}%")
                        sl_price = current_price * (1 - (MAX_SL_PCT / 100.0))
                        risk_pct = MAX_SL_PCT

                    print(f"[{logic_name} LONG] VALID SIGNAL on {symbol} | Price: {current_price} | Gap: {rsi_sma_diff:.2f} | Risk: {risk_pct:.2f}%")
                    execute_hedge_trade(symbol, 'LONG', current_price, sl_price, risk_pct > 1.0)
                    last_traded_candles[symbol] = current_candle_time 
                
        # --- CHECK SHORT ---
        elif short_condition:
            if is_short_active:
                print(f"[SKIP] {symbol} Already running a SHORT trade.")
            elif last_traded_candles[symbol] == current_candle_time:
                print(f"[SKIP] {symbol} Already traded SHORT on this candle.")
            else:
                sl_price = last_closed['recent_high']
                risk_pct = ((sl_price - current_price) / current_price) * 100
                
                if not vwap_short_ok:
                    print(f"[FILTER] {logic_name} Short Rejected on {symbol}. Price is ABOVE {VWAP_ANCHOR} VWAP.")
                else:
                    if USE_SL_CAP and risk_pct > MAX_SL_PCT:
                        print(f"[ADJUST] {symbol} Short SL was {risk_pct:.2f}%. Capping to {MAX_SL_PCT}%")
                        sl_price = current_price * (1 + (MAX_SL_PCT / 100.0))
                        risk_pct = MAX_SL_PCT

                    print(f"[{logic_name} SHORT] VALID SIGNAL on {symbol} | Price: {current_price} | Gap: {rsi_sma_diff:.2f} | Risk: {risk_pct:.2f}%")
                    execute_hedge_trade(symbol, 'SHORT', current_price, sl_price, risk_pct > 1.0)
                    last_traded_candles[symbol] = current_candle_time 
        else:
            print(f"[SCAN] {symbol} | Price: {current_price} | {VWAP_ANCHOR} VWAP: {current_vwap:.4f} | Waiting...")
            
        return df 
            
    except Exception as e:
        print(f"[ERROR] Fetching data for {symbol}: {e}")
        return None

# ==========================================
# 6. MAIN BOT LOOP (Multi-Coin Scanner)
# ==========================================
print(f"\n[SYSTEM] Multi-Coin Bot started. Logic: HYBRID (Trend + RSI) | VWAP Anchor: {VWAP_ANCHOR} | Gap Filter: {MIN_RSI_SMA_DIFF}")
recover_active_positions()

while True:
    print(f"\n--- Starting New Scan Cycle [{time.strftime('%H:%M:%S')}] ---")
    m_data = {}
    for symbol in SYMBOLS:
        df = fetch_data_and_check_signal(symbol)
        if df is not None:
            m_data[symbol] = df
        time.sleep(1) 
        
    manage_active_trades(m_data)
    time.sleep(30)
