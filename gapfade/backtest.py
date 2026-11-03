from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import timedelta
import hashlib
import json
import math
from pathlib import Path
from statistics import mean, stdev

from . import data
from .config import Config
from .model import TradingError, ZERO, at, fill_price, number, size_position
from .strategy import select


@dataclass
class Trade:
    session: str
    symbol: str
    side: int
    quantity: int
    gap: str
    entry_time: str
    exit_time: str
    entry_price: str
    exit_price: str
    gross_pnl: str
    fees: str
    net_pnl: str
    exit_reason: str


def run(config: Config, universe_path, calendar_path, reference_path, bar_path, output):
    output = Path(output)
    if output.exists():
        raise TradingError("Use a new output directory; existing experiments are never overwritten")
    if (Path(bar_path).parent / "INCOMPLETE.txt").exists():
        raise TradingError("Historical download is incomplete")
    instruments, calendar = data.universe(universe_path), data.sessions(calendar_path)
    refs, bars = data.references(reference_path), data.bars(bar_path)
    equity = number(config.capital)
    fee_rate = number(config.fee_bps_per_leg) / 10000
    slip = number(config.slippage_bps_per_leg)
    trades, days = [], []
    peak, max_drawdown = equity, ZERO
    def mark(value):
        nonlocal peak, max_drawdown
        peak = max(peak, value)
        max_drawdown = min(max_drawdown, value / peak - 1)

    for day, previous in sorted(calendar.items()):
        start_equity = equity
        candidates = select(day, previous, instruments, refs, config)
        entry, exit_ = at(day, config.entry_time), at(day, config.exit_time)
        # Validate all candidate paths before simulating, including skipped zero-size names.
        for signal in candidates:
            t = entry
            while t <= exit_:
                if (t, signal.symbol) not in bars or bars[t, signal.symbol].volume <= 0:
                    raise TradingError(f"Missing/untradable required bar {t} {signal.symbol}")
                t += timedelta(minutes=1)
        positions, reserved = [], ZERO
        for signal in candidates:
            price = fill_price(bars[entry, signal.symbol].open, signal.side, slip)
            quantity = size_position(start_equity, price, number(config.position_weight),
                                     number(config.fee_bps_per_leg))
            if quantity == 0:
                continue
            reserve = quantity * price * (1 + 2 * fee_rate)
            if reserved + reserve > start_equity:
                raise TradingError("Concurrent gross exposure exceeds available simulated capital")
            reserved += reserve
            positions.append((signal, quantity, price))
        reason = "scheduled_exit"
        actual_exit = exit_
        t = entry
        while positions and t < exit_:
            # Completed minute close is observed at t+1; flatten at that next open.
            liquidation_value = start_equity + sum(
                qty * sig.side * (bars[t, sig.symbol].close - price)
                - qty * (price + bars[t, sig.symbol].close) * fee_rate
                for sig, qty, price in positions)
            mark(liquidation_value)
            if liquidation_value - start_equity <= -start_equity * number(config.max_daily_loss_fraction):
                actual_exit, reason = t + timedelta(minutes=1), "daily_loss_limit"
                break
            t += timedelta(minutes=1)
        for signal, quantity, entry_price in positions:
            exit_price = fill_price(bars[actual_exit, signal.symbol].open, -signal.side, slip)
            gross = quantity * signal.side * (exit_price - entry_price)
            fees = quantity * (entry_price + exit_price) * fee_rate
            net = gross - fees
            equity += net
            trades.append(Trade(str(day), signal.symbol, signal.side, quantity, str(signal.gap),
                                entry.isoformat(), actual_exit.isoformat(), str(entry_price),
                                str(exit_price), str(gross), str(fees), str(net), reason))
        mark(equity)
        days.append({"session": str(day), "start_equity": str(start_equity),
                     "end_equity": str(equity), "return": str(equity / start_equity - 1),
                     "selected": len(candidates), "filled": len(positions), "exit_reason": reason})
        if equity <= 0:
            raise TradingError("Simulated capital exhausted")
    pnl = [number(t.net_pnl) for t in trades]
    wins, losses = sum((x for x in pnl if x > 0), ZERO), -sum((x for x in pnl if x < 0), ZERO)
    returns = [float(x["return"]) for x in days]
    annual = {}
    for d in days:
        year = d["session"][:4]
        annual[year] = annual.get(year, 1.0) * (1 + float(d["return"]))
    summary = {
        "strategy": "gap-fade-0.1-executable-bars", "data_kind": "user_supplied_not_independently_verified",
        "initial_equity": config.capital, "final_equity": str(equity),
        "net_profit": str(equity - number(config.capital)),
        "total_return": float(equity / number(config.capital) - 1),
        "trade_count": len(trades), "session_count": len(days),
        "net_expectancy_per_trade": str(sum(pnl, ZERO) / len(pnl)) if pnl else None,
        "profit_factor": float(wins / losses) if losses else None,
        "win_rate": sum(x > 0 for x in pnl) / len(pnl) if pnl else None,
        "max_drawdown_marked_and_daily": float(max_drawdown),
        "sharpe_daily_annualized_rf_zero": mean(returns) / stdev(returns) * math.sqrt(252)
            if len(returns) > 1 and stdev(returns) > 0 else None,
        "annual_returns_partial_years_possible": {k: v - 1 for k, v in annual.items()},
        "turnover": str(sum((t.quantity * (number(t.entry_price) + number(t.exit_price)) for t in trades), ZERO)),
        "limitations": ["Minute OHLCV does not establish spread, available size, short margin or attainable fills.",
                        "Missing required bars abort the experiment rather than remove a trade.",
                        "Daily loss control is observed at minute closes, executed next minute; losses can exceed the limit.",
                        "Configured fee assumption is not an independently verified historical tax schedule.",
                        "No historical market dataset was bundled. Demo results are synthetic."]}
    output.mkdir(parents=True)
    data.write_csv(output / "trades.csv", [asdict(t) for t in trades], list(Trade.__dataclass_fields__))
    data.write_csv(output / "daily_equity.csv", days,
                   ["session", "start_equity", "end_equity", "return", "selected", "filled", "exit_reason"])
    (output / "summary.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
    manifest = {"config": config.snapshot(), "input_sha256": {
        str(p): hashlib.sha256(Path(p).read_bytes()).hexdigest()
        for p in (universe_path, calendar_path, reference_path, bar_path)}}
    (output / "manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    return summary
