import streamlit as st
import sqlite3
import pandas as pd
import asyncio
import threading
import uuid
from datetime import datetime, timezone

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

@st.cache_data(ttl=300)
def fetch_markets_data(tickers: list):
    if not tickers: return {}, {}
    async def fetch():
        kalshi = KalshiClient()
        market_resp = await kalshi.get_markets(tickers=tickers, limit=100)
        markets = market_resp.get("markets", [])
        market_map = {m.get("ticker"): m for m in markets}
        # Fetch event slugs for URL building
        event_tickers = list({m.get("event_ticker") for m in markets if m.get("event_ticker")})
        event_map = {}
        for et in event_tickers:
            try:
                ev_resp = await kalshi.get_event(et)
                ev = ev_resp.get("event", {})
                # Derive URL slug from event title (e.g. "When will X happen?" -> "when-will-x-happen")
                import re
                raw_title = ev.get("title", "")
                slug = re.sub(r'[^a-z0-9]+', '-', raw_title.lower()).strip('-')
                event_map[et] = {
                    "slug": slug,
                    "series_ticker": ev.get("series_ticker", ""),
                }
            except Exception:
                event_map[et] = {"slug": "", "series_ticker": ""}
        return market_map, event_map
    try:
        market_map, event_map = run_coroutine_in_thread(fetch())
        return market_map, event_map
    except Exception:
        return {}, {}

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

    # Fetch live market data for all tickers
    tickers = [p['market_id'] for p in positions]
    market_data_map, event_data_map = fetch_markets_data(tickers)

    col1, col2, col3 = st.columns(3)
    col1.metric("Pending Trades", len(positions))
    col2.metric("Environment", "DEMO API")
    col3.metric("Available Balance", "$600.00") 
    st.divider()

    for pos in positions:
        market_info = market_data_map.get(pos['market_id'], {})
        
        # Auto-expiry check: Silently delete expired/closed trades from DB and skip rendering
        close_time_str = market_info.get('close_time')
        if close_time_str:
            try:
                expiry = datetime.fromisoformat(close_time_str.replace("Z", "+00:00"))
                now = datetime.now(timezone.utc)
                if (expiry - now).total_seconds() < 0:
                    update_db_status(pos['id'], False)
                    continue
            except Exception:
                pass

        with st.container():
            is_risky = 'risky' in str(pos['strategy']).lower()
            
            # --- VISUAL STRATEGY IDENTIFICATION ---
            if is_risky:
                st.error("🔥 **RISKY VOLATILITY PLAY** — *Short expiry momentum/coin-flip.*")
            else:
                st.info("🛡️ **SAFE COMPENSATED EDGE** — *High probability edge compounding.*")
                
            title = market_info.get('title', pos['market_id'])
            
            time_left = "Unknown Expiry"
            if close_time_str:
                try:
                    expiry = datetime.fromisoformat(close_time_str.replace("Z", "+00:00"))
                    now = datetime.now(timezone.utc)
                    delta = expiry - now
                    if delta.total_seconds() < 0:
                        time_left = "Market Closed"
                    else:
                        days = delta.days
                        seconds = delta.seconds
                        hours = seconds // 3600
                        minutes = (seconds % 3600) // 60
                        if days > 0:
                            time_left = f"{days}d {hours}h left"
                        else:
                            time_left = f"{hours}h {minutes}m left"
                except Exception:
                    pass

            st.subheader(f"🏷️ {title}")
            event_ticker = market_info.get('event_ticker', pos['market_id'])
            event_info = event_data_map.get(event_ticker, {})
            slug = event_info.get('slug', '')
            series_ticker = event_info.get('series_ticker', '')
            et_lower = event_ticker.lower()
            # Build correct 3-part Kalshi URL: /markets/{series}/{slug}/{event_ticker}
            if slug and series_ticker:
                kalshi_url = f"https://kalshi.com/markets/{series_ticker.lower()}/{slug}/{et_lower}"
            elif slug:
                kalshi_url = f"https://kalshi.com/markets/{slug}/{et_lower}"
            else:
                kalshi_url = f"https://kalshi.com/markets/{et_lower}"
            # Extract live ask price based on contract side
            live_price = 0.0
            if market_info:
                if pos['side'].lower() == "yes":
                    live_price = float(market_info.get("yes_ask_dollars", 0) or (market_info.get("yes_ask", 0) / 100))
                else:
                    live_price = float(market_info.get("no_ask_dollars", 0) or (market_info.get("no_ask", 0) / 100))
            
            target_entry = float(pos['entry_price'])
            if live_price == 0.0:
                live_price = target_entry
                
            price_diff = live_price - target_entry
            
            # Reclassify edge status based on price shift
            if price_diff <= -0.02:
                edge_status = "🟢 EDGE BOOSTED (Contract is cheaper than analyzed!)"
                edge_color = "green"
            elif price_diff >= 0.02:
                edge_status = "🔴 EDGE REDUCED (Contract is more expensive)"
                edge_color = "red"
            else:
                edge_status = "🔵 ACTIVE EDGE (Live price matches original)"
                edge_color = "blue"

            # Dynamic contract auto-scaling logic based on price drift
            base_qty = int(pos['quantity'])
            auto_scaled_qty = base_qty
            
            if target_entry > 0 and live_price > 0:
                multiplier = target_entry / live_price
                if price_diff >= 0.05:
                    # Odds reduced -> scale down aggressively
                    multiplier = max(0.10, multiplier * 0.7)
                elif price_diff <= -0.05:
                    # Odds boosted -> scale up to lock in the edge
                    multiplier = min(2.0, multiplier * 1.3)
                auto_scaled_qty = max(1, int(round(base_qty * multiplier)))

            # Calculate execution stats statically (No interactive inputs)
            execution_price = live_price
            execution_qty = auto_scaled_qty
            
            cost = execution_qty * execution_price
            profit = (execution_qty * 1.00) - cost
            roi = (profit / cost) * 100 if cost > 0 else 0

            st.caption(f"**Ticker:** `{pos['market_id']}` | **Time till payout:** ⏳ `{time_left}` | [**🔗 View on Kalshi**]({kalshi_url})")
            st.markdown(f"**Live Odds Grading:** :{edge_color}[**{edge_status}**]")
            
            info_col, reason_col, action_col = st.columns([1.6, 2.4, 1])
            
            with info_col:
                st.markdown(f"**Side:** :{'red' if is_risky else 'blue'}[**{pos['side'].upper()}**]")
                
                st.metric("Execution Price", f"${execution_price:.2f}")
                st.metric("Contracts", f"{execution_qty}")
                
                # Help tips explaining auto-scaling
                if auto_scaled_qty > base_qty:
                    scale_tip = f"🤖 **Auto-scaled:** +{auto_scaled_qty - base_qty} contracts added"
                elif auto_scaled_qty < base_qty:
                    scale_tip = f"🤖 **Auto-scaled:** -{base_qty - auto_scaled_qty} contracts reduced"
                else:
                    scale_tip = "🤖 **Auto-scaled:** Size optimal"
                
                st.caption(scale_tip)
                st.caption(f"Original Target: ${target_entry:.2f}")
                st.markdown(f"**Total Cost:** `${cost:.2f}`")
                st.markdown(f"**Expected Profit:** :green[`${profit:.2f}`]")
                st.markdown(f"**ROI:** `+{roi:.1f}%`")

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
                        # Override database properties with the model's dynamic choices before placing order
                        pos['entry_price'] = execution_price
                        pos['quantity'] = execution_qty
                        if place_live_order(pos): st.rerun()

                if st.button("❌ Deny", type="primary", key=f"den_{pos['id']}", use_container_width=True):
                    update_db_status(pos['id'], False) 
                    st.rerun()
        st.markdown("---")

if __name__ == "__main__":
    main()