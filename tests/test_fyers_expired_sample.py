from datetime import date, datetime
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from scripts.fetch_fyers_expired import IST, PREFIX, candle_rows, fetch, select_pair


class ExpiredSampleTests(unittest.TestCase):
    def test_contract_selection_uses_returned_symbols_and_lower_tie(self):
        symbols = [f"NSE:NIFTY26NOV{strike}{side}" for strike in (23900, 23950) for side in ("CE", "PE")]
        strike, pair = select_pair(symbols, 23925)
        self.assertEqual(strike, 23900)
        self.assertEqual(set(pair.values()), set(symbols[:2]))

    def test_empty_and_duplicate_candles_fail(self):
        for response in ({"candles": []}, {"candles": [[1, 2, 3, 1, 2, 1]] * 2}):
            with self.assertRaises(RuntimeError):
                candle_rows(response)

    def test_fetch_calls_expired_endpoint_and_preserves_evidence(self):
        timestamp = int(datetime(2026, 11, 2, 9, 19, tzinfo=IST).timestamp())
        candles = {"s": "ok", "candles": [[timestamp, 23900, 23910, 23890, 23900, 100]]}
        symbols = ["NSE:NIFTY26N0323900CE", "NSE:NIFTY26N0323900PE"]
        replies = [
            {"s": "ok", "data": {"expiry_dates": {"options": ["2026-10-27", "2026-11-03"]}}},
            {"s": "ok", "data": {"contracts": {"options": symbols}}},
            candles, candles, candles,
        ]
        calls = []

        def fake_get(client, path, params):
            client.calls += 1
            calls.append((path, params))
            return replies.pop(0)

        with tempfile.TemporaryDirectory() as folder:
            token = Path(folder) / "token.json"
            token.write_text(json.dumps({"client_id": "test", "access_token": "private-test-token"}))
            output = Path(folder) / "download"
            with patch("scripts.fetch_fyers_expired.DataClient.get", fake_get):
                result = fetch(token, output, today=date(2026, 11, 5))
            self.assertEqual(result["requests_made"], 5)
            self.assertEqual(result["expiry"], "2026-11-03")
            self.assertEqual(result["session"], "2026-11-02")
            self.assertFalse(result["backtest_ready"])
            self.assertFalse((output / "INCOMPLETE.txt").exists())
            for side in ("ce", "pe"):
                self.assertTrue((output / f"{side}.csv").exists())
            self.assertEqual([p for p, _ in calls], [PREFIX + "expiry-dates", PREFIX + "underlying-symbols",
                                                   "/data/history", PREFIX + "historical-data",
                                                   PREFIX + "historical-data"])
            for file in output.iterdir():
                self.assertNotIn("private-test-token", file.read_text())

    def test_rejected_request_keeps_incomplete_marker(self):
        with tempfile.TemporaryDirectory() as folder:
            token = Path(folder) / "token.json"
            token.write_text(json.dumps({"client_id": "test", "access_token": "private-test-token"}))
            output = Path(folder) / "download"
            with patch("scripts.fetch_fyers_expired.DataClient.get", side_effect=RuntimeError("Rejected")):
                with self.assertRaisesRegex(RuntimeError, "Rejected"):
                    fetch(token, output, today=date(2026, 11, 5))
            self.assertTrue((output / "INCOMPLETE.txt").exists())
            self.assertFalse((output / "manifest.json").exists())


if __name__ == "__main__":
    unittest.main()
