"""Read-only local audit of downloaded FYERS files; no account or API access."""
import argparse
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from gapfade.fyers_data_audit import audit_sources
from gapfade.model import TradingError


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--sample', required=True)
    parser.add_argument('--history', required=True)
    parser.add_argument('--out', required=True)
    args = parser.parse_args()
    try:
        result = audit_sources(args.sample, args.history, args.out)
    except (TradingError, OSError, ValueError, KeyError, TypeError) as exc:
        print(f'Audit failed: {exc}', file=sys.stderr)
        return 1
    print(json.dumps({key: result[key] for key in ('status', 'backtest_ready', 'structural_status', 'counts')}, indent=2))
    return 0 if result['backtest_ready'] else 2


if __name__ == '__main__':
    raise SystemExit(main())
