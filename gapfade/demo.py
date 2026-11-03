"""Generate obviously synthetic inputs to exercise plumbing, never performance evidence."""
from datetime import date, timedelta
from pathlib import Path

from .data import write_csv
from .model import TradingError, at


def create(path):
    path = Path(path)
    if path.exists():
        raise TradingError("Demo directory must be new")
    path.mkdir(parents=True)
    days = [date(2026, 1, 5), date(2026, 1, 6)]
    symbols = ["NSE:DEMOA-EQ", "NSE:DEMOB-EQ"]
    write_csv(path / "universe.csv", [dict(symbol=s, start_date="2026-01-01", end_date="2026-01-31",
        long_allowed="true", short_allowed="true") for s in symbols],
        ["symbol", "start_date", "end_date", "long_allowed", "short_allowed"])
    write_csv(path / "sessions.csv", [{"session": str(days[0]), "previous_session": "2026-01-02"},
                                       {"session": str(days[1]), "previous_session": str(days[0])}],
              ["session", "previous_session"])
    refs, bars = [], []
    for day in days:
        for i, symbol in enumerate(symbols):
            opening = 97 if i == 0 else 103
            refs.append(dict(session=str(day), symbol=symbol,
                             previous_session="2026-01-02" if day == days[0] else str(days[0]),
                             reference_close="100", official_open=str(opening),
                             available_at=f"{day}T09:15:30+05:30", verified="true"))
            t = at(day, "09:16")
            j = 0
            while t <= at(day, "15:00"):
                movement = j / 344 * (2 if i == 0 else -2)
                if day == days[1]:
                    movement = -movement
                price = opening + movement
                bars.append(dict(timestamp=t.isoformat(), symbol=symbol, open=f"{price:.4f}",
                                 high=f"{price + 0.1:.4f}", low=f"{price - 0.1:.4f}",
                                 close=f"{price:.4f}", volume="10000"))
                t += timedelta(minutes=1)
                j += 1
    write_csv(path / "references.csv", refs,
              ["session", "symbol", "previous_session", "reference_close", "official_open", "available_at", "verified"])
    write_csv(path / "bars.csv", bars, ["timestamp", "symbol", "open", "high", "low", "close", "volume"])
    (path / "SYNTHETIC.txt").write_text("Fictitious instruments and prices. Plumbing demonstration only.\n", encoding="utf-8")
