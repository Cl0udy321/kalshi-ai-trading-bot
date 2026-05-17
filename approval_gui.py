import streamlit as st
import sqlite3
import pandas as pd
import asyncio
import threading
import uuid

from src.clients.kalshi_client import KalshiClient

st.set_page_config(page_title="Kalshi Trade Approval", layout="wide")
DB_PATH = "trading_system.db"

def get_pending_positions():
    try:
        conn = sqlite3.connect(DB_PATH)
        query = """
            SELECT id, market_id, side, entry_price, quantity, rationale, confidence, strategy 
            FROM positions 
            WHERE live = 0
        """
        df = pd.read_sql_query(query, conn)
        conn.close()
        return df.to_dict('records')
    except Exception as e:
        st.error(f"Database error: {e}")
        return []

def update_db_status(position_id, is_live):
    try:
        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()
        if is_live:
            cursor.execute("UPDATE positions SET live = 1 WHERE id = ?", (position_id,))
        else:
            cursor.execute("DELETE FROM positions WHERE id = ?", (position_id,))
        conn.commit()
        conn.close()
    except Exception as e:
        st.error(f"Database error: {e}")

def run_coroutine_in_thread(coro):
    result, exception = None, None
    def worker():
        nonlocal result, exception
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            result = loop.run_until_complete(coro)
        except Exception as e:
            exception = e
        finally:
            loop.close()
    thread = threading.Thread(target=worker)
    thread.start()
    thread.join()
    if exception: raise exception
    return result

async def execute_async_trade(pos):
    kalshi = KalshiClient()
    price_cents = int(float(pos['entry_price']) * 100)
    kwargs = {
        "ticker": pos['market_id'],
        "client_order_id": str(uuid.uuid4()),
        "action": "buy",
        "side": pos['side'].lower(),
        "count": max(1, int(pos['quantity']))
    }
    if kwargs["side"] == "yes": kwargs["yes_price"] = price_cents
    else: kwargs["no_price"] = price_cents
    return await kalshi.place_order(**kwargs)

def place_live_order(pos):
    try:
        run_coroutine_in_thread(execute_async_trade(pos))
        st.success(f"✅ Order filled! Bought {int(pos['quantity'])} {pos['side']} contracts for {pos['market_id']}.")
        update_db_status(pos['id'], True) 
        return True
    except Exception as e:
        error_msg = str(e)
        if "market_closed" in error_msg.lower() or "market closed" in error_msg.lower():
            st.error(f"⚠️ Market `{pos['market_id']}` has closed on Kalshi! Removing from queue.")
            update_db_status(pos['id'], False) # Auto-delete closed markets to keep things clean
        else:
            st.error(f"❌ Failed to place order: {error_msg}")
        return False

def main():
    st.title("⚖️ Kalshi Execution Desk")
    
    positions = get_pending_positions()
    if not positions:
        st.info("No pending trades in the queue. Run `cli.py` or `run_risky.py` to find opportunities.")
        return

    col1, col2, col3 = st.columns(3)
    col1.metric("Pending Trades", len(positions))
    col2.metric("Environment", "DEMO API")
    col3.metric("Available Balance", "$600.00") 
    st.divider()

    for pos in positions:
        with st.container():
            is_risky = 'risky' in str(pos['strategy']).lower()
            
            # --- VISUAL STRATEGY IDENTIFICATION ---
            if is_risky:
                st.error("🔥 **RISKY VOLATILITY PLAY** — *Short expiry momentum/coin-flip.*")
            else:
                st.info("🛡️ **SAFE COMPENSATED EDGE** — *High probability edge compounding.*")
                
            st.subheader(f"🏷️ {pos['market_id']}")
            
            info_col, reason_col, action_col = st.columns([1.5, 2.5, 1])
            
            with info_col:
                st.markdown(f"**Contract Side:** :{'red' if is_risky else 'blue'}[**{pos['side']}**]")
                
                qty = int(pos['quantity'])
                entry = float(pos['entry_price'])
                cost = qty * entry
                profit = (qty * 1.00) - cost
                roi = (profit / cost) * 100 if cost > 0 else 0
                
                st.metric("Target Entry", f"${entry:.2f}")
                st.metric("Quantity", f"{qty}", help=f"Total Cost: ${cost:.2f}")
                st.metric("Expected Profit", f"${profit:.2f}", f"+{roi:.1f}% ROI")

            with reason_col:
                st.markdown("### 🧠 Logic")
                st.write(str(pos['rationale']))
                
                if pos['confidence']:
                    conf = float(pos['confidence'])
                    st.progress(min(conf, 1.0), text=f"Calculated Score: {conf:.0%}")

            with action_col:
                st.write("\n\n")
                if st.button("✅ Approve", key=f"app_{pos['id']}", use_container_width=True):
                    with st.spinner("Routing..."):
                        if place_live_order(pos): st.rerun()

                if st.button("❌ Deny", type="primary", key=f"den_{pos['id']}", use_container_width=True):
                    update_db_status(pos['id'], False) 
                    st.rerun()
        st.markdown("---")

if __name__ == "__main__":
    main()