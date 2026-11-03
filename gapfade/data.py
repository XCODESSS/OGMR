from __future__ import annotations

import csv
from datetime import date
from pathlib import Path

from .model import Bar, Instrument, Reference, TradingError, number, timestamp


def rows(path):
    with Path(path).open(newline="", encoding="utf-8-sig") as handle:
        return list(csv.DictReader(handle))


def boolean(value):
    if value not in ("true", "false"):
        raise TradingError("CSV booleans must be true or false")
    return value == "true"


def universe(path):
    result = []
    for r in rows(path):
        symbol = r["symbol"]
        if not symbol.startswith("NSE:") or not symbol.endswith("-EQ"):
            raise TradingError(f"Only NSE cash equities are supported: {symbol}")
        item = Instrument(symbol, date.fromisoformat(r["start_date"]),
                          date.fromisoformat(r["end_date"]), boolean(r["long_allowed"]),
                          boolean(r["short_allowed"]))
        if item.start > item.end:
            raise TradingError("Invalid universe date range")
        if any(x.symbol == symbol and max(x.start, item.start) <= min(x.end, item.end)
               for x in result):
            raise TradingError(f"Overlapping membership for {symbol}")
        result.append(item)
    if not result:
        raise TradingError("Universe is empty")
    return result


def sessions(path):
    result = {}
    for r in rows(path):
        day, prev = date.fromisoformat(r["session"]), date.fromisoformat(r["previous_session"])
        if day in result or prev >= day:
            raise TradingError("Duplicate or invalid calendar session")
        result[day] = prev
    if not result:
        raise TradingError("Session calendar is empty")
    ordered = sorted(result)
    for before, after in zip(ordered, ordered[1:]):
        if result[after] != before:
            raise TradingError(f"Calendar gap before {after}; include every exchange session")
    return result


def references(path):
    result = {}
    for r in rows(path):
        item = Reference(date.fromisoformat(r["session"]), r["symbol"],
                         date.fromisoformat(r["previous_session"]),
                         number(r["reference_close"], positive=True),
                         number(r["official_open"], positive=True), timestamp(r["available_at"]),
                         boolean(r["verified"]))
        key = item.session, item.symbol
        if key in result:
            raise TradingError(f"Duplicate reference {key}")
        result[key] = item
    return result


def bars(path):
    result = {}
    for r in rows(path):
        item = Bar(timestamp(r["timestamp"]), r["symbol"],
                   *(number(r[k], positive=True) for k in ("open", "high", "low", "close")),
                   number(r["volume"]))
        if item.timestamp.second or item.timestamp.microsecond:
            raise TradingError("Only minute-start timestamps are supported")
        if item.low > min(item.open, item.close) or item.high < max(item.open, item.close) or item.low > item.high or item.volume < 0:
            raise TradingError(f"Invalid OHLCV at {item.timestamp}: {item.symbol}")
        key = item.timestamp, item.symbol
        if key in result:
            raise TradingError(f"Duplicate bar {key}")
        result[key] = item
    return result


def write_csv(path, records, columns):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=columns)
        writer.writeheader()
        writer.writerows(records)
