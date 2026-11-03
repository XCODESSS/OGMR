from __future__ import annotations

from datetime import datetime, timedelta
import hashlib
import json
import os
from pathlib import Path
import time

from . import data
from .execution import Journal, LiveBroker, PaperBroker, flatten, single_runner
from .model import IST, Reference, TradingError, at, number, size_position
from .strategy import select


def validate_quotes(quotes, now, config):
    for q in quotes.values():
        age = (now - q.timestamp).total_seconds()
        if q.timestamp.date() != now.date() or not -1 <= age <= config.max_quote_age_seconds:
            raise TradingError(f"Stale or future quote: {q.symbol}")
        if q.bid <= 0 or q.ask <= 0 or q.bid > q.ask:
            raise TradingError(f"Invalid bid/ask: {q.symbol}")


def live_enabled(config):
    if not (config.allow_live and config.live_readiness_ack
            and os.environ.get("FYERS_LIVE_ARM") == "I_ACCEPT_LIVE_ORDERS"):
        raise TradingError("Live mode requires allow_live, live_readiness_ack and FYERS_LIVE_ARM=I_ACCEPT_LIVE_ORDERS")
    if number(config.live_capital) <= 0 or number(config.live_max_order_notional) <= 0:
        raise TradingError("Set explicit positive live_capital and live_max_order_notional")


def run_session(config, client, universe_path, calendar_path, baseline_path, database,
                *, mode="paper", kill_file="STOP_TRADING", now_fn=lambda: datetime.now(IST),
                sleep=time.sleep):
    if mode not in ("paper", "live"):
        raise TradingError("Unsupported mode")
    if mode == "live":
        live_enabled(config)  # Before any broker interaction.
    day = now_fn().date()
    instruments, calendar = data.universe(universe_path), data.sessions(calendar_path)
    if day not in calendar:
        raise TradingError("Today is absent from the explicit exchange-session calendar")
    active = sorted(i.symbol for i in instruments if i.active(day))
    if not 1 <= len(active) <= 50:
        raise TradingError("Use an active universe of 1 to 50 NSE cash equities")
    baselines = {}
    for r in data.rows(baseline_path):
        if r["session"] != str(day):
            continue
        if r["symbol"] in baselines:
            raise TradingError("Duplicate session baseline")
        baselines[r["symbol"]] = r
    for symbol in active:
        r = baselines.get(symbol)
        if not r or not data.boolean(r["verified"]) or r["previous_session"] != str(calendar[day]):
            raise TradingError(f"Missing/unverified/wrong-session baseline: {symbol}")
        number(r["reference_close"], positive=True)
    identity = {"day": str(day), "mode": mode, "config": config.snapshot(),
                "files": {str(p): hashlib.sha256(Path(p).read_bytes()).hexdigest()
                          for p in (universe_path, calendar_path, baseline_path)},
                "account": client.account_fingerprint() if mode == "live" else "paper-simulation"}
    entry, exit_ = at(day, config.entry_time), at(day, config.exit_time)
    capital = config.live_capital if mode == "live" else config.capital
    with single_runner(database):
        journal = Journal(database, identity, capital)
        try:
            broker = LiveBroker(client, journal, config) if mode == "live" else PaperBroker(journal, config)
            broker.reconcile()
            if journal.state["phase"] == "closed":
                print("This session is already closed; no orders sent.")
                return journal.state
            if journal.state["phase"] == "entering":
                # On restart preserve fills but never reconstruct an incomplete entry batch.
                journal.state["phase"] = "entered"
                journal.save()
                journal.event("Restart during entry: remaining new entries skipped")
            journal.event(f"Started {mode} session; allocated capital={journal.state['start_equity']}")
            while True:
                now = now_fn()
                if now.date() != day:
                    raise TradingError("Session crossed midnight without confirmed closure; inspect broker positions")
                stop = Path(kill_file).exists() or journal.state.get("halted", False)
                if stop or now >= exit_:
                    journal.state["halted"] = True
                    journal.save()
                    quotes = None
                    if mode == "paper" and journal.net_positions():
                        quotes = client.quotes(list(journal.net_positions()))
                        validate_quotes(quotes, now_fn(), config)
                    flatten(broker, journal, quotes)
                    return journal.state
                phase = journal.state["phase"]
                if phase == "waiting" and now >= entry:
                    journal.state["phase"] = "closed"
                    journal.save()
                    journal.event("Missed pre-entry signal capture; no late entries allowed")
                    return journal.state
                if phase == "waiting" and now >= entry - timedelta(seconds=30):
                    quotes = client.quotes(active)
                    observed = now_fn()
                    validate_quotes(quotes, observed, config)
                    if observed >= entry:
                        raise TradingError("Quote snapshot finished after the entry cutoff")
                    refs = {}
                    for symbol in active:
                        q = quotes[symbol]
                        close = number(baselines[symbol]["reference_close"], positive=True)
                        if q.previous_close != close:
                            raise TradingError(f"FYERS previous close differs from verified baseline: {symbol}; check corporate actions")
                        refs[day, symbol] = Reference(day, symbol, calendar[day], close, q.open, observed, True)
                    signals = select(day, calendar[day], instruments, refs, config)
                    journal.state["signals"] = [{"symbol": s.symbol, "side": s.side, "gap": str(s.gap)} for s in signals]
                    journal.state["signal_snapshot"] = {
                        "observed_at": observed.isoformat(),
                        "references": {symbol: {"reference_close": str(ref.reference_close), "official_open": str(ref.official_open)}
                                       for (_, symbol), ref in refs.items()}}
                    journal.state["phase"] = "ready"
                    journal.save()
                    journal.event(f"Frozen {len(signals)} ranked gap signals before entry")
                elif phase == "ready" and now >= entry:
                    journal.state["phase"] = "entering"
                    journal.save()
                    reserved = number("0")
                    for signal in journal.state["signals"]:
                        if Path(kill_file).exists():
                            journal.state["halted"] = True
                            journal.save()
                            break
                        if now_fn() > entry + timedelta(seconds=config.entry_window_seconds):
                            journal.event("Entry window elapsed; unsubmitted signals skipped")
                            break
                        symbol, side = signal["symbol"], signal["side"]
                        quotes = client.quotes([symbol])
                        validate_quotes(quotes, now_fn(), config)
                        quote = quotes[symbol]
                        if now_fn() > entry + timedelta(seconds=config.entry_window_seconds):
                            break
                        spread = (quote.ask - quote.bid) / ((quote.ask + quote.bid) / 2) * 10000
                        if spread > number(config.max_spread_bps):
                            journal.event(f"Skipped {symbol}: spread exceeds configured entry limit")
                            continue
                        price = max(quote.ask, quote.bid) * (1 + number(config.slippage_bps_per_leg) / 10000)
                        cap = number(config.live_max_order_notional) if mode == "live" else None
                        qty = size_position(number(journal.state["start_equity"]), price,
                                            number(config.position_weight), number(config.fee_bps_per_leg), cap)
                        if qty == 0:
                            journal.event(f"Skipped {symbol}: whole-share allocation is zero")
                            continue
                        reserve = qty * price * (1 + 2 * number(config.fee_bps_per_leg) / 10000)
                        if reserved + reserve > number(journal.state["start_equity"]):
                            raise TradingError("Entry batch exceeds allocated gross capital")
                        broker.submit(symbol, qty, side, "entry", quote)
                        reserved += reserve
                        held = journal.net_positions()
                        if held:
                            current_quotes = client.quotes(list(held))
                            validate_quotes(current_quotes, now_fn(), config)
                            if journal.pnl(current_quotes, config.fee_bps_per_leg) <= -number(journal.state["start_equity"]) * number(config.max_daily_loss_fraction):
                                journal.state["halted"] = True
                                journal.save()
                                break
                    journal.state["phase"] = "entered"
                    journal.save()
                elif phase == "entered":
                    broker.reconcile()
                    if not journal.net_positions():
                        flatten(broker, journal)
                        return journal.state
                    try:
                        quotes = client.quotes(list(journal.net_positions()))
                        validate_quotes(quotes, now_fn(), config)
                    except TradingError:
                        journal.event("Quote unavailable/stale: no synthetic fill; monitoring will retry, scheduled live exits do not need quotes")
                        sleep(config.poll_seconds)
                        continue
                    pnl = journal.pnl(quotes, config.fee_bps_per_leg)
                    if pnl <= -number(journal.state["start_equity"]) * number(config.max_daily_loss_fraction):
                        journal.state["halted"] = True
                        journal.save()
                        journal.event("Daily loss threshold reached; flattening bot positions")
                        flatten(broker, journal, quotes)
                        return journal.state
                sleep(config.poll_seconds)
        except BaseException:
            journal.event("RUNNER STOPPED: check positions and pending orders in FYERS immediately; no blind retries")
            raise
        finally:
            journal.db.close()
