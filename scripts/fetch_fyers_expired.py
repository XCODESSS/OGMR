"""Download a bounded, read-only FYERS expired NIFTY CE/PE sample.

Run from the repository root. Credentials use the same JSON format as
`python -m gapfade login`. Output is raw research data, not audited backtest input.
"""
from __future__ import annotations

import argparse
import csv
from datetime import date, datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path
import time
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen


BASE = "https://api-t1.fyers.in"
PREFIX = "/data/history/fno/expired/"
UNDERLYING = "NSE:NIFTY50-INDEX"
IST = timezone(timedelta(hours=5, minutes=30))


class DataClient:
    def __init__(self, token_file):
        credentials = json.loads(Path(token_file).read_text(encoding="utf-8-sig"))
        self.authorization = f"{credentials['client_id']}:{credentials['access_token']}"
        self.calls = 0

    def get(self, path, params):
        if self.calls >= 5:
            raise RuntimeError("Sample request budget exhausted (5 total GETs).")
        if self.calls:
            time.sleep(1.3)
        self.calls += 1
        request = Request(BASE + path + "?" + urlencode(params),
                          headers={"Authorization": self.authorization,
                                   "Content-Type": "application/json",
                                   "User-Agent": "fyers-data-research/1.0"})
        try:
            with urlopen(request, timeout=30) as response:
                result = json.load(response)
        except HTTPError as exc:
            # Do not print the request, credentials, response headers or body.
            raise RuntimeError(f"FYERS HTTP {exc.code}; stopped without retry.") from None
        except (URLError, TimeoutError, OSError, ValueError):
            raise RuntimeError("FYERS transport or JSON failure; stopped without retry.") from None
        if not isinstance(result, dict):
            raise RuntimeError("Unexpected FYERS response schema.")
        if result.get("s") not in ("ok", "no_data"):
            code = result.get("code", "unknown")
            raise RuntimeError(f"FYERS rejected data request (code {code}); stopped without retry.")
        return result


def save_json(path, value):
    path.write_text(json.dumps(value, indent=2), encoding="utf-8")


def candle_rows(response):
    columns = response.get("columns") or ["timestamp", "open", "high", "low", "close", "volume"]
    rows = response.get("candles")
    if not isinstance(rows, list) or not rows:
        raise RuntimeError("FYERS returned no candles.")
    if columns[:6] != ["timestamp", "open", "high", "low", "close", "volume"]:
        raise RuntimeError("Unexpected core candle columns.")
    seen, normalized = set(), []
    for row in rows:
        if len(row) != len(columns):
            raise RuntimeError("Candle width does not match columns.")
        timestamp = int(row[0])
        if timestamp in seen:
            raise RuntimeError("Duplicate candle timestamps.")
        seen.add(timestamp)
        normalized.append(dict(zip(columns, row)))
    return sorted(normalized, key=lambda row: row["timestamp"])


def write_candles(path, rows):
    with path.open("w", newline="", encoding="utf-8") as stream:
        writer = csv.DictWriter(stream, fieldnames=["timestamp_ist", *rows[0]])
        writer.writeheader()
        for row in rows:
            writer.writerow({"timestamp_ist": datetime.fromtimestamp(row["timestamp"], IST).isoformat(), **row})


def select_pair(symbols, spot):
    # NIFTY strikes use a 50-point grid. Match whole returned suffixes and
    # require a unique symbol per side rather than inventing expired symbols.
    candidates = []
    center = round(float(spot) / 50) * 50
    for strike in range(center - 1000, center + 1001, 50):
        pair = {}
        for side in ("CE", "PE"):
            matches = [symbol for symbol in symbols if symbol.endswith(f"{strike}{side}")]
            if len(matches) == 1:
                pair[side] = matches[0]
        if len(pair) == 2:
            candidates.append((abs(strike - float(spot)), strike, pair))
    if not candidates:
        raise RuntimeError("No unique CE/PE pair found near the sample spot price.")
    _, strike, pair = min(candidates, key=lambda item: (item[0], item[1]))
    return strike, pair


def fetch(token_file, destination, today=None):
    today = today or datetime.now(IST).date()
    out = Path(destination)
    out.mkdir(parents=True, exist_ok=False)
    marker = out / "INCOMPLETE.txt"
    marker.write_text("Incomplete download; do not backtest.\n", encoding="utf-8")
    client = DataClient(token_file)
    discovery = client.get(PREFIX + "expiry-dates", {
        "symbol": UNDERLYING, "date_format": 1,
        "range_from": str(today - timedelta(days=30)), "range_to": str(today - timedelta(days=1))})
    save_json(out / "expiry_dates.json", discovery)
    expiries = discovery["data"]["expiry_dates"]["options"]
    expiry = max(date.fromisoformat(value) for value in expiries if date.fromisoformat(value) < today)
    catalog = client.get(PREFIX + "underlying-symbols", {"symbol": UNDERLYING, "expiry_date": str(expiry)})
    save_json(out / "contracts.json", catalog)
    symbols = catalog["data"]["contracts"]["options"]
    if not isinstance(symbols, list) or not all(isinstance(s, str) for s in symbols):
        raise RuntimeError("Unexpected expired contract catalog.")
    # Fetch the preceding week to discover the last actual session before expiry.
    spot_response = client.get("/data/history", {
        "symbol": UNDERLYING, "resolution": "1", "date_format": 1,
        "range_from": str(expiry - timedelta(days=7)),
        "range_to": str(expiry - timedelta(days=1)), "cont_flag": "0"})
    save_json(out / "spot_raw.json", spot_response)
    spot_rows = candle_rows(spot_response)
    write_candles(out / "spot.csv", spot_rows)
    reference = max((row for row in spot_rows
                     if datetime.fromtimestamp(row["timestamp"], IST).strftime("%H:%M") == "09:19"),
                    key=lambda row: row["timestamp"])
    session = datetime.fromtimestamp(reference["timestamp"], IST).date()
    strike, pair = select_pair(symbols, reference["close"])
    summaries = {}
    for side, symbol in pair.items():
        response = client.get(PREFIX + "historical-data", {
            "symbol": symbol, "resolution": "1", "date_format": 1,
            "range_from": str(session), "range_to": str(session)})
        save_json(out / f"{side.lower()}_raw.json", response)
        rows = candle_rows(response)
        if any(datetime.fromtimestamp(row["timestamp"], IST).date() != session for row in rows):
            raise RuntimeError("Option candles outside the requested session.")
        write_candles(out / f"{side.lower()}.csv", rows)
        summaries[side] = {"symbol": symbol, "candles": len(rows),
                           "first_timestamp": rows[0]["timestamp"], "last_timestamp": rows[-1]["timestamp"]}
    manifest = {"provider": "FYERS expired F&O API", "downloaded_at": datetime.now(IST).isoformat(),
                "expiry": str(expiry), "session": str(session), "strike": strike,
                "selection": "Nearest available CE/PE pair to 09:19 spot close; access sample only",
                "requests_made": client.calls, "options": summaries,
                "backtest_ready": False,
                "note": "Raw access sample. Calendar, historical lot sizes, data coverage and execution assumptions remain unaudited.",
                "sha256": {p.name: hashlib.sha256(p.read_bytes()).hexdigest()
                           for p in sorted(out.iterdir()) if p.is_file() and p != marker}}
    save_json(out / "manifest.json", manifest)
    marker.unlink()
    return manifest


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--token-file", default=".secrets/fyers.json")
    parser.add_argument("--out", required=True, help="New output directory")
    args = parser.parse_args()
    try:
        result = fetch(args.token_file, args.out)
    except (RuntimeError, ValueError, KeyError, TypeError, OSError) as exc:
        # Schema and credential file errors must not expose credential contents.
        print(str(exc) if isinstance(exc, RuntimeError) else f"Local/schema error: {type(exc).__name__}")
        return 1
    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
