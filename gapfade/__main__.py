import argparse
from contextlib import closing
from dataclasses import replace
from datetime import date
import json
from pathlib import Path
import sys

from . import backtest, data, demo
from .config import Config
from .fyers import FyersClient, download, login
from .model import TradingError
from .runner import run_session


def main(argv=None):
    parser = argparse.ArgumentParser(description="Gap-fade research: NSE equity runner and offline NIFTY options")
    sub = parser.add_subparsers(dest="command", required=True)
    p = sub.add_parser("demo", help="Generate fictitious inputs; never submits orders")
    p.add_argument("--out", default="data/demo")
    p = sub.add_parser("login", help="Interactive FYERS OAuth; writes a local ignored token file")
    p.add_argument("--token-file", default=".secrets/fyers.json")
    p = sub.add_parser("check", help="Read-only account/data connectivity check")
    p.add_argument("--token-file", default=".secrets/fyers.json")
    p.add_argument("--symbol", default="NSE:SBIN-EQ")
    p = sub.add_parser("fetch", help="Download historical data; reference rows need an audit")
    p.add_argument("--universe", required=True)
    p.add_argument("--calendar", required=True)
    p.add_argument("--out", required=True)
    p.add_argument("--token-file", default=".secrets/fyers.json")
    p = sub.add_parser("backtest")
    p.add_argument("--config", default="config/paper.json")
    p.add_argument("--universe", required=True)
    p.add_argument("--calendar", required=True)
    p.add_argument("--references", required=True)
    p.add_argument("--bars", required=True)
    p.add_argument("--out", required=True)
    p = sub.add_parser("options-demo", help="Generate fictitious options inputs; offline only")
    p.add_argument("--out", required=True)
    for command in ("options-audit", "options-backtest"):
        p = sub.add_parser(command, help="Offline NIFTY options CSV research")
        p.add_argument("--data", required=True)
        p.add_argument("--start", required=True)
        p.add_argument("--end", required=True)
        p.add_argument("--out", required=True)
        if command == "options-backtest":
            p.add_argument("--config", default="config/options-research.json")
            p.add_argument("--no-confirmation", action="store_true")
    for mode in ("paper", "live"):
        p = sub.add_parser(mode)
        p.add_argument("--config", default="config/paper.json")
        p.add_argument("--universe", required=True)
        p.add_argument("--calendar", required=True)
        p.add_argument("--baselines", required=True)
        p.add_argument("--database", default=f"runs/{mode}.sqlite")
        p.add_argument("--token-file", default=".secrets/fyers.json")
        p.add_argument("--kill-file", default="STOP_TRADING")
    p = sub.add_parser("status", help="Inspect local journal only; does not contact FYERS")
    p.add_argument("--database", required=True)
    args = parser.parse_args(argv)
    try:
        if args.command == "demo":
            demo.create(args.out)
            print(f"Synthetic demo inputs written to {args.out}")
        elif args.command == "login":
            login(args.token_file)
        elif args.command == "check":
            client = FyersClient(args.token_file)
            client.account_fingerprint()
            quote = client.quotes([args.symbol])[args.symbol]
            print(json.dumps({"authenticated": True, "symbol": quote.symbol,
                              "quote_time": quote.timestamp.isoformat(), "bid": str(quote.bid),
                              "ask": str(quote.ask), "orders_submitted": 0}, indent=2))
        elif args.command == "fetch":
            download(FyersClient(args.token_file), data.universe(args.universe), data.sessions(args.calendar), args.out)
            print("Download complete. Audit references before marking verified=true.")
        elif args.command == "backtest":
            print(json.dumps(backtest.run(Config.load(args.config), args.universe, args.calendar,
                                         args.references, args.bars, args.out), indent=2))
        elif args.command == "options-demo":
            from .options_demo import create
            create(args.out)
            print(f"Synthetic options inputs written to {args.out}; no market performance verified.")
        elif args.command in ("options-audit", "options-backtest"):
            from .options_data import OptionsDataset, audit
            from .options_model import OptionsConfig
            from .options_backtest import run
            start, end = date.fromisoformat(args.start), date.fromisoformat(args.end)
            if args.command == "options-audit":
                result = audit(OptionsDataset(args.data), start, end, Path(args.out))
            else:
                config = OptionsConfig.load(args.config)
                if args.no_confirmation:
                    config = replace(config, confirmation=False)
                result = run(config, Path(args.data), start, end, Path(args.out))
            print(json.dumps(result, indent=2))
        elif args.command in ("paper", "live"):
            config = Config.load(args.config)
            if args.command == "live":
                from .runner import live_enabled
                live_enabled(config)
            run_session(config, FyersClient(args.token_file), args.universe, args.calendar,
                        args.baselines, args.database, mode=args.command, kill_file=args.kill_file)
        elif args.command == "status":
            import sqlite3
            with closing(sqlite3.connect(Path(args.database).resolve().as_uri() + "?mode=ro", uri=True)) as db:
                for day, document in db.execute("SELECT day,document FROM session ORDER BY day"):
                    state = json.loads(document)
                    print(json.dumps({"session": day, "phase": state["phase"], "mode": state["identity"]["mode"],
                                      "equity": state["equity"], "orders": state["orders"],
                                      "last_events": state["events"][-5:]}, indent=2))
    except (TradingError, OSError, ValueError, KeyError) as exc:
        # Errors from the REST adapter contain no response bodies or credentials.
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("Stopped. Open/pending LIVE orders are NOT assumed closed; check FYERS now.", file=sys.stderr)
        return 130
    return 0


if __name__ == "__main__":
    sys.exit(main())
