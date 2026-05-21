import asyncio
import csv
from pathlib import Path

import aiosqlite
import pandas as pd
from anthropic import Anthropic
from tabulate import tabulate

from utils.logger import get_logger

logger = get_logger("analytics")


class AnalyticsEngine:
    def __init__(self, db_path: str, anthropic_api_key: str):
        self.db_path = db_path
        self.client = Anthropic(api_key=anthropic_api_key)

    async def run_report(self) -> None:
        print("\n" + "=" * 80)
        print("GOLD TRADING ANALYTICS REPORT")
        print("=" * 80)

        print("\n--- PROVIDER TRACK RECORD ---")
        tr = await self.provider_track_record()
        print(tabulate([
            ["Total Signals", tr["total_signals"]],
            ["Total Trades", tr["total_trades"]],
            ["Total PnL (USD)", f"${tr['total_pnl_usd']:.2f}"],
        ], headers=["Metric", "Value"], tablefmt="grid"))

        print("\n--- WIN RATE ---")
        wr = await self.win_rate()
        for symbol, dirs in wr.items():
            for direction, data in dirs.items():
                print(f"{symbol} {direction.upper()}: "
                      f"{data['tp_count']} wins / {data['sl_count']} losses "
                      f"({data['win_rate']:.1%})")

        print("\n--- AVERAGE R:R ---")
        print(f"{await self.avg_rr():.2f}")

        print("\n--- DRAWDOWN ---")
        dd = await self.max_drawdown()
        print(tabulate([
            ["Max Consecutive Losses", dd["max_consecutive_losses"]],
            ["Max Equity Dip (USD)", f"${dd['max_equity_dip']:.2f}"],
        ], headers=["Metric", "Value"], tablefmt="grid"))

        print("\n--- TIME OF DAY ---")
        tod = await self.time_of_day_analysis()
        if not tod.empty:
            print(tabulate(tod, headers=tod.columns, tablefmt="grid", showindex=False))

        print("\n--- PATTERN ANALYSIS ---")
        print(await self.pattern_analysis())

        print("\n" + "=" * 80)

    async def win_rate(self) -> dict:
        result = {}
        async with aiosqlite.connect(self.db_path) as db:
            cursor = await db.execute("""
                SELECT s.symbol, s.direction, o.exit_reason
                FROM signals s
                JOIN trades t ON t.signal_id = s.id
                JOIN outcomes o ON o.trade_id = t.id
                WHERE o.exit_reason IS NOT NULL
            """)
            rows = await cursor.fetchall()
            agg = {}
            for symbol, direction, reason in rows:
                key = (symbol, (direction or "").lower())
                if key not in agg:
                    agg[key] = {"tp": 0, "sl": 0}
                if str(reason).startswith("TP"):
                    agg[key]["tp"] += 1
                else:
                    agg[key]["sl"] += 1
            for (sym, d), counts in agg.items():
                if sym not in result:
                    result[sym] = {}
                total = counts["tp"] + counts["sl"]
                result[sym][d] = {
                    "tp_count": counts["tp"],
                    "sl_count": counts["sl"],
                    "win_rate": counts["tp"] / total if total else 0,
                }
        return result

    async def avg_rr(self) -> float:
        async with aiosqlite.connect(self.db_path) as db:
            cursor = await db.execute("""
                SELECT s.entry, s.stop_loss, s.direction, o.pnl_pips
                FROM signals s
                JOIN trades t ON t.signal_id = s.id
                JOIN outcomes o ON o.trade_id = t.id
                WHERE o.pnl_pips IS NOT NULL
            """)
            rows = await cursor.fetchall()
            rr_vals = []
            for entry, sl, direction, pnl_pips in rows:
                if entry and sl and entry != sl:
                    risk_pips = abs(entry - sl)
                    rr = pnl_pips / risk_pips if risk_pips else 0
                    rr_vals.append(rr)
            return sum(rr_vals) / len(rr_vals) if rr_vals else 0.0

    async def max_drawdown(self) -> dict:
        async with aiosqlite.connect(self.db_path) as db:
            cursor = await db.execute("""
                SELECT o.exit_reason, o.pnl_usd
                FROM outcomes o
                JOIN trades t ON t.id = o.trade_id
                ORDER BY t.closed_at ASC
            """)
            rows = await cursor.fetchall()
            max_consec = cur_consec = 0
            max_dip = running = 0.0
            for reason, pnl in rows:
                pnl = pnl or 0
                running += pnl
                if not str(reason).startswith("TP"):
                    cur_consec += 1
                    max_consec = max(max_consec, cur_consec)
                else:
                    cur_consec = 0
                if running < 0:
                    max_dip = min(max_dip, running)
        return {"max_consecutive_losses": max_consec,
                "max_equity_dip": abs(max_dip)}

    async def time_of_day_analysis(self) -> pd.DataFrame:
        async with aiosqlite.connect(self.db_path) as db:
            cursor = await db.execute("""
                SELECT strftime('%H', s.timestamp) as hour, o.exit_reason
                FROM signals s
                JOIN trades t ON t.signal_id = s.id
                JOIN outcomes o ON o.trade_id = t.id
                WHERE o.exit_reason IS NOT NULL
                ORDER BY hour
            """)
            rows = await cursor.fetchall()
            hour_data = {}
            for hour, reason in rows:
                if hour not in hour_data:
                    hour_data[hour] = {"tp": 0, "sl": 0}
                if str(reason).startswith("TP"):
                    hour_data[hour]["tp"] += 1
                else:
                    hour_data[hour]["sl"] += 1
            data = []
            for h in sorted(hour_data):
                c = hour_data[h]
                total = c["tp"] + c["sl"]
                data.append({"Hour": f"{h}:00", "TP": c["tp"], "SL": c["sl"],
                             "Win Rate": f"{c['tp']/total:.1%}" if total else "0%"})
        return pd.DataFrame(data)

    async def pattern_analysis(self) -> str:
        async with aiosqlite.connect(self.db_path) as db:
            cursor = await db.execute("""
                SELECT s.symbol, s.direction, s.entry, s.stop_loss,
                       s.take_profits, s.timestamp, o.exit_reason
                FROM signals s
                LEFT JOIN trades t ON t.signal_id = s.id
                LEFT JOIN outcomes o ON o.trade_id = t.id
                ORDER BY s.timestamp DESC LIMIT 50
            """)
            rows = await cursor.fetchall()
        if not rows:
            return "No signals yet."
        lines = ["Recent signals:"]
        for symbol, direction, entry, sl, tps, ts, reason in rows:
            lines.append(f"  {ts} {symbol} {direction} entry={entry} sl={sl} "
                         f"tps={tps} outcome={reason or 'open'}")
        response = self.client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=1024,
            messages=[{"role": "user", "content":
                       "Analyze these gold trading signals for patterns and insights:\n\n"
                       + "\n".join(lines)}],
        )
        return response.content[0].text

    async def provider_track_record(self) -> dict:
        async with aiosqlite.connect(self.db_path) as db:
            total_signals = (await (await db.execute("SELECT COUNT(*) FROM signals")).fetchone())[0]
            total_trades = (await (await db.execute(
                "SELECT COUNT(*) FROM outcomes WHERE exit_reason IS NOT NULL"
            )).fetchone())[0]
            row = await (await db.execute(
                "SELECT COALESCE(SUM(pnl_pips),0), COALESCE(SUM(pnl_usd),0) FROM outcomes"
            )).fetchone()
        return {"total_signals": total_signals, "total_trades": total_trades,
                "total_pnl_pips": row[0], "total_pnl_usd": row[1]}

    async def export_csv(self, output_path: str) -> None:
        async with aiosqlite.connect(self.db_path) as db:
            cursor = await db.execute("""
                SELECT s.id, s.symbol, s.direction, s.entry, s.stop_loss,
                       s.take_profits, s.timestamp, o.exit_reason,
                       o.exit_price, o.pnl_pips, o.pnl_usd
                FROM signals s
                LEFT JOIN trades t ON t.signal_id = s.id
                LEFT JOIN outcomes o ON o.trade_id = t.id
                ORDER BY s.timestamp DESC
            """)
            rows = await cursor.fetchall()
        out = Path(output_path)
        out.parent.mkdir(parents=True, exist_ok=True)
        with open(out, "w", newline="") as f:
            w = csv.writer(f)
            w.writerow(["Signal ID", "Symbol", "Direction", "Entry", "SL",
                        "TPs", "Time", "Outcome", "Exit Price", "PnL Pips", "PnL USD"])
            w.writerows(rows)
        print(f"CSV exported to {output_path}")


async def main():
    import os
    from dotenv import load_dotenv
    load_dotenv()
    engine = AnalyticsEngine("tradebot.db", os.getenv("ANTHROPIC_API_KEY", ""))
    await engine.run_report()


if __name__ == "__main__":
    asyncio.run(main())
