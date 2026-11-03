from dataclasses import replace
from datetime import date, timedelta
from decimal import Decimal as D
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch
from test_execution import FakeFyers

from gapfade import data, demo
from gapfade.config import Config
from gapfade.model import Quote, TradingError, at
from gapfade.runner import run_session


class ClockFeed:
    def __init__(self, day):
        self.current = at(day, "09:15:30")
        self.calls = 0

    def now(self):
        return self.current

    def sleep(self, seconds):
        self.current += timedelta(seconds=seconds)

    def quotes(self, symbols):
        self.calls += 1
        return {s: Quote(s, self.current, D("96.99") if "A" in s else D("102.99"),
                         D("97.01") if "A" in s else D("103.01"),
                         D("97") if "A" in s else D("103"), D("100")) for s in symbols}


class RunnerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.input = self.root / "inputs"
        demo.create(self.input)
        self.day = date(2026, 1, 5)
        self.feed = ClockFeed(self.day)
        self.config = Config(exit_time="09:17:00")

    def tearDown(self):
        self.tmp.cleanup()

    def run_session(self, **kwargs):
        return run_session(self.config, self.feed, self.input / "universe.csv", self.input / "sessions.csv",
                           self.input / "references.csv", self.root / "paper.sqlite", mode="paper",
                           kill_file=self.root / "STOP", now_fn=self.feed.now, sleep=self.feed.sleep, **kwargs)

    def test_paper_session_restarts_without_duplicate_fills(self):
        state = self.run_session()
        self.assertEqual(state["phase"], "closed")
        self.assertEqual(len(state["orders"]), 4)
        self.assertEqual({o["side"] for o in state["orders"].values() if o["role"] == "entry"}, {1, -1})
        second = self.run_session()
        self.assertEqual(state["orders"], second["orders"])

    def test_late_start_does_not_chase_signal(self):
        self.feed.current = at(self.day, "09:16:05")
        state = self.run_session()
        self.assertEqual(state["phase"], "closed")
        self.assertEqual(state["orders"], {})

    def test_kill_file_closes_paper_positions(self):
        original_sleep = self.feed.sleep
        def sleep(seconds):
            original_sleep(seconds)
            if self.feed.current >= at(self.day, "09:16:10"):
                (self.root / "STOP").touch()
        self.feed.sleep = sleep
        state = self.run_session()
        self.assertEqual(state["phase"], "closed")
        self.assertTrue(state["halted"])
        self.assertLess(self.feed.current, at(self.day, "09:17"))

    def test_changed_reference_close_blocks_entries(self):
        path = self.input / "references.csv"
        records = data.rows(path)
        records[0]["reference_close"] = "99"
        data.write_csv(path, records, list(records[0]))
        with self.assertRaisesRegex(TradingError, "differs"):
            self.run_session()
        with sqlite3.connect(self.root / "paper.sqlite") as db:
            state = json.loads(db.execute("SELECT document FROM session").fetchone()[0])
        db.close()
        self.assertEqual(state["orders"], {})

    def test_live_runner_uses_real_fill_accounting_and_exits_without_quotes(self):
        class SimulatedLive(ClockFeed, FakeFyers):
            def __init__(self, day):
                ClockFeed.__init__(self, day)
                FakeFyers.__init__(self)

            def account_fingerprint(self):
                return "mock-account-not-real"

            def quotes(self, symbols):
                if self.current >= at(self.current.date(), "09:16:10"):
                    raise TradingError("simulated data outage")
                return ClockFeed.quotes(self, symbols)

            def place(self, symbol, qty, side, tag):
                response = FakeFyers.place(self, symbol, qty, side, tag)
                self.orders[-1]["tradedPrice"] = 97 if "A" in symbol else 103
                return response
        feed = SimulatedLive(self.day)
        config = replace(self.config, live_capital="10000", live_max_order_notional="1000",
                         allow_live=True, live_readiness_ack=True)
        with patch.dict("os.environ", {"FYERS_LIVE_ARM": "I_ACCEPT_LIVE_ORDERS"}):
            state = run_session(config, feed, self.input / "universe.csv", self.input / "sessions.csv",
                                self.input / "references.csv", self.root / "live-mock.sqlite", mode="live",
                                kill_file=self.root / "STOP", now_fn=feed.now, sleep=feed.sleep)
        self.assertEqual(state["phase"], "closed")
        self.assertEqual(len(state["orders"]), 4)
        self.assertEqual({s: q for s, q in feed.net.items() if q}, {})


if __name__ == "__main__":
    unittest.main()
