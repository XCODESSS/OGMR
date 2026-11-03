from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import time

from .model import IST, TradingError, ZERO, fill_price, number


@contextmanager
def single_runner(database):
    path = Path(str(database) + ".lock")
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError:
        raise TradingError(f"Runner lock exists: {path}. Check the process and broker before removing a stale lock.") from None
    try:
        os.write(fd, str(os.getpid()).encode())
        os.close(fd)
        yield
    finally:
        path.unlink(missing_ok=True)


class Journal:
    def __init__(self, path, identity, initial_equity):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path)
        try:
            self._initialize(identity, initial_equity)
        except BaseException:
            self.db.close()
            raise

    def _initialize(self, identity, initial_equity):
        self.db.execute("PRAGMA synchronous=FULL")
        self.db.execute("CREATE TABLE IF NOT EXISTS session (day TEXT PRIMARY KEY, document TEXT NOT NULL)")
        self.day = identity["day"]
        for day, doc in self.db.execute("SELECT day,document FROM session"):
            old = json.loads(doc)
            if (old["identity"]["mode"] != identity["mode"]
                    or old["identity"].get("account") != identity.get("account")):
                raise TradingError("Journal belongs to a different mode/account")
            if day != self.day and (old["phase"] != "closed" or any(
                    x.get("state") in ("intent", "unknown") for x in old["orders"].values())):
                raise TradingError("Prior session is unresolved; reconcile it before starting a new day")
        row = self.db.execute("SELECT document FROM session WHERE day=?", (self.day,)).fetchone()
        if row:
            self.state = json.loads(row[0])
            if self.state["identity"] != identity:
                raise TradingError("Journal identity/config/input changed; preserve the original configuration to reconcile")
        else:
            prior = self.db.execute("SELECT document FROM session ORDER BY day DESC LIMIT 1").fetchone()
            prior_equity = json.loads(prior[0])["equity"] if prior and identity["mode"] == "paper" else initial_equity
            self.state = {"identity": identity, "phase": "waiting", "equity": str(prior_equity),
                          "start_equity": str(prior_equity), "orders": {}, "signals": [], "events": []}
            self.save()

    def save(self):
        with self.db:
            self.db.execute("INSERT OR REPLACE INTO session VALUES (?,?)", (self.day, json.dumps(self.state)))

    def event(self, message):
        self.state["events"].append({"time": datetime.now(IST).isoformat(), "message": message})
        self.save()
        print(f"{datetime.now(IST).isoformat()} {message}", flush=True)

    def order_tag(self, symbol, role):
        material = f"{self.day}:{symbol}:{role}:gapfade01"
        return "GF" + hashlib.sha256(material.encode()).hexdigest()[:24]

    def net_positions(self):
        result = {}
        for order in self.state["orders"].values():
            quantity = order.get("filled", 0) * order["side"]
            result[order["symbol"]] = result.get(order["symbol"], 0) + quantity
        return {symbol: qty for symbol, qty in result.items() if qty}

    def pnl(self, quotes=None, fee_bps="10"):
        rate = number(fee_bps) / 10000
        cashflow, fees = ZERO, ZERO
        for o in self.state["orders"].values():
            turnover = o.get("filled", 0) * number(o.get("price", "0"))
            cashflow -= o["side"] * turnover
            fees += turnover * rate
        for symbol, qty in self.net_positions().items():
            if not quotes or symbol not in quotes:
                raise TradingError("Cannot value an open position without its quote")
            q = quotes[symbol]
            liquidation = q.bid if qty > 0 else q.ask
            cashflow += qty * liquidation
            fees += abs(qty) * liquidation * rate
        return cashflow - fees


class PaperBroker:
    def __init__(self, journal, config):
        self.journal, self.config = journal, config

    def submit(self, symbol, quantity, side, role, quote=None):
        tag = self.journal.order_tag(symbol, role)
        orders = self.journal.state["orders"]
        if tag in orders:
            return orders[tag]
        if quote is None:
            raise TradingError("Paper fills require a fresh bid/ask quote")
        price = fill_price(quote.ask if side == 1 else quote.bid, side,
                           number(self.config.slippage_bps_per_leg))
        order = {"symbol": symbol, "qty": quantity, "side": side, "role": role,
                 "state": "terminal", "filled": quantity, "price": str(price),
                 "time": datetime.now(IST).isoformat()}
        orders[tag] = order
        self.journal.save()
        self.journal.event(f"PAPER {role} {symbol} side={side} qty={quantity} price={price}")
        return order

    def reconcile(self):
        return None


class LiveBroker:
    TERMINAL = {1, 2, 5, 7}

    def __init__(self, client, journal, config, *, clock=time.monotonic, sleep=time.sleep):
        self.client, self.journal, self.config = client, journal, config
        self.clock, self.sleep = clock, sleep

    @staticmethod
    def _tag(row):
        tag = row.get("orderTag", "")
        return tag[2:] if tag.startswith("1:") else tag

    def resolve(self, tag, *, cancel_pending=False):
        intent = self.journal.state["orders"][tag]
        rows = [r for r in self.client.orderbook() if self._tag(r) == tag]
        if len(rows) != 1:
            raise TradingError(f"Order {tag} is absent or ambiguous; do not resubmit. Inspect FYERS manually.")
        row = rows[0]
        if (row.get("symbol") != intent["symbol"] or row.get("productType") != "INTRADAY"
                or int(row.get("qty", -1)) != intent["qty"] or int(row.get("side", 0)) != intent["side"]):
            raise TradingError("FYERS order does not match its durable intent")
        status = int(row["status"])
        if status not in {1, 2, 4, 5, 6, 7}:
            raise TradingError("Unknown FYERS order status")
        filled = int(row["filledQty"])
        if not 0 <= filled <= intent["qty"] or (status == 2 and filled != intent["qty"]):
            raise TradingError("Invalid FYERS cumulative fill quantity")
        if filled < intent.get("filled", 0):
            raise TradingError("FYERS cumulative fills decreased; manual reconciliation required")
        intent.update(broker_id=str(row["id"]), filled=filled,
                      price=str(number(row["tradedPrice"], positive=filled > 0)),
                      broker_status=status, state="terminal" if status in self.TERMINAL else "unknown")
        self.journal.save()
        if status not in self.TERMINAL and cancel_pending:
            self.client.cancel(str(row["id"]))
        return status in self.TERMINAL

    def reconcile(self):
        for tag, order in self.journal.state["orders"].items():
            if order["state"] != "terminal" and not self.resolve(tag):
                raise TradingError("A previous order is still pending. Reconcile/cancel it in FYERS before restarting.")
        actual = {}
        for row in self.client.positions():
            if row.get("productType") == "INTRADAY" and int(row["netQty"]):
                symbol = row["symbol"]
                actual[symbol] = actual.get(symbol, 0) + int(row["netQty"])
        if actual != self.journal.net_positions():
            raise TradingError("FYERS INTRADAY positions differ from the bot journal; no new orders will be sent")
        known = set(self.journal.state["orders"])
        for row in self.client.orderbook():
            if (row.get("productType") == "INTRADAY" and int(row["status"]) not in self.TERMINAL
                    and self._tag(row) not in known):
                raise TradingError("Unowned pending intraday order exists; use an isolated bot session")

    def submit(self, symbol, quantity, side, role, quote=None):
        if quantity <= 0 or side not in (-1, 1):
            raise TradingError("Invalid order quantity/side")
        self.reconcile()
        existing = self.journal.net_positions().get(symbol, 0)
        if role.startswith("exit"):
            if existing * side >= 0 or quantity > abs(existing):
                raise TradingError("Exit would increase or reverse the bot position")
        else:
            if existing or quote is None:
                raise TradingError("Entry requires a new position and a valid quote")
            value = quantity * max(quote.ask, quote.bid)
            if value > number(self.config.live_max_order_notional):
                raise TradingError("Live order exceeds the explicit per-order notional cap")
        tag = self.journal.order_tag(symbol, role)
        if tag in self.journal.state["orders"]:
            return self.journal.state["orders"][tag]
        order = {"symbol": symbol, "qty": quantity, "side": side, "role": role,
                 "state": "intent", "filled": 0, "price": "0"}
        self.journal.state["orders"][tag] = order
        self.journal.save()  # Commit BEFORE crossing the broker boundary.
        try:
            response = self.client.place(symbol, quantity, side, tag)
            order.update(state="unknown", broker_id=str(response.get("id", "")))
            self.journal.save()
            deadline = self.clock() + 15
            while self.clock() < deadline:
                try:
                    if self.resolve(tag):
                        self.journal.event(f"LIVE {role} {symbol} filled={order['filled']}/{quantity}")
                        return order
                except TradingError as exc:
                    if "absent or ambiguous" not in str(exc):
                        raise
                self.sleep(1)
            self.resolve(tag, cancel_pending=True)
            for _ in range(5):
                self.sleep(1)
                if self.resolve(tag):
                    return order
            raise TradingError("Order still unresolved after cancellation; manage it in FYERS")
        except Exception:
            order["state"] = "unknown"
            self.journal.save()
            raise


def flatten(broker, journal, quotes=None):
    broker.reconcile()
    for symbol in list(journal.net_positions()):
        for _ in range(3):
            quantity = journal.net_positions().get(symbol, 0)
            if not quantity:
                break
            count = sum(o["symbol"] == symbol and o["role"].startswith("exit")
                        for o in journal.state["orders"].values())
            if count >= 3:
                raise TradingError(f"Exit attempts exhausted for {symbol}; manage remaining position in FYERS")
            broker.submit(symbol, abs(quantity), -1 if quantity > 0 else 1,
                          f"exit{count}", (quotes or {}).get(symbol))
        if journal.net_positions().get(symbol):
            raise TradingError(f"Unclosed position: {symbol}")
    broker.reconcile()
    journal.state["equity"] = str(number(journal.state["start_equity"]) + journal.pnl(fee_bps=broker.config.fee_bps_per_leg))
    journal.state["phase"] = "closed"
    journal.save()
    journal.event("All bot positions confirmed flat; session closed")
