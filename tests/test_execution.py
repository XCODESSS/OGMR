from dataclasses import replace
from datetime import date, timedelta
from decimal import Decimal as D
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from gapfade.config import Config
from gapfade.execution import Journal, LiveBroker, PaperBroker, flatten, single_runner
from gapfade.model import Quote, TradingError, at
from gapfade.runner import live_enabled, validate_quotes


class FakeFyers:
    def __init__(self):
        self.orders = []
        self.net = {}
        self.calls = 0
        self.timeout = False

    def orderbook(self):
        return self.orders

    def positions(self):
        return [{"symbol": s, "productType": "INTRADAY", "netQty": q} for s, q in self.net.items() if q]

    def place(self, symbol, qty, side, tag):
        self.calls += 1
        if self.timeout:
            raise TradingError("transport timeout")
        self.orders.append({"orderTag": "1:" + tag, "symbol": symbol, "productType": "INTRADAY",
                            "qty": qty, "side": side, "status": 2, "filledQty": qty,
                            "tradedPrice": "100", "id": str(self.calls)})
        self.net[symbol] = self.net.get(symbol, 0) + qty * side
        return {"s": "ok", "id": str(self.calls)}

    def cancel(self, order_id):
        for row in self.orders:
            if row["id"] == order_id:
                row["status"] = 1


class ExecutionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "journal.sqlite"
        self.identity = {"day": "2026-01-05", "mode": "live"}
        self.journal = Journal(self.path, self.identity, "10000")
        self.client = FakeFyers()
        self.config = Config(live_capital="10000", live_max_order_notional="1000")
        self.broker = LiveBroker(self.client, self.journal, self.config, sleep=lambda _: None)
        self.quote = Quote("NSE:A-EQ", at(date(2026, 1, 5), "09:16"), D("99"), D("100"), D("97"), D("100"))

    def tearDown(self):
        self.journal.db.close()
        self.tmp.cleanup()

    def test_live_arm_is_required(self):
        with patch.dict(os.environ, {}, clear=True):
            with self.assertRaises(TradingError):
                live_enabled(Config())
        with patch.dict(os.environ, {"FYERS_LIVE_ARM": "I_ACCEPT_LIVE_ORDERS"}):
            live_enabled(replace(self.config, allow_live=True, live_readiness_ack=True))

    def test_filled_entry_exit_reconciles_positions(self):
        self.broker.submit("NSE:A-EQ", 5, 1, "entry", self.quote)
        self.assertEqual(self.journal.net_positions(), {"NSE:A-EQ": 5})
        flatten(self.broker, self.journal)
        self.assertEqual(self.client.calls, 2)
        self.assertEqual(self.journal.net_positions(), {})
        self.assertEqual(self.journal.state["phase"], "closed")
        self.assertLess(D(self.journal.state["equity"]), D("10000"))  # Charges on both legs.

    def test_timeout_is_persisted_and_not_blindly_retried_after_restart(self):
        self.client.timeout = True
        with self.assertRaises(TradingError):
            self.broker.submit("NSE:A-EQ", 5, 1, "entry", self.quote)
        self.assertEqual(self.client.calls, 1)
        self.journal.db.close()
        self.journal = Journal(self.path, self.identity, "10000")
        self.broker = LiveBroker(self.client, self.journal, self.config)
        with self.assertRaisesRegex(TradingError, "absent or ambiguous"):
            self.broker.submit("NSE:A-EQ", 5, 1, "entry", self.quote)
        self.assertEqual(self.client.calls, 1)

    def test_partial_cancelled_fill_is_owned_and_closed_only_to_its_size(self):
        tag = self.journal.order_tag("NSE:A-EQ", "entry")
        self.journal.state["orders"][tag] = {"symbol": "NSE:A-EQ", "qty": 10, "side": -1,
                                            "role": "entry", "state": "unknown", "filled": 0, "price": "0"}
        self.journal.save()
        self.client.orders = [{"orderTag": "1:" + tag, "symbol": "NSE:A-EQ", "productType": "INTRADAY",
                               "qty": 10, "side": -1, "filledQty": 4, "tradedPrice": 100, "status": 1, "id": "partial"}]
        self.client.net = {"NSE:A-EQ": -4}
        self.broker.reconcile()
        self.assertEqual(self.journal.net_positions(), {"NSE:A-EQ": -4})
        flatten(self.broker, self.journal)
        self.assertEqual(self.client.orders[-1]["qty"], 4)

    def test_unowned_positions_block_orders_and_exit_cannot_reverse(self):
        self.client.net["NSE:OTHER-EQ"] = 1
        with self.assertRaisesRegex(TradingError, "differ"):
            self.broker.submit("NSE:A-EQ", 5, 1, "entry", self.quote)
        self.assertEqual(self.client.calls, 0)
        self.client.net.clear()
        with self.assertRaisesRegex(TradingError, "increase or reverse"):
            self.broker.submit("NSE:A-EQ", 1, -1, "exit0")

    def test_cap_stale_quotes_and_single_process_lock(self):
        with self.assertRaisesRegex(TradingError, "cap"):
            self.broker.submit("NSE:A-EQ", 11, 1, "entry", self.quote)
        with self.assertRaisesRegex(TradingError, "Stale"):
            validate_quotes({self.quote.symbol: self.quote}, self.quote.timestamp + timedelta(seconds=20), Config())
        with single_runner(self.path):
            with self.assertRaisesRegex(TradingError, "lock exists"):
                with single_runner(self.path):
                    pass

    def test_changed_identity_cannot_reuse_journal(self):
        with self.assertRaisesRegex(TradingError, "identity|mode/account"):
            Journal(self.path, {**self.identity, "account": "different"}, "10000")


if __name__ == "__main__":
    unittest.main()
