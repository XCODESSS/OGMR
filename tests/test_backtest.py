import csv
import json
from pathlib import Path
import tempfile
import unittest

from gapfade import backtest, data, demo
from gapfade.config import Config
from gapfade.model import TradingError, number


class BacktestTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.inputs = self.root / "inputs"
        demo.create(self.inputs)

    def tearDown(self):
        self.tmp.cleanup()

    def run_test(self, config=None):
        return backtest.run(config or Config(), self.inputs / "universe.csv", self.inputs / "sessions.csv",
                            self.inputs / "references.csv", self.inputs / "bars.csv", self.root / "result")

    def test_full_replay_accounting_long_short_and_reproducibility(self):
        summary = self.run_test()
        trades = data.rows(self.root / "result/trades.csv")
        self.assertEqual(len(trades), 4)
        self.assertEqual({int(t["side"]) for t in trades}, {-1, 1})
        for trade in trades:
            gross = number(trade["quantity"]) * number(trade["side"]) * (number(trade["exit_price"]) - number(trade["entry_price"]))
            self.assertEqual(gross, number(trade["gross_pnl"]))
            self.assertEqual(gross - number(trade["fees"]), number(trade["net_pnl"]))
        self.assertEqual(sum((number(t["net_pnl"]) for t in trades), number(0)), number(summary["net_profit"]))
        second = self.root / "result2"
        other = backtest.run(Config(), self.inputs / "universe.csv", self.inputs / "sessions.csv",
                             self.inputs / "references.csv", self.inputs / "bars.csv", second)
        self.assertEqual(summary, other)
        self.assertEqual((self.root / "result/trades.csv").read_bytes(), (second / "trades.csv").read_bytes())

    def test_missing_exit_bar_aborts_instead_of_dropping_trade(self):
        path = self.inputs / "bars.csv"
        records = data.rows(path)
        records = [r for r in records if not (r["timestamp"].endswith("15:00:00+05:30") and r["symbol"] == "NSE:DEMOA-EQ")]
        data.write_csv(path, records, list(records[0]))
        with self.assertRaisesRegex(TradingError, "Missing/untradable"):
            self.run_test()
        self.assertFalse((self.root / "result").exists())

    def test_duplicate_and_invalid_bars_fail(self):
        path = self.inputs / "bars.csv"
        records = data.rows(path)
        data.write_csv(path, records + [records[0]], list(records[0]))
        with self.assertRaisesRegex(TradingError, "Duplicate bar"):
            self.run_test()

    def test_daily_loss_exits_on_next_bar_not_observed_close(self):
        path = self.inputs / "bars.csv"
        records = data.rows(path)
        for r in records:
            if r["timestamp"].startswith("2026-01-05T09:16"):
                price = "85" if r["symbol"] == "NSE:DEMOA-EQ" else "115"
                r["close"] = price
                r["low"] = min(r["open"], price, key=float)
                r["high"] = max(r["open"], price, key=float)
        data.write_csv(path, records, list(records[0]))
        self.run_test()
        trades = data.rows(self.root / "result/trades.csv")
        for t in trades[:2]:
            self.assertEqual(t["exit_reason"], "daily_loss_limit")
            self.assertIn("09:17:00", t["exit_time"])
            self.assertNotEqual(number(t["exit_price"]), number("85"))

    def test_calendar_gap_fails(self):
        path = self.inputs / "sessions.csv"
        path.write_text("session,previous_session\n2026-01-05,2026-01-02\n2026-01-07,2026-01-06\n")
        with self.assertRaisesRegex(TradingError, "Calendar gap"):
            data.sessions(path)


if __name__ == "__main__":
    unittest.main()
