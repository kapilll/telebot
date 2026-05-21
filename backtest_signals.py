"""
Scrapes 3 months of Telegram channel history, parses all signals,
then checks each against MT5 historical XAUUSD data to see how often
the market followed the signal vs failed.

Usage: python backtest_signals.py
Output: backtest_results_TIMESTAMP.txt  +  backtest_results_TIMESTAMP.json

Outcomes per signal:
  all_tps_hit   — every numeric TP was reached before SL
  partial_tps   — some TPs hit, then SL (or time expired)
  sl_hit        — SL hit before any TP
  entry_missed  — price never entered the signal entry zone
  inconclusive  — not enough MT5 data (broker gap, weekend, etc.)
  no_mt5        — MT5 unavailable, signal parsed only
"""
import asyncio
import os
import sys
import json
import yaml
import struct
from datetime import datetime, timedelta, timezone
from typing import Optional
from dotenv import load_dotenv
from telethon import TelegramClient

load_dotenv()

try:
    import MetaTrader5 as mt5
    MT5_AVAILABLE = True
except ImportError:
    MT5_AVAILABLE = False

# Add project root to path
sys.path.insert(0, os.path.dirname(__file__))
from parser.signal_parser import SignalParser


# ── Tee: write to stdout + file simultaneously ────────────────────────────────

class Tee:
    def __init__(self, filepath):
        self.file = open(filepath, "w", encoding="utf-8")
        self._stdout = sys.stdout
    def write(self, data):
        self._stdout.write(data)
        self.file.write(data)
    def flush(self):
        self._stdout.flush()
        self.file.flush()
    def close(self):
        self.file.close()


# ── Outcome logic ─────────────────────────────────────────────────────────────

def check_signal_outcome(direction: str, entry: float, sl: float, tps: list,
                          bars, max_hours: int = 72) -> dict:
    """
    Walk M5 bars from signal time and determine what happened first.

    bars: numpy structured array from mt5.copy_rates_range with fields
          time, open, high, low, close, tick_volume, spread, real_volume
    """
    if bars is None or len(bars) == 0:
        return _result("inconclusive")

    # Verify entry zone was reached within first 4 hours (48 M5 bars)
    # For a SELL the entry zone means price was at/above entry range
    # For a BUY the entry zone means price was at/below entry range
    ENTRY_WINDOW_BARS = 48
    entry_hit = False
    for bar in bars[:ENTRY_WINDOW_BARS]:
        if direction == "BUY":
            if bar["low"] <= entry * 1.001:   # within 0.1% of entry (allow slight slippage)
                entry_hit = True
                break
        else:  # SELL
            if bar["high"] >= entry * 0.999:
                entry_hit = True
                break

    if not entry_hit:
        return _result("entry_missed")

    n_tps = len(tps)
    tps_hit = [False] * n_tps
    exit_price = None
    exit_bar_time = None
    sl_was_hit = False

    for bar in bars:
        bar_high = bar["high"]
        bar_low  = bar["low"]
        bar_time = datetime.fromtimestamp(int(bar["time"]), tz=timezone.utc)

        if direction == "BUY":
            # SL is below entry — check low first (pessimistic)
            if bar_low <= sl:
                sl_was_hit = True
                exit_price  = sl
                exit_bar_time = bar_time
                break
            for i, tp in enumerate(tps):
                if not tps_hit[i] and bar_high >= tp:
                    tps_hit[i] = True

        else:  # SELL
            # SL is above entry — check high first (pessimistic)
            if bar_high >= sl:
                sl_was_hit = True
                exit_price  = sl
                exit_bar_time = bar_time
                break
            for i, tp in enumerate(tps):
                if not tps_hit[i] and bar_low <= tp:
                    tps_hit[i] = True

    tps_hit_nums = [i + 1 for i, hit in enumerate(tps_hit) if hit]
    highest_tp_idx = max((i for i, h in enumerate(tps_hit) if h), default=-1)

    if sl_was_hit:
        if tps_hit_nums:
            outcome = "partial_tps"
            best_tp  = tps[highest_tp_idx]
            pips = (best_tp - entry) if direction == "BUY" else (entry - best_tp)
        else:
            outcome = "sl_hit"
            pips = (sl - entry) if direction == "BUY" else (entry - sl)   # negative
        exit_price = exit_price
    elif all(tps_hit):
        outcome    = "all_tps_hit"
        exit_price = tps[-1]
        pips       = (tps[-1] - entry) if direction == "BUY" else (entry - tps[-1])
    elif any(tps_hit):
        outcome    = "partial_tps"
        exit_price = tps[highest_tp_idx]
        pips       = (exit_price - entry) if direction == "BUY" else (entry - exit_price)
    else:
        outcome    = "inconclusive"
        pips       = None

    hours = None
    if exit_bar_time and len(bars) > 0:
        first_time = datetime.fromtimestamp(int(bars[0]["time"]), tz=timezone.utc)
        hours = round((exit_bar_time - first_time).total_seconds() / 3600, 1)

    return {
        "outcome": outcome,
        "tps_hit": tps_hit_nums,
        "exit_price": round(exit_price, 2) if exit_price else None,
        "pips_result": round(pips, 2) if pips is not None else None,
        "time_to_exit_hours": hours,
    }


def _result(outcome):
    return {"outcome": outcome, "tps_hit": [], "exit_price": None,
            "pips_result": None, "time_to_exit_hours": None}


# ── Main ──────────────────────────────────────────────────────────────────────

async def main():
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    out_txt  = f"backtest_results_{ts}.txt"
    out_json = f"backtest_results_{ts}.json"

    tee = Tee(out_txt)
    sys.stdout = tee

    print(f"Backtest run: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"Output: {out_txt}  |  {out_json}\n")

    with open("config.yaml") as f:
        config = yaml.safe_load(f)

    api_id       = int(os.getenv("TELEGRAM_API_ID"))
    api_hash     = os.getenv("TELEGRAM_API_HASH")
    channel      = config["signal_channel"]
    anthropic_key = os.getenv("ANTHROPIC_API_KEY")

    cutoff_date  = datetime.now(tz=timezone.utc) - timedelta(days=90)
    print(f"Period: {cutoff_date.strftime('%Y-%m-%d')} to today (90 days)")
    print(f"Channel ID: {channel}\n")

    parser  = SignalParser(anthropic_key)
    signals = []   # list of dicts: {msg, signal}

    # ── Phase 1: Scrape & parse ───────────────────────────────────────────────
    print("=" * 60)
    print("PHASE 1: Scraping Telegram history & parsing signals")
    print("=" * 60)

    session_path = os.path.join(os.path.dirname(__file__), "tradebot_session")
    client = TelegramClient(session_path, api_id, api_hash)
    await client.connect()
    if not await client.is_user_authorized():
        print("ERROR: Telethon session not authorized. Run main.py first to log in.")
        await client.disconnect()
        return

    total_msgs = 0
    try:
        channel_lookup = int(channel) if str(channel).lstrip("-").isdigit() else channel
        entity = None
        try:
            entity = await client.get_entity(channel_lookup)
        except Exception:
            print("Entity not cached -- scanning dialogs...")
            async for dialog in client.iter_dialogs():
                did    = dialog.entity.id
                needle = abs(int(channel))
                if str(needle).endswith(str(did)) or did == needle:
                    entity = dialog.entity
                    print(f"  Found: {dialog.name}")
                    break

        if entity is None:
            print(f"ERROR: Cannot find channel {channel}. Are you a member?")
            await client.disconnect()
            return

        print(f"\nIterating messages (newest to oldest, stopping at {cutoff_date.strftime('%Y-%m-%d')})...\n")

        async for msg in client.iter_messages(entity):
            if msg.date < cutoff_date:
                break

            total_msgs += 1

            if not msg.text or not msg.text.strip():
                continue

            signal = await parser.parse(msg.text)
            if signal:
                signals.append({"msg": msg, "signal": signal})
                idx = len(signals)
                print(f"  Signal #{idx:3d}  [{msg.date.strftime('%Y-%m-%d %H:%M')}]"
                      f"  {signal.direction:<4}  entry={signal.entry}"
                      f"  sl={signal.stop_loss}  tps={signal.take_profits}")

            if total_msgs % 250 == 0:
                print(f"  ... {total_msgs} messages processed, {len(signals)} signals found so far")

    finally:
        await client.disconnect()

    print(f"\nPhase 1 done: {total_msgs} messages scanned, {len(signals)} signals parsed\n")

    # ── Phase 2: MT5 backtest ─────────────────────────────────────────────────
    results = []

    if not MT5_AVAILABLE:
        print("=" * 60)
        print("PHASE 2 SKIPPED — MetaTrader5 package not installed")
        print("Install: pip install MetaTrader5")
        print("=" * 60)
        for item in signals:
            results.append(_build_record(item, _result("no_mt5")))
    else:
        print("=" * 60)
        print("PHASE 2: Fetching MT5 data & evaluating outcomes")
        print("=" * 60)

        if not mt5.initialize():
            print(f"  mt5.initialize() failed: {mt5.last_error()}")
            print("  Make sure MetaTrader5 terminal is running and logged in.")
            for item in signals:
                results.append(_build_record(item, _result("inconclusive")))
        else:
            info = mt5.terminal_info()
            print(f"  MT5 connected: {info.name if info else 'unknown'}\n")

            for i, item in enumerate(signals):
                msg    = item["msg"]
                signal = item["signal"]

                sig_time  = msg.date.replace(tzinfo=None)   # MT5 expects naive UTC
                end_time  = sig_time + timedelta(hours=72)

                bars = mt5.copy_rates_range("XAUUSD", mt5.TIMEFRAME_M5, sig_time, end_time)

                outcome_data = check_signal_outcome(
                    signal.direction, signal.entry, signal.stop_loss,
                    signal.take_profits, bars
                )

                results.append(_build_record(item, outcome_data))

                status_icon = {
                    "all_tps_hit": "[OK]",
                    "partial_tps": "[~~]",
                    "sl_hit":      "[X] ",
                    "entry_missed":"[?] ",
                    "inconclusive":"[-] ",
                }.get(outcome_data["outcome"], "    ")

                print(f"  [{i+1:3d}/{len(signals)}] {msg.date.strftime('%Y-%m-%d %H:%M')}"
                      f"  {signal.direction:<4}"
                      f"  {status_icon} {outcome_data['outcome']:<16}"
                      f"  TPs:{outcome_data['tps_hit']}"
                      f"  pips:{outcome_data['pips_result']}")

            mt5.shutdown()

    # ── Phase 3: Report ───────────────────────────────────────────────────────
    print("\n" + "=" * 60)
    print("BACKTEST REPORT")
    print("=" * 60)

    _print_report(results, total_msgs, cutoff_date)

    # Save JSON
    with open(out_json, "w", encoding="utf-8") as f:
        json.dump(results, f, indent=2, default=str)
    print(f"\nFull JSON results: {out_json}")
    print(f"Text report:       {out_txt}")


def _build_record(item, outcome_data: dict) -> dict:
    msg    = item["msg"]
    signal = item["signal"]
    return {
        "message_id":        msg.id,
        "timestamp":         msg.date.isoformat(),
        "direction":         signal.direction,
        "entry":             signal.entry,
        "stop_loss":         signal.stop_loss,
        "take_profits":      signal.take_profits,
        "raw_text":          signal.raw_text[:300],
        **outcome_data,
    }


def _print_report(results: list, total_msgs: int, cutoff_date: datetime):
    n = len(results)
    if n == 0:
        print("No signals found.")
        return

    def count(o): return sum(1 for r in results if r["outcome"] == o)
    def pct(k):   return 100 * count(k) / n

    all_tps  = count("all_tps_hit")
    partial  = count("partial_tps")
    sl_hit   = count("sl_hit")
    missed   = count("entry_missed")
    inconc   = count("inconclusive") + count("no_mt5")

    print(f"\nPeriod:   {cutoff_date.strftime('%Y-%m-%d')}  to  {datetime.now().strftime('%Y-%m-%d')}")
    print(f"Messages: {total_msgs}")
    print(f"Signals:  {n}")
    print()
    print(f"  [OK] All TPs hit (perfect follow): {all_tps:3d}  ({pct('all_tps_hit'):5.1f}%)")
    print(f"  [~~] Partial TPs, then SL/expiry: {partial:3d}  ({pct('partial_tps'):5.1f}%)")
    print(f"  [X]  SL hit (no TPs):              {sl_hit:3d}  ({pct('sl_hit'):5.1f}%)")
    print(f"  [?]  Entry zone never reached:     {missed:3d}  ({pct('entry_missed'):5.1f}%)")
    print(f"  [-]  Inconclusive / no MT5 data:   {inconc:3d}  ({100*inconc/n:5.1f}%)")

    # TP-by-TP hit rates
    max_tps = max((len(r["take_profits"]) for r in results), default=0)
    if max_tps > 0:
        print(f"\nTP Hit Rates (of all {n} signals):")
        for tp_n in range(1, max_tps + 1):
            eligible = sum(1 for r in results if len(r["take_profits"]) >= tp_n)
            hit = sum(1 for r in results if tp_n in r.get("tps_hit", []))
            if eligible:
                bar = "#" * int(20 * hit / eligible) + "." * (20 - int(20 * hit / eligible))
                print(f"  TP{tp_n}: {bar}  {hit}/{eligible}  ({100*hit/eligible:.1f}%)")

    # Pips analysis
    pips_list = [r["pips_result"] for r in results if r["pips_result"] is not None]
    if pips_list:
        wins   = [p for p in pips_list if p > 0]
        losses = [p for p in pips_list if p < 0]
        print(f"\nPips (resolved trades only, n={len(pips_list)}):")
        print(f"  Average result: {sum(pips_list)/len(pips_list):+.1f} pips")
        if wins:
            print(f"  Avg win:        {sum(wins)/len(wins):+.1f} pips")
        if losses:
            print(f"  Avg loss:       {sum(losses)/len(losses):+.1f} pips")
        if wins and losses:
            rr = abs(sum(wins)/len(wins)) / abs(sum(losses)/len(losses))
            print(f"  Avg R:R ratio:  {rr:.2f}")

    # Direction split
    buys  = [r for r in results if r["direction"] == "BUY"]
    sells = [r for r in results if r["direction"] == "SELL"]
    print(f"\nDirection split:  {len(buys)} BUY  /  {len(sells)} SELL")

    # Detailed table
    print(f"\n{'-'*90}")
    print(f"{'Date':<18} {'Dir':<5} {'Entry':>7} {'SL':>7} {'Outcome':<17} {'TPs Hit':<12} {'Pips':>7}  {'Hrs':>5}")
    print(f"{'-'*18} {'-'*5} {'-'*7} {'-'*7} {'-'*17} {'-'*12} {'-'*7}  {'-'*5}")
    for r in sorted(results, key=lambda x: x["timestamp"]):
        dt   = datetime.fromisoformat(r["timestamp"]).strftime("%Y-%m-%d %H:%M")
        tps  = str(r["tps_hit"]) if r["tps_hit"] else "-"
        pips = f"{r['pips_result']:+.1f}" if r["pips_result"] is not None else "-"
        hrs  = f"{r['time_to_exit_hours']:.1f}" if r["time_to_exit_hours"] else "-"
        print(f"{dt:<18} {r['direction']:<5} {r['entry']:>7.1f} {r['stop_loss']:>7.1f}"
              f" {r['outcome']:<17} {tps:<12} {pips:>7}  {hrs:>5}")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    finally:
        if isinstance(sys.stdout, Tee):
            sys.stdout.close()
            sys.stdout = sys.__stdout__
