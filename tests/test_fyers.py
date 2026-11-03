from datetime import datetime
from io import BytesIO
from pathlib import Path
import json
import tempfile
import unittest
from unittest.mock import patch
from urllib.error import URLError

from gapfade.fyers import FyersClient, request
from gapfade.model import TradingError


class FyersContractTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "token.json"
        self.path.write_text(json.dumps({"client_id": "test-app", "access_token": "test-only-token"}))
        self.client = FyersClient(self.path)

    def tearDown(self):
        self.tmp.cleanup()

    def test_quotes_decode_and_individual_symbol_errors(self):
        response = {"s": "ok", "d": [{"n": "NSE:A-EQ", "s": "ok", "v": {
            "tt": 1767585000, "bid": 99, "ask": 100, "open_price": 97, "prev_close_price": 100}}]}
        with patch.object(self.client, "call", return_value=response) as call:
            result = self.client.quotes(["NSE:A-EQ"])
            self.assertEqual(str(result["NSE:A-EQ"].open), "97")
            self.assertIn("NSE%3AA-EQ", call.call_args.args[0])
        response["d"][0]["s"] = "error"
        with patch.object(self.client, "call", return_value=response), self.assertRaises(TradingError):
            self.client.quotes(["NSE:A-EQ"])

    def test_order_payload_and_no_hidden_account_wide_exit(self):
        with patch.object(self.client, "call", return_value={"s": "ok", "id": "123"}) as call:
            self.client.place("NSE:A-EQ", 2, -1, "GFtest")
            args = call.call_args.kwargs
            self.assertEqual(args["method"], "POST")
            self.assertEqual(args["payload"]["type"], 2)
            self.assertEqual(args["payload"]["productType"], "INTRADAY")
            self.assertFalse(args["payload"]["offlineOrder"])

    def test_transport_failure_does_not_retry_or_disclose_token(self):
        with patch("gapfade.fyers.urlopen", side_effect=URLError("sensitive upstream text")) as call:
            with self.assertRaises(TradingError) as failure:
                request("/api/v3/orders/sync", method="POST", payload={}, token="secret", client_id="app")
            self.assertEqual(call.call_count, 1)
            self.assertNotIn("secret", str(failure.exception))
            self.assertNotIn("sensitive", str(failure.exception))

    def test_unacknowledged_order_response_is_preserved_for_reconciliation(self):
        with patch("gapfade.fyers.urlopen", return_value=BytesIO(b'{"s":"error","code":201}')):
            self.assertEqual(request("/api/v3/orders/sync", method="POST", payload={})["code"], 201)


if __name__ == "__main__":
    unittest.main()
