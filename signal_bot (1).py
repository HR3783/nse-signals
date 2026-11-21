"""
Free NSE signal scanner -> Telegram.

Same logic as the TradingView indicator (15m chart):
  BUY breakout : close crosses above the first-15m-candle high, above VWAP, volume > 1.5x avg
  BUY pullback : after a breakout, dip to 20-EMA then a green candle closing back above it (and above VWAP)
  EXIT         : close drops below VWAP after a breakout, or below the opening-range low

Setup (free):
  pip install yfinance pandas requests
  Telegram: message @BotFather -> /newbot -> copy token.
            Send any message to your bot, then open
            https://api.telegram.org/bot<TOKEN>/getUpdates and copy "chat":{"id":...}
  export TELEGRAM_TOKEN=xxxx
  export TELEGRAM_CHAT_ID=yyyy
  python signal_bot.py          # loops during market hours
  python signal_bot.py --once   # single scan (use with cron / GitHub Actions)

Data comes from Yahoo Finance (free, can be delayed a few minutes). Not trading advice.
"""
import argparse
import json
import os
import time
from datetime import datetime
from zoneinfo import ZoneInfo

import pandas as pd
import requests

WATCHLIST = [
    "APOLLOHOSP", "MAXHEALTH", "AXISBANK", "ULTRACEMCO",
    "HDFCBANK", "ICICIBANK", "RELIANCE", "TCS", "INFY", "SBIN",
    "LT", "BAJFINANCE", "TATAMOTORS", "SUNPHARMA", "ADANIENT",
]  # add NSE symbols without ".NS"

GAP_PCT = 0.5      # only used to tag "GAP-UP" in the message
VOL_MULT = 1.5
RR = 2.0           # target = entry + RR * (entry - stop)
EMA_LEN = 20
BAR_MINUTES = 15
SCAN_EVERY_SEC = 300
SIGNALS_FILE = "signals.json"  # also read by the website (index.html)
IST = ZoneInfo("Asia/Kolkata")


def evaluate(df: pd.DataFrame, now=None):
    """Return a list of signal dicts for the latest COMPLETED 15m bar. df = OHLCV, tz-aware index."""
    if df is None or df.empty:
        return []
    df = df.copy()
    now = now or pd.Timestamp.now(tz=df.index.tz)
    df = df[df.index + pd.Timedelta(minutes=BAR_MINUTES) <= now]  # drop the still-forming bar
    if len(df) < 25:
        return []

    df["day"] = df.index.date
    df["vol_avg"] = df["Volume"].rolling(20).mean()
    df["ema"] = df["Close"].ewm(span=EMA_LEN, adjust=False).mean()

    today = df["day"].iloc[-1]
    t = df[df["day"] == today].copy()
    prev_days = df[df["day"] < today]
    if len(t) < 2 or prev_days.empty:
        return []

    prev_close = prev_days["Close"].iloc[-1]
    day_open = t["Open"].iloc[0]
    or_high, or_low = t["High"].iloc[0], t["Low"].iloc[0]

    tp = (t["High"] + t["Low"] + t["Close"]) / 3
    t["vwap"] = (tp * t["Volume"]).cumsum() / t["Volume"].cumsum()

    last, prev = t.iloc[-1], t.iloc[-2]
    gap_up = day_open > prev_close * (1 + GAP_PCT / 100)
    broke_before = (t["Close"].iloc[:-1] > or_high).any()
    out = []

    if (last.Close > or_high and prev.Close <= or_high and last.Close > last.vwap
            and last.Volume > VOL_MULT * last.vol_avg):
        out.append(("BUY breakout", last.Close, or_low))

    if (broke_before and last.Low <= last.ema * 1.001 and last.Close > last.ema
            and last.Close > last.Open and last.Close > last.vwap):
        out.append(("BUY pullback", last.Close, last.Low))

    if (broke_before and last.Close < last.vwap and prev.Close >= prev.vwap) or \
       (last.Close < or_low and prev.Close >= or_low):
        out.append(("EXIT / SELL", last.Close, None))

    res = []
    for kind, price, stop in out:
        d = {"kind": kind, "price": float(price), "bar": str(t.index[-1]), "gap": bool(gap_up)}
        if stop is not None and price > stop:
            d["stop"] = float(stop)
            d["target"] = float(price + RR * (price - stop))
        res.append(d)
    return res


def fetch(symbol: str) -> pd.DataFrame:
    import yfinance as yf  # imported here so evaluate() can be tested without it
    return yf.Ticker(symbol + ".NS").history(period="5d", interval="15m")


def format_msg(sym, s):
    lines = [f"{'🟢' if s['kind'].startswith('BUY') else '🔴'} {sym}  {s['kind']}"
             + ("  (gap-up day)" if s["gap"] else ""),
             f"Price: {s['price']:.2f}"]
    if "stop" in s:
        lines.append(f"Stop: {s['stop']:.2f}   Target ({RR:g}R): {s['target']:.2f}")
    lines.append(f"Bar: {s['bar'][:16]}")
    return "\n".join(lines)


def notify(text):
    token, chat = os.getenv("TELEGRAM_TOKEN"), os.getenv("TELEGRAM_CHAT_ID")
    print(text, "\n")
    if token and chat:
        try:
            requests.post(f"https://api.telegram.org/bot{token}/sendMessage",
                          data={"chat_id": chat, "text": text}, timeout=15)
        except requests.RequestException as e:
            print("Telegram error:", e)


def load_signals():
    """Today's already-found signals (signals.json doubles as the de-duplication memory)."""
    try:
        with open(SIGNALS_FILE) as f:
            old = json.load(f).get("signals", [])
    except (FileNotFoundError, json.JSONDecodeError):
        return []
    today = datetime.now(IST).strftime("%Y-%m-%d")
    return [x for x in old if x.get("bar", "")[:10] == today]


def save_signals(signals):
    data = {"updated": datetime.now(IST).isoformat(timespec="seconds"),
            "watchlist": WATCHLIST, "signals": signals}
    with open(SIGNALS_FILE, "w") as f:
        json.dump(data, f, indent=1)


def scan_once():
    signals = load_signals()
    seen = {(x["symbol"], x["bar"], x["kind"]) for x in signals}
    today = datetime.now(IST).strftime("%Y-%m-%d")  # ignore leftovers from earlier days / weekends
    for sym in WATCHLIST:
        try:
            sigs = evaluate(fetch(sym))
        except Exception as e:  # network / bad symbol: skip, keep going
            print(f"{sym}: {e}")
            continue
        for s in sigs:
            if s["bar"][:10] != today:
                continue
            if (sym, s["bar"], s["kind"]) not in seen:
                signals.append({"symbol": sym, **s})
                notify(format_msg(sym, s))
    save_signals(signals)


def market_open(now):
    if now.weekday() >= 5:
        return False
    return (now.hour, now.minute) >= (9, 30) and (now.hour, now.minute) <= (15, 30)


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--once", action="store_true", help="scan once and exit")
    args = ap.parse_args()
    if args.once:
        scan_once()
    else:
        while True:
            if market_open(datetime.now(IST)):
                scan_once()
            time.sleep(SCAN_EVERY_SEC)
