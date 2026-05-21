import asyncio
import csv
from datetime import datetime
from pathlib import Path
from typing import Optional

import aiosqlite
import pandas as pd
from anthropic import Anthropic
from tabulate import tabulate


class AnalyticsEngine:
    def __init__(self, db_path: str, anthropic_api_key: str):
        self.db_path = db_path
        self.anthropic_client = Anthropic(api_key=anthropic_api_key)

    async def run_report(self) -> None:
        """Run all analyses and print to console."""
        print("\n" + "=" * 80)
        print("GOLD TRADING ANALYTICS REPORT")
        print("=" * 80)

        # Provider track record
        print("\n--- PROVIDER TRACK RECORD ---")
        track_record = await self.provider_track_record()
        if track_record:
            track_data = [
                ["Total Signals", track_record.get("total_signals", 0)],
                ["Total Trades", track_record.get("total_trades", 0)],
                ["Total PnL (Pips)", f"{track_record.get('total_pnl_pips', 0):.2f}"],
                ["Total PnL (USD)", f"${track_record.get('total_pnl_usd', 0):.2f}"],
            ]
            print(tabulate(track_data, headers=["Metric", "Value"], tablefmt="grid"))

        # Win rate
        print("\n--- WIN RATE ANALYSIS ---")
        win_rate_data = await self.win_rate()
        if win_rate_data:
            for symbol, directions in win_rate_data.items():
                print(f"\n{symbol}:")
                table = []
                for direction, rates in directions.items():
                    table.append(
                        [
                            direction.upper(),
                            f"{rates.get('tp_count', 0)}",
                            f"{rates.get('sl_count', 0)}",
                            f"{rates.get('win_rate', 0):.2%}",
                        ]
                    )
                print(
                    tabulate(
                        table,
                        headers=["Direction", "TP Hits", "SL Hits", "Win Rate"],
                        tablefmt="grid",
                    )
                )

        # Average R:R
        print("\n--- RISK:REWARD ANALYSIS ---")
        avg_rr = await self.avg_rr()
        print(f"Average R:R: {avg_rr:.2f}")

        # Max drawdown
        print("\n--- DRAWDOWN ANALYSIS ---")
        max_dd = await self.max_drawdown()
        if max_dd:
            dd_data = [
                ["Max Consecutive Losses", max_dd.get("max_consecutive_losses", 0)],
                ["Max Equity Dip (Pips)", f"{max_dd.get('max_equity_dip', 0):.2f}"],
            ]
            print(tabulate(dd_data, headers=["Metric", "Value"], tablefmt="grid"))

        # Time of day analysis
        print("\n--- TIME OF DAY ANALYSIS ---")
        time_analysis = await self.time_of_day_analysis()
        if not time_analysis.empty:
            print(
                tabulate(
                    time_analysis.reset_index(),
                    headers=time_analysis.reset_index().columns,
                    tablefmt="grid",
                    showindex=False,
                )
            )

        # Pattern analysis
        print("\n--- PATTERN ANALYSIS ---")
        pattern_text = await self.pattern_analysis()
        if pattern_text:
            print(pattern_text)

        print("\n" + "=" * 80)

    async def win_rate(self) -> dict:
        """
        Calculate % TP vs SL outcomes, by symbol and direction.

        Returns:
            dict: {symbol: {direction: {tp_count, sl_count, win_rate}}}
        """
        result = {}

        async with aiosqlite.connect(self.db_path) as db:
            query = """
            SELECT
                s.symbol,
                s.direction,
                CASE WHEN o.outcome = 'TP' THEN 1 ELSE 0 END as tp_count,
                CASE WHEN o.outcome = 'SL' THEN 1 ELSE 0 END as sl_count
            FROM signals s
            LEFT JOIN outcomes o ON s.id = o.signal_id
            WHERE o.outcome IS NOT NULL
            """
            cursor = await db.execute(query)
            rows = await cursor.fetchall()

            # Aggregate by symbol and direction
            symbol_dir_data = {}
            for row in rows:
                symbol, direction, tp, sl = row
                key = (symbol, direction.lower())
                if key not in symbol_dir_data:
                    symbol_dir_data[key] = {"tp": 0, "sl": 0}
                symbol_dir_data[key]["tp"] += tp
                symbol_dir_data[key]["sl"] += sl

            # Convert to result format
            for (symbol, direction), counts in symbol_dir_data.items():
                if symbol not in result:
                    result[symbol] = {}
                total = counts["tp"] + counts["sl"]
                win_rate = counts["tp"] / total if total > 0 else 0
                result[symbol][direction] = {
                    "tp_count": counts["tp"],
                    "sl_count": counts["sl"],
                    "win_rate": win_rate,
                }

        return result

    async def avg_rr(self) -> float:
        """
        Calculate average Risk:Reward ratio from outcomes.

        Returns:
            float: Average R:R
        """
        async with aiosqlite.connect(self.db_path) as db:
            query = """
            SELECT AVG(o.rr) as avg_rr
            FROM outcomes o
            WHERE o.rr IS NOT NULL AND o.rr > 0
            """
            cursor = await db.execute(query)
            row = await cursor.fetchone()
            return row[0] if row and row[0] else 0.0

    async def max_drawdown(self) -> dict:
        """
        Calculate max consecutive losses and max equity dip.

        Returns:
            dict: {max_consecutive_losses, max_equity_dip}
        """
        async with aiosqlite.connect(self.db_path) as db:
            # Get all outcomes ordered by date
            query = """
            SELECT o.outcome, o.pnl_pips
            FROM outcomes o
            ORDER BY o.created_at ASC
            """
            cursor = await db.execute(query)
            rows = await cursor.fetchall()

            max_consecutive = 0
            current_consecutive = 0
            max_equity_dip = 0
            running_pnl = 0

            for outcome, pnl_pips in rows:
                pnl = pnl_pips if pnl_pips else 0
                running_pnl += pnl

                if outcome == "SL":
                    current_consecutive += 1
                    max_consecutive = max(max_consecutive, current_consecutive)
                else:
                    current_consecutive = 0

                if running_pnl < 0:
                    max_equity_dip = min(max_equity_dip, running_pnl)

        return {
            "max_consecutive_losses": max_consecutive,
            "max_equity_dip": abs(max_equity_dip),
        }

    async def time_of_day_analysis(self) -> pd.DataFrame:
        """
        Group outcomes by hour, show win rate per hour.

        Returns:
            pd.DataFrame: Hour, TP Count, SL Count, Win Rate
        """
        async with aiosqlite.connect(self.db_path) as db:
            query = """
            SELECT
                strftime('%H', o.created_at) as hour,
                o.outcome
            FROM outcomes o
            WHERE o.outcome IS NOT NULL
            ORDER BY hour
            """
            cursor = await db.execute(query)
            rows = await cursor.fetchall()

            # Group by hour
            hour_data = {}
            for hour, outcome in rows:
                if hour not in hour_data:
                    hour_data[hour] = {"tp": 0, "sl": 0}
                if outcome == "TP":
                    hour_data[hour]["tp"] += 1
                else:
                    hour_data[hour]["sl"] += 1

            # Create DataFrame
            data = []
            for hour in sorted(hour_data.keys()):
                counts = hour_data[hour]
                total = counts["tp"] + counts["sl"]
                win_rate = counts["tp"] / total if total > 0 else 0
                data.append(
                    {
                        "Hour": f"{hour}:00",
                        "TP": counts["tp"],
                        "SL": counts["sl"],
                        "Win Rate": f"{win_rate:.2%}",
                    }
                )

            return pd.DataFrame(data)

    async def pattern_analysis(self) -> str:
        """
        Fetch all signals from DB, send to Claude Sonnet for pattern detection.

        Returns:
            str: Analysis text from Claude
        """
        async with aiosqlite.connect(self.db_path) as db:
            query = """
            SELECT
                s.symbol,
                s.direction,
                s.entry_price,
                s.tp_price,
                s.sl_price,
                s.created_at,
                o.outcome
            FROM signals s
            LEFT JOIN outcomes o ON s.id = o.signal_id
            ORDER BY s.created_at DESC
            LIMIT 50
            """
            cursor = await db.execute(query)
            rows = await cursor.fetchall()

            if not rows:
                return "No signals available for pattern analysis."

            # Format signal data for Claude
            signals_text = "Recent Trading Signals:\n\n"
            for row in rows:
                symbol, direction, entry, tp, sl, created_at, outcome = row
                outcome_str = outcome if outcome else "Pending"
                signals_text += f"- Symbol: {symbol}, Direction: {direction}, Entry: {entry}, TP: {tp}, SL: {sl}, Outcome: {outcome_str}, Time: {created_at}\n"

            # Send to Claude Sonnet
            message = self.anthropic_client.messages.create(
                model="claude-sonnet-4-6",
                max_tokens=1024,
                messages=[
                    {
                        "role": "user",
                        "content": f"""You are analyzing gold trading signals. Identify recurring patterns, best-performing setups, and the provider's apparent strategy.

{signals_text}

Provide a concise analysis of:
1. Most common signal types and their win rates
2. Best performing direction (long/short)
3. Entry/exit behavior patterns
4. Key strategy observations""",
                    }
                ],
            )

            return message.content[0].text

    async def provider_track_record(self) -> dict:
        """
        Get total signals, total trades, total PnL pips/USD.

        Returns:
            dict: {total_signals, total_trades, total_pnl_pips, total_pnl_usd}
        """
        async with aiosqlite.connect(self.db_path) as db:
            # Count signals
            cursor = await db.execute("SELECT COUNT(*) FROM signals")
            total_signals = (await cursor.fetchone())[0]

            # Count trades with outcomes
            cursor = await db.execute(
                "SELECT COUNT(*) FROM outcomes WHERE outcome IS NOT NULL"
            )
            total_trades = (await cursor.fetchone())[0]

            # Sum PnL
            cursor = await db.execute("SELECT SUM(pnl_pips), SUM(pnl_usd) FROM outcomes")
            row = await cursor.fetchone()
            total_pnl_pips = row[0] if row[0] else 0
            total_pnl_usd = row[1] if row[1] else 0

        return {
            "total_signals": total_signals,
            "total_trades": total_trades,
            "total_pnl_pips": total_pnl_pips,
            "total_pnl_usd": total_pnl_usd,
        }

    async def export_csv(self, output_path: str) -> None:
        """
        Export trades and outcomes as CSV.

        Args:
            output_path: Path to save CSV file
        """
        async with aiosqlite.connect(self.db_path) as db:
            query = """
            SELECT
                s.id,
                s.symbol,
                s.direction,
                s.entry_price,
                s.tp_price,
                s.sl_price,
                s.created_at,
                o.outcome,
                o.exit_price,
                o.pnl_pips,
                o.pnl_usd,
                o.rr
            FROM signals s
            LEFT JOIN outcomes o ON s.id = o.signal_id
            ORDER BY s.created_at DESC
            """
            cursor = await db.execute(query)
            rows = await cursor.fetchall()

        # Write to CSV
        output_file = Path(output_path)
        output_file.parent.mkdir(parents=True, exist_ok=True)

        with open(output_file, "w", newline="") as f:
            writer = csv.writer(f)
            writer.writerow(
                [
                    "Signal ID",
                    "Symbol",
                    "Direction",
                    "Entry Price",
                    "TP Price",
                    "SL Price",
                    "Signal Time",
                    "Outcome",
                    "Exit Price",
                    "PnL Pips",
                    "PnL USD",
                    "R:R",
                ]
            )
            for row in rows:
                writer.writerow(row)

        print(f"CSV exported to {output_path}")


async def main():
    """Main entry point."""
    import os

    db_path = "trades.db"
    api_key = os.getenv("ANTHROPIC_API_KEY", "")

    if not api_key:
        print("Error: ANTHROPIC_API_KEY environment variable not set")
        return

    engine = AnalyticsEngine(db_path, api_key)
    await engine.run_report()


if __name__ == "__main__":
    asyncio.run(main())
