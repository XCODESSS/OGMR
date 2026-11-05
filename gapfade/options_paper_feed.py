"""FYERS market-data-only adapter for forward paper testing. GET endpoints only."""
import csv
from datetime import date, datetime, timedelta
import io
import json
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from .model import Bar, IST, TradingError, number
from .options_model import OptionContract

SPOT = 'NSE:NIFTY50-INDEX'
MASTER = 'https://public.fyers.in/sym_details/NSE_FO.csv'
ALLOWED = {'/data/quotes', '/data/history'}


def parse_master(text, observed_at, sessions):
    """Current snapshot availability, not reconstructed historical listing dates."""
    result, seen = [], set()
    day, horizon = observed_at.date(), max(sessions)
    for row in csv.reader(io.StringIO(text)):
        if not row or row[0] == 'Fytoken':
            continue
        if len(row) < 18:
            raise TradingError('Unexpected FYERS symbol-master column count')
        if row[13] not in ('NIFTY', 'NIFTY50', 'NIFTY50-INDEX') or row[16] not in ('CE', 'PE'):
            continue
        expiry = (datetime.fromtimestamp(int(row[8]), IST).date()
                  if row[8].isdigit() else date.fromisoformat(row[8]))
        if not day <= expiry <= horizon:
            continue
        if expiry not in sessions:
            raise TradingError('Calendar omits an expiry present in the current symbol master')
        symbol, lot = row[9], int(row[3])
        if not symbol.startswith('NSE:NIFTY') or symbol in seen or lot <= 0:
            raise TradingError('Invalid or duplicate current NIFTY contract')
        seen.add(symbol)
        result.append(OptionContract(symbol, 'NIFTY', row[16], number(row[15], positive=True),
            expiry, observed_at, day, expiry, lot))
    if not result:
        raise TradingError('No NIFTY options within the supplied calendar horizon')
    return result


class MarketData:
    """No order, position, account or cancellation methods exist on this adapter."""
    def __init__(self, token_file, output, now_fn=lambda: datetime.now(IST)):
        credentials = json.loads(Path(token_file).read_text(encoding='utf-8-sig'))
        self._authorization = f"{credentials['client_id']}:{credentials['access_token']}"
        self.output, self.now_fn = Path(output), now_fn
        self.cache = {}
        self.limit = 1000
        self.usage = self.output / 'data_request_usage.json'
        self.requests = json.loads(self.usage.read_text())['requests'] if self.usage.exists() else 0

    def get(self, path, params):
        if path not in ALLOWED:
            raise TradingError('Paper feed refuses any endpoint outside the market-data allowlist')
        if self.requests >= self.limit:
            raise TradingError('Local paper-data request budget exhausted (1000 per output session)')
        self.requests += 1
        temporary = self.output / 'data_request_usage.tmp'
        temporary.write_text(json.dumps(dict(requests=self.requests, limit=self.limit)), encoding='utf-8')
        temporary.replace(self.usage)
        request = Request('https://api-t1.fyers.in' + path + '?' + urlencode(params), headers={
            'Authorization': self._authorization, 'Content-Type': 'application/json',
            'User-Agent': 'ogmr-options-paper/1.0'})
        try:
            with urlopen(request, timeout=8) as response:
                obj = json.load(response)
        except HTTPError as exc:
            raise TradingError(f'FYERS market-data HTTP {exc.code}; no order was submitted') from None
        except (URLError, TimeoutError, OSError, ValueError):
            raise TradingError('FYERS market-data transport/response failure') from None
        if not isinstance(obj, dict) or obj.get('s') != 'ok':
            raise TradingError('FYERS rejected market-data request; check token and Market Data permission')
        with (self.output / 'market_data.jsonl').open('a', encoding='utf-8') as stream:
            stream.write(json.dumps(dict(received_at=self.now_fn().isoformat(), endpoint=path,
                                         parameters=params, response=obj)) + '\n')
        return obj

    def master(self, sessions):
        path = self.output / 'symbol_master.csv'
        stamp = self.output / 'symbol_master_received.json'
        if not path.exists():
            if stamp.exists():
                raise TradingError('Partial symbol-master snapshot; use a new output folder before the session')
            try:
                with urlopen(Request(MASTER, headers={'User-Agent': 'ogmr-options-paper/1.0'}), timeout=30) as response:
                    text = response.read().decode('utf-8-sig')
            except (URLError, TimeoutError, OSError, ValueError):
                raise TradingError('Cannot download the public FYERS symbol master') from None
            observed = self.now_fn()
            parse_master(text, observed, sessions)
            path.write_text(text, encoding='utf-8')
            stamp.write_text(json.dumps(dict(observed_at=observed.isoformat())), encoding='utf-8')
        observed = datetime.fromisoformat(json.loads(stamp.read_text())['observed_at'])
        if observed.date() != self.now_fn().date():
            raise TradingError('Symbol-master snapshot is not from today; use a new session output')
        return parse_master(path.read_text(encoding='utf-8-sig'), observed, sessions)

    def values(self, symbol):
        obj = self.get('/data/quotes', {'symbols': symbol})
        entries = obj.get('d')
        if not isinstance(entries, list) or len(entries) != 1 or entries[0].get('n') != symbol or entries[0].get('s') != 'ok':
            raise TradingError('Incomplete or rejected per-symbol quote')
        return entries[0]['v']

    def reference(self):
        values = self.values(SPOT)
        return number(values['open_price'], positive=True), number(values['prev_close_price'], positive=True)

    def quote(self, symbol):
        values = self.values(symbol)
        now = self.now_fn()
        moment = datetime.fromtimestamp(int(values['tt']), IST)
        bid, ask = number(values['bid'], positive=True), number(values['ask'], positive=True)
        if not -2 <= (now - moment).total_seconds() <= 10 or bid > ask:
            raise TradingError('Stale, future-dated or crossed option bid/ask; paper fill refused')
        return dict(bid=bid, ask=ask, timestamp=moment, received_at=now)

    def bars(self, symbol, first, last):
        if last >= self.now_fn().replace(second=0, microsecond=0):
            raise TradingError('Paper feed refuses unfinished minute candles')
        key = symbol, first, last
        if key not in self.cache:
            obj = self.get('/data/history', dict(symbol=symbol, resolution='1', date_format=0,
                range_from=str(int(first.timestamp())), range_to=str(int(last.timestamp())), cont_flag=0))
            bars = {}
            for row in obj.get('candles', []):
                if not isinstance(row, list) or len(row) != 6:
                    raise TradingError('Unexpected minute candle schema')
                if isinstance(row[0], bool) or not isinstance(row[0], (int, float)) or number(row[0]) != int(row[0]):
                    raise TradingError('Invalid candle epoch')
                moment = datetime.fromtimestamp(int(row[0]), IST)
                if not first <= moment <= last:
                    continue
                opening, high, low, close = [number(v, positive=True) for v in row[1:5]]
                volume = number(row[5])
                if moment.second or moment.microsecond or moment in bars or low > min(opening, close) or high < max(opening, close) or low > high or volume < 0:
                    raise TradingError('Invalid minute candle returned by FYERS')
                bars[moment] = Bar(moment, 'NIFTY' if symbol == SPOT else symbol, opening, high, low, close, volume)
            expected = int((last - first).total_seconds() // 60) + 1
            if len(bars) != expected:
                raise TradingError('Completed minute data has not arrived or has a gap; no fill invented')
            self.cache[key] = bars
        return self.cache[key]
