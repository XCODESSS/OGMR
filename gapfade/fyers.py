"""Small FYERS v3 REST adapter. Never retries an order mutation automatically."""
from __future__ import annotations

from datetime import datetime, timedelta
from getpass import getpass
import hashlib
import json
import os
from pathlib import Path
import secrets
import time
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, urlencode, urlsplit
from urllib.request import Request, urlopen

from .model import IST, Quote, TradingError, number

BASE = "https://api-t1.fyers.in"


def request(path, *, method="GET", payload=None, token=None, client_id=None):
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"{client_id}:{token}"
    body = json.dumps(payload).encode() if payload is not None else None
    req = Request(BASE + path, data=body, headers=headers, method=method)
    try:
        with urlopen(req, timeout=10) as response:
            obj = json.load(response)
    except HTTPError as exc:
        raise TradingError(f"FYERS HTTP {exc.code}; order outcome may be unknown, reconcile before retry") from None
    except (URLError, TimeoutError, OSError, ValueError) as exc:
        raise TradingError("FYERS transport/response failure; do not retry an order without reconciliation") from None
    if not isinstance(obj, dict):
        raise TradingError("Unexpected FYERS response schema")
    if obj.get("s") != "ok" and obj.get("code") != 201:
        raise TradingError(f"FYERS rejected request (code {obj.get('code', 'unknown')}); see FYERS account/API diagnostics")
    return obj


def login(token_file):
    client = os.environ.get("FYERS_CLIENT_ID") or input("FYERS app ID: ").strip()
    redirect = os.environ.get("FYERS_REDIRECT_URI") or input("Registered redirect URL: ").strip()
    secret = os.environ.get("FYERS_SECRET_KEY") or getpass("FYERS app secret (hidden): ")
    state = secrets.token_urlsafe(24)
    auth_url = BASE + "/api/v3/generate-authcode?" + urlencode({
        "client_id": client, "redirect_uri": redirect, "response_type": "code", "state": state})
    print("Open this URL, log in yourself, then paste the complete redirect URL below:\n" + auth_url)
    callback = getpass("Complete redirect URL (hidden): ")
    actual, expected = urlsplit(callback), urlsplit(redirect)
    if (actual.scheme, actual.netloc, actual.path) != (expected.scheme, expected.netloc, expected.path):
        raise TradingError("Redirect URL does not match the registered URL")
    query = parse_qs(actual.query)
    if query.get("state") != [state] or len(query.get("auth_code", [])) != 1:
        raise TradingError("OAuth state mismatch or missing auth code")
    response = request("/api/v3/validate-authcode", method="POST", payload={
        "grant_type": "authorization_code", "appIdHash": hashlib.sha256(f"{client}:{secret}".encode()).hexdigest(),
        "code": query["auth_code"][0]})
    access = response.get("access_token")
    if not isinstance(access, str) or not access:
        raise TradingError("No access token returned")
    path = Path(token_file)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(".tmp")
    temp.write_text(json.dumps({"client_id": client, "access_token": access,
                               "created_at": datetime.now(IST).isoformat()}), encoding="utf-8")
    os.chmod(temp, 0o600)
    temp.replace(path)
    print(f"Token saved locally to {path}; token was not printed.")


class FyersClient:
    def __init__(self, token_file):
        obj = json.loads(Path(token_file).read_text(encoding="utf-8-sig"))
        self.client_id, self._token = obj["client_id"], obj["access_token"]

    def call(self, path, *, method="GET", payload=None):
        return request(path, method=method, payload=payload, token=self._token, client_id=self.client_id)

    def quotes(self, symbols):
        if not 1 <= len(symbols) <= 50:
            raise TradingError("This runner supports 1 to 50 symbols per quote snapshot")
        response = self.call("/data/quotes?" + urlencode({"symbols": ",".join(symbols)}))
        result = {}
        for row in response.get("d", []):
            symbol = row.get("n")
            if row.get("s") != "ok" or symbol not in symbols or symbol in result:
                raise TradingError("FYERS per-symbol quote error, duplicate or unexpected symbol")
            v = row["v"]
            ts = datetime.fromtimestamp(int(v["tt"]), IST)
            result[symbol] = Quote(symbol, ts, number(v["bid"], positive=True),
                                   number(v["ask"], positive=True), number(v["open_price"], positive=True),
                                   number(v["prev_close_price"], positive=True))
        if set(result) != set(symbols):
            raise TradingError("Incomplete quote snapshot; no ranking or entry is allowed")
        return result

    def orderbook(self):
        result = self.call("/api/v3/orders")
        if not isinstance(result.get("orderBook"), list):
            raise TradingError("Missing FYERS orderBook array")
        return result["orderBook"]

    def positions(self):
        result = self.call("/api/v3/positions")
        if not isinstance(result.get("netPositions"), list):
            raise TradingError("Missing FYERS netPositions array")
        return result["netPositions"]

    def account_fingerprint(self):
        obj = self.call("/api/v3/profile").get("data", {})
        identity = obj.get("fy_id")
        if not identity:
            raise TradingError("Cannot establish FYERS account identity from profile fy_id")
        return hashlib.sha256(f"{self.client_id}:{identity}".encode()).hexdigest()

    def place(self, symbol, quantity, side, tag):
        return self.call("/api/v3/orders/sync", method="POST", payload={
            "symbol": symbol, "qty": quantity, "type": 2, "side": side,
            "productType": "INTRADAY", "limitPrice": 0, "stopPrice": 0,
            "validity": "DAY", "disclosedQty": 0, "offlineOrder": False,
            "orderTag": tag, "isSliceOrder": False})

    def cancel(self, order_id):
        return self.call("/api/v3/orders/sync", method="DELETE", payload={"id": order_id})

    def history(self, symbol, start, end, resolution="1"):
        params = {"symbol": symbol, "resolution": resolution, "date_format": "1",
                  "range_from": str(start), "range_to": str(end), "cont_flag": "0"}
        response = self.call("/data/history?" + urlencode(params))
        candles = response.get("candles")
        if not isinstance(candles, list) or not candles:
            raise TradingError(f"No historical candles for {symbol} {start}..{end}")
        return candles


def download(client, instruments, calendar, destination):
    from .data import write_csv
    destination = Path(destination)
    if destination.exists():
        raise TradingError("Download destination must be new")
    if max(calendar) >= datetime.now(IST).date():
        raise TradingError("Download only completed prior sessions")
    records, refs = [], []
    destination.mkdir(parents=True)
    (destination / "INCOMPLETE.txt").write_text("Download has not completed. Do not backtest this directory.\n", encoding="utf-8")
    raw_dir = destination / "raw"
    raw_dir.mkdir()
    start, end = min(calendar.values()), max(calendar)
    for symbol in sorted({i.symbol for i in instruments}):
        daily = {}
        cursor = start
        while cursor <= end:
            last = min(cursor + timedelta(days=89), end)
            daily_raw = client.history(symbol, cursor, last, "D")
            time.sleep(1)
            minute_raw = client.history(symbol, cursor, last, "1")
            raw_name = hashlib.sha256(symbol.encode()).hexdigest()[:12] + f"-{cursor}-{last}.json"
            (raw_dir / raw_name).write_text(json.dumps({"symbol": symbol, "from": str(cursor), "to": str(last),
                "daily_candles": daily_raw, "minute_candles": minute_raw}), encoding="utf-8")
            for row in daily_raw:
                day = datetime.fromtimestamp(int(row[0]), IST).date()
                if day in daily:
                    raise TradingError("Duplicate daily candle returned")
                daily[day] = row
            for row in minute_raw:
                ts = datetime.fromtimestamp(int(row[0]), IST)
                if ts.date() in calendar:
                    records.append(dict(zip(["timestamp", "symbol", "open", "high", "low", "close", "volume"],
                                             [ts.isoformat(), symbol, *row[1:6]])))
            time.sleep(1)
            cursor = last + timedelta(days=1)
        for day, previous in sorted(calendar.items()):
            if not any(i.symbol == symbol and i.active(day) for i in instruments):
                continue
            if day not in daily or previous not in daily:
                raise TradingError(f"Missing daily reference for {symbol} {day}")
            refs.append({"session": day, "symbol": symbol, "previous_session": previous,
                         "reference_close": daily[previous][4], "official_open": daily[day][1],
                         "available_at": f"{day}T09:15:59+05:30", "verified": "false"})
    write_csv(destination / "bars.csv", records,
              ["timestamp", "symbol", "open", "high", "low", "close", "volume"])
    write_csv(destination / "references.csv", refs,
              ["session", "symbol", "previous_session", "reference_close", "official_open", "available_at", "verified"])
    (destination / "provenance.json").write_text(json.dumps({
        "provider": "FYERS v3 history", "downloaded_at": datetime.now(IST).isoformat(),
        "references_verified": False,
        "note": "Audit corporate actions, reference-price adjustment, historical membership and signal availability before setting verified=true."}, indent=2), encoding="utf-8")
    (destination / "INCOMPLETE.txt").unlink()
