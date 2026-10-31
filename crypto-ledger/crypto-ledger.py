#!/usr/bin/env python3
"""Crypto ledger: archives the movements of crypto exchange accounts, then replays them.

With read-only API keys it collects every movement of the account (trades, Convert, deposits, withdrawals, fiat,
dust, distributions) into an append-only archive: a movement already archived is never fetched or processed again.
The replay gives, per asset, the quantity held and the average cost in EUR. It knows nothing of Sure or Kresus.

Exchanges: Binance and Kraken. A withdrawal from one paired with a deposit on the other is a transfer: the coins keep
their cost.

Usage:
    crypto-ledger.py sync              fetch what is new (the first run walks back the whole history)
    crypto-ledger.py summary           per exchange, the replayed quantities against the live balances; then, all
                                       exchanges together: transfers, cost, value and latent gain per asset
    crypto-ledger.py events [ASSET]    the normalized movements, oldest first
    crypto-ledger.py export            write positions.json for the Sure bridge (sync does it too)
"""
import argparse
import base64
import hashlib
import hmac
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from decimal import Decimal

ENV_FILE = os.environ.get("CRYPTO_LEDGER_ENV", "/etc/sure-bridge.env")
DATA_DIR = os.environ.get("CRYPTO_LEDGER_DIR", "/var/lib/crypto-ledger")
DAY = 86400000
HOUR = 3600000
# Binance opened in July 2017: nothing older to fetch
BINANCE_START = "2017-07-01"
EARN_START = "2023-01-01"
# Quote assets tried when looking for the pairs traded (with the assets seen in the account)
QUOTES = {"EUR", "USDT", "USDC", "FDUSD", "BUSD", "BTC", "ETH", "BNB"}
FIAT = {"EUR"}
# Kraken asset codes -> usual tickers. Suffixes (.S staked, .M opt-in rewards, .B bonded, .F, .P) are dropped: the
# coins are still held, and moving them between spot and staking or Earn then nets to nothing
KRAKEN_ASSETS = {
    "XXBT": "BTC", "XBT": "BTC", "XETH": "ETH", "ETH2": "ETH", "XXDG": "DOGE", "XDG": "DOGE", "XLTC": "LTC",
    "XXRP": "XRP", "XXLM": "XLM", "XETC": "ETC", "XZEC": "ZEC", "XXMR": "XMR", "XREP": "REP", "XMLN": "MLN",
    "ZEUR": "EUR", "ZUSD": "USD", "ZGBP": "GBP", "ZCAD": "CAD", "ZJPY": "JPY", "ZCHF": "CHF",
}


# --------------------------------------------------------------------------- helpers

def load_env():
    cfg = {}
    try:
        with open(ENV_FILE, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    k, v = line.split("=", 1)
                    cfg[k.strip()] = v.strip().strip('"').strip("'")
    except OSError:
        pass
    cfg.update({k: v for k, v in os.environ.items() if k.startswith(("BINANCE_", "KRAKEN_", "CRYPTO_LEDGER_"))})
    return cfg


def read_json(path, default):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


def write_json(path, data):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=1, sort_keys=True)
    os.replace(tmp, path)


def ms(day):
    return int(datetime.strptime(day, "%Y-%m-%d").replace(tzinfo=timezone.utc).timestamp() * 1000)


def iso(t):
    return datetime.fromtimestamp(t / 1000, timezone.utc).strftime("%Y-%m-%d %H:%M")


def D(x):
    return Decimal(str(x)) if x not in (None, "") else Decimal(0)


class Archive:
    """Append-only archive of one exchange: one JSON line per movement, keyed by the exchange's own id. The raw
    record is kept as received, so the normalization can be fixed later without asking the exchange again."""

    def __init__(self, name):
        os.makedirs(DATA_DIR, exist_ok=True)
        self.path = os.path.join(DATA_DIR, name + ".jsonl")
        self.cursor_path = os.path.join(DATA_DIR, name + "-cursors.json")
        self.records = {}
        if os.path.exists(self.path):
            with open(self.path, encoding="utf-8") as f:
                for line in f:
                    if line.strip():
                        r = json.loads(line)
                        self.records[r["key"]] = r
        self.cursors = read_json(self.cursor_path, {})
        self.added = {}

    def add(self, kind, key, raw, **meta):
        key = "%s:%s" % (kind, key)
        if key in self.records:
            return False
        r = {"key": key, "kind": kind, "raw": raw}
        if meta:
            r["meta"] = meta
        with open(self.path, "a", encoding="utf-8") as f:
            f.write(json.dumps(r, separators=(",", ":"), sort_keys=True) + "\n")
        self.records[key] = r
        self.added[kind] = self.added.get(kind, 0) + 1
        return True

    def set_cursor(self, name, value):
        self.cursors[name] = value
        write_json(self.cursor_path, self.cursors)

    def walk(self, name, start, window, fetch, now):
        """Calls fetch(begin, end) on every window from the cursor (minus one day of overlap, for late records) to
        now. fetch returns the time of the oldest record still pending, if any: the cursor then stays before it,
        so that the record is fetched again once final."""
        begin = max(start, self.cursors.get(name, start) - DAY)
        hold = None
        while begin <= now:
            end = min(begin + window - 1, now)
            pending = fetch(begin, end)
            if pending is not None and hold is None:
                hold = pending - 1
            self.set_cursor(name, hold if hold is not None else end)
            begin = end + 1


# --------------------------------------------------------------------------- Binance

class Binance:
    BASE = "https://api.binance.com"

    def __init__(self, key=None, secret=None, pause=0.25):
        self.key, self.secret, self.pause = key, secret, pause

    def call(self, path, signed=True, method="GET", **params):
        params = {k: v for k, v in params.items() if v is not None}
        for attempt in range(5):
            if signed:
                params.update(timestamp=int(time.time() * 1000), recvWindow=10000)
                params.pop("signature", None)
                q = urllib.parse.urlencode(params)
                q += "&signature=" + hmac.new(self.secret.encode(), q.encode(), hashlib.sha256).hexdigest()
            else:
                q = urllib.parse.urlencode(params)
            req = urllib.request.Request(self.BASE + path + ("?" + q if q else ""), method=method,
                                         headers={"X-MBX-APIKEY": self.key} if signed else {})
            try:
                with urllib.request.urlopen(req, timeout=30) as r:
                    time.sleep(self.pause)
                    return json.loads(r.read())
            except urllib.error.HTTPError as e:
                body = e.read().decode(errors="replace")[:300]
                e.close()
                if e.code in (418, 429) and attempt < 4:
                    # the quotas are per minute, and Binance may answer Retry-After: 0
                    time.sleep(max(int(e.headers.get("Retry-After") or 0), 61))
                    continue
                raise RuntimeError("Binance HTTP %s on %s: %s" % (e.code, path, body))


def binance_time(text):
    """'2021-04-01 12:00:00' (UTC, as in the withdrawal history) -> ms."""
    return int(datetime.strptime(text, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc).timestamp() * 1000)


class BinanceCollector:
    def __init__(self, api, arc, start):
        self.api, self.arc, self.start = api, arc, ms(start)
        self.now = int(time.time() * 1000)

    def sync(self):
        a = self.arc
        a.walk("deposits", self.start, 90 * DAY, self.deposits, self.now)
        a.walk("withdrawals", self.start, 90 * DAY, self.withdrawals, self.now)
        # the fiat history is costly in request weight (a few calls a minute): one window, paged
        a.walk("fiat_orders", self.start, self.now - self.start + 1, self.fiat_orders, self.now)
        a.walk("fiat_payments", self.start, self.now - self.start + 1, self.fiat_payments, self.now)
        a.walk("convert", self.start, 30 * DAY, self.convert, self.now)
        a.walk("dust", self.start, 90 * DAY, self.dust, self.now)
        a.walk("dividends", self.start, 90 * DAY, self.dividends, self.now)
        a.walk("eth_staking", self.start, 89 * DAY, self.eth_staking, self.now)
        # Simple Earn replaced the savings in 2023; its real-time rewards are not in the distributions
        a.walk("earn_realtime", max(self.start, ms(EARN_START)), 29 * DAY, self.earn_realtime, self.now)
        self.trades()

    def paged(self, path, begin, end, key=None, **params):
        out, offset = [], 0
        while True:
            rows = self.api.call(path, startTime=begin, endTime=end, offset=offset, limit=1000, **params)
            rows = rows.get(key, []) if key else rows
            out += rows
            if len(rows) < 1000:
                return out
            offset += 1000

    def deposits(self, begin, end):
        pending = None
        for x in self.paged("/sapi/v1/capital/deposit/hisrec", begin, end):
            if x["status"] in (1, 6):  # success, credited but not yet withdrawable
                self.arc.add("deposit", x.get("id") or "%s:%s" % (x["txId"], x["coin"]), x)
            elif x["status"] in (0, 8):  # pending, waiting for confirmations
                pending = min(pending or x["insertTime"], x["insertTime"])
        return pending

    def withdrawals(self, begin, end):
        pending = None
        for x in self.paged("/sapi/v1/capital/withdraw/history", begin, end):
            if x["status"] == 6:  # completed
                self.arc.add("withdrawal", x["id"], x)
            elif x["status"] in (0, 2, 4):  # email sent, awaiting approval, processing
                t = binance_time(x["applyTime"])
                pending = min(pending or t, t)
        return pending

    def fiat(self, path, kinds, begin, end):
        pending = None
        for ttype, kind in kinds:
            page = 1
            while True:
                rows = self.api.call(path, transactionType=ttype, beginTime=begin, endTime=end,
                                     page=page, rows=500).get("data") or []
                for x in rows:
                    if x["status"] in ("Successful", "Completed"):
                        self.arc.add(kind, x["orderNo"], x)
                    elif x["status"] in ("Processing", "Pending"):
                        pending = min(pending or x["createTime"], x["createTime"])
                if len(rows) < 500:
                    break
                page += 1
        return pending

    def fiat_orders(self, begin, end):
        return self.fiat("/sapi/v1/fiat/orders", ((0, "fiat_deposit"), (1, "fiat_withdrawal")), begin, end)

    def fiat_payments(self, begin, end):
        return self.fiat("/sapi/v1/fiat/payments", ((0, "card_buy"), (1, "card_sell")), begin, end)

    def convert(self, begin, end):
        r = self.api.call("/sapi/v1/convert/tradeFlow", startTime=begin, endTime=end, limit=1000)
        for x in r.get("list") or []:
            if x.get("orderStatus") == "SUCCESS":
                self.arc.add("convert", x["orderId"], x)

    def dust(self, begin, end):
        r = self.api.call("/sapi/v1/asset/dribblet", startTime=begin, endTime=end)
        for group in r.get("userAssetDribblets") or []:
            for x in group.get("userAssetDribbletDetails") or []:
                x = dict(x, operateTime=x.get("operateTime") or group["operateTime"])
                self.arc.add("dust", "%s:%s" % (x["transId"], x["fromAsset"]), x)

    def dividends(self, begin, end):
        rows = self.api.call("/sapi/v1/asset/assetDividend", startTime=begin, endTime=end, limit=500).get("rows") or []
        if len(rows) >= 500 and end - begin > HOUR:  # too many in this window: split it
            mid = (begin + end) // 2
            self.dividends(begin, mid)
            self.dividends(mid + 1, end)
            return
        for x in rows:
            self.arc.add("dividend", x.get("id") or x["tranId"], x)

    def rows(self, path, begin, end, **params):
        """Endpoints answering {rows, total}, paged with current/size."""
        out, page = [], 1
        while True:
            r = self.api.call(path, startTime=begin, endTime=end, current=page, size=100, **params)
            rows = r.get("rows") or []
            out += rows
            if len(rows) < 100:
                return out
            page += 1

    def eth_staking(self, begin, end):
        """ETH staked for WBETH and back, WBETH wrapped and unwrapped: the same coins under another name."""
        for path, kind in (("/sapi/v1/eth-staking/eth/history/stakingHistory", "eth_stake"),
                           ("/sapi/v1/eth-staking/eth/history/redemptionHistory", "eth_redeem"),
                           ("/sapi/v1/eth-staking/wbeth/history/wrapHistory", "wbeth_wrap"),
                           ("/sapi/v1/eth-staking/wbeth/history/unwrapHistory", "wbeth_unwrap")):
            for x in self.rows(path, begin, end):
                if x.get("status") in ("SUCCESS", None):
                    amount = x.get("amount") or x.get("fromAmount")
                    self.arc.add(kind, "%s:%s" % (x["time"], amount), x)

    def earn_realtime(self, begin, end):
        for x in self.rows("/sapi/v1/simple-earn/flexible/history/rewardsRecord", begin, end, type="REALTIME"):
            self.arc.add("earn_realtime", "%s:%s:%s" % (x["time"], x.get("productId") or x["asset"], x["rewards"]), x)

    def assets_seen(self):
        seen = set()
        for r in self.arc.records.values():
            x, k = r["raw"], r["kind"]
            if k in ("deposit", "withdrawal"):
                seen.add(x["coin"])
            elif k == "convert":
                seen |= {x["fromAsset"], x["toAsset"]}
            elif k == "dust":
                seen.add(x["fromAsset"])
            elif k in ("dividend", "earn_realtime"):
                seen.add(x["asset"])
            elif k in ("card_buy", "card_sell"):
                seen.add(x["cryptoCurrency"])
            elif k == "trade":
                seen |= {r["meta"]["base"], r["meta"]["quote"]}
        return seen | set(binance_holdings(self.api, quiet=True))

    def trades(self):
        """Trades are listed per pair: the pairs whose two assets were seen in the account (or are a common quote)."""
        seen = self.assets_seen()
        info = self.api.call("/api/v3/exchangeInfo", signed=False)
        known = {r["meta"]["symbol"] for r in self.arc.records.values() if r["kind"] == "trade"}
        symbols = sorted(
            (s["symbol"], s["baseAsset"], s["quoteAsset"]) for s in info["symbols"]
            if s["symbol"] in known or (s["baseAsset"] in seen and s["quoteAsset"] in seen | QUOTES))
        for symbol, base, quote in symbols:
            name = "trades:" + symbol
            from_id = self.arc.cursors.get(name, -1) + 1
            while True:
                rows = self.api.call("/api/v3/myTrades", symbol=symbol, fromId=from_id, limit=1000)
                for x in rows:
                    self.arc.add("trade", "%s:%s" % (symbol, x["id"]), x, symbol=symbol, base=base, quote=quote)
                if rows:
                    from_id = rows[-1]["id"] + 1
                    self.arc.set_cursor(name, rows[-1]["id"])
                if len(rows) < 1000:
                    break


def binance_holdings(api, quiet=False):
    """Live quantities per asset: spot + funding + Simple Earn (flexible and locked)."""
    out = {}

    def put(asset, qty):
        if asset.startswith("LD") and len(asset) > 2:  # old flexible savings, shown in spot as LD<asset>
            asset = asset[2:]
        if qty:
            out[asset] = out.get(asset, Decimal(0)) + qty

    sources = (
        ("spot", lambda: [(b["asset"], D(b["free"]) + D(b["locked"]))
                          for b in api.call("/api/v3/account", omitZeroBalances="true")["balances"]]),
        ("funding", lambda: [(b["asset"], D(b["free"]) + D(b["locked"]) + D(b.get("freeze")) + D(b.get("withdrawing")))
                             for b in api.call("/sapi/v1/asset/get-funding-asset", method="POST")]),
        ("earn flexible", lambda: [(b["asset"], D(b["totalAmount"]))
                                   for b in api.call("/sapi/v1/simple-earn/flexible/position", size=100).get("rows") or []]),
        ("earn locked", lambda: [(b["asset"], D(b["amount"]))
                                 for b in api.call("/sapi/v1/simple-earn/locked/position", size=100).get("rows") or []]),
    )
    for name, fetch in sources:
        try:
            for asset, qty in fetch():
                put(asset, qty)
        except Exception as e:
            if not quiet:
                print("warning: live balances, %s not read: %s" % (name, e))
    return out


def binance_events(arc):
    """Normalized movements: {time, type, legs {asset: signed quantity}, key, [txid, cost_eur]}."""
    out = []

    def ev(t, kind, legs, key, **extra):
        net = {}
        for asset, q in legs:
            net[asset] = net.get(asset, Decimal(0)) + q
        out.append(dict(time=t, type=kind, legs={a: q for a, q in net.items() if q}, key=key, **extra))

    for r in arc.records.values():
        x, k, key = r["raw"], r["kind"], "binance:" + r["key"]
        if k == "trade":
            m, sign = r["meta"], 1 if x["isBuyer"] else -1
            ev(x["time"], "trade", [(m["base"], sign * D(x["qty"])), (m["quote"], -sign * D(x["quoteQty"])),
                                    (x["commissionAsset"], -D(x["commission"]))], key)
        elif k == "convert":
            ev(x["createTime"], "trade", [(x["fromAsset"], -D(x["fromAmount"])), (x["toAsset"], D(x["toAmount"]))], key)
        elif k == "dust":
            ev(x["operateTime"], "trade", [(x["fromAsset"], -D(x["amount"])), ("BNB", D(x["transferedAmount"]))], key)
        elif k == "deposit":
            ev(x["insertTime"], "deposit", [(x["coin"], D(x["amount"]))], key, txid=x.get("txId"))
        elif k == "withdrawal":
            # amount is what left for the destination, the fee is taken on top of it
            ev(binance_time(x["applyTime"]), "withdrawal", [(x["coin"], -D(x["amount"]) - D(x.get("transactionFee")))],
               key, txid=x.get("txId"), fee=D(x.get("transactionFee")))
        elif k == "dividend":
            ev(x["divTime"], "reward", [(x["asset"], D(x["amount"]))], key, label=x.get("enInfo"))
        elif k == "earn_realtime":
            ev(x["time"], "reward", [(x["asset"], D(x["rewards"]))], key, label="Simple Earn real-time")
        elif k in ("eth_stake", "eth_redeem"):
            ev(x["time"], "swap", [(x["asset"], -D(x["amount"])), (x["distributeAsset"], D(x["distributeAmount"]))], key)
        elif k in ("wbeth_wrap", "wbeth_unwrap"):
            ev(x["time"], "swap", [(x["fromAsset"], -D(x["fromAmount"])), (x["toAsset"], D(x["toAmount"]))], key)
        elif k == "fiat_deposit":
            # amount is what reached the account, the fee already taken (indicatedAmount is what was sent)
            ev(x["createTime"], "fiat", [(x["fiatCurrency"], D(x["amount"]))], key, fee=D(x.get("totalFee")))
        elif k == "fiat_withdrawal":
            sent = D(x["indicatedAmount"]) if x.get("indicatedAmount") else D(x["amount"]) + D(x.get("totalFee"))
            ev(x["createTime"], "fiat", [(x["fiatCurrency"], -sent)], key, fee=D(x.get("totalFee")))
        elif k == "card_buy":
            # paid with the fiat balance of the account, or by card: then the fiat never was in the account
            legs = [(x["cryptoCurrency"], D(x["obtainAmount"]))]
            if x.get("paymentMethod") == "Cash Balance":
                legs.append((x["fiatCurrency"], -D(x["sourceAmount"])))
            ev(x["createTime"], "trade", legs, key,
               cost_eur=float(x["sourceAmount"]) if x["fiatCurrency"] == "EUR" else None)
        elif k == "card_sell":
            ev(x["createTime"], "trade", [(x["cryptoCurrency"], -D(x["sourceAmount"]))], key,
               cost_eur=float(x["obtainAmount"]) if x["fiatCurrency"] == "EUR" else None)
    return out


# --------------------------------------------------------------------------- Kraken

class Kraken:
    BASE = "https://api.kraken.com"

    def __init__(self, key=None, secret=None, pause=1.0):
        self.key, self.secret, self.pause = key, secret, pause

    def call(self, method, **data):
        path = "/0/private/" + method
        for attempt in range(5):
            data["nonce"] = str(int(time.time() * 1000000))
            post = urllib.parse.urlencode(data)
            digest = path.encode() + hashlib.sha256((data["nonce"] + post).encode()).digest()
            sign = base64.b64encode(hmac.new(base64.b64decode(self.secret), digest, hashlib.sha512).digest()).decode()
            req = urllib.request.Request(self.BASE + path, data=post.encode(), method="POST", headers={
                "API-Key": self.key, "API-Sign": sign, "Content-Type": "application/x-www-form-urlencoded"})
            try:
                with urllib.request.urlopen(req, timeout=30) as r:
                    body = json.loads(r.read())
            except urllib.error.HTTPError as e:
                text = e.read().decode(errors="replace")[:300]
                e.close()
                raise RuntimeError("Kraken HTTP %s on %s: %s" % (e.code, method, text))
            time.sleep(self.pause)
            errors = body.get("error") or []
            if any("Rate limit" in x for x in errors) and attempt < 4:
                time.sleep(15)
                continue
            if errors:
                raise RuntimeError("Kraken %s: %s" % (method, ", ".join(errors)))
            return body["result"]


def kraken_asset(code):
    base = code.split(".")[0]
    return KRAKEN_ASSETS.get(base, base)


class KrakenCollector:
    """Everything goes through the ledger: one entry per balance change, the two sides of a trade sharing a refid."""

    def __init__(self, api, arc):
        self.api, self.arc = api, arc

    def sync(self):
        cursor = self.arc.cursors.get("ledgers")
        # newest first, 50 per page; one day of overlap after the last entry seen
        params = {} if cursor is None else {"start": int(cursor) - 86400}
        ofs, newest = 0, cursor
        while True:
            r = self.api.call("Ledgers", ofs=ofs, **params)
            rows = r.get("ledger") or {}
            for lid, x in rows.items():
                self.arc.add("ledger", lid, dict(x, id=lid))
                newest = max(newest or 0, float(x["time"]))
            ofs += len(rows)
            if not rows or ofs >= int(r.get("count") or 0):
                break
        if newest:
            self.arc.set_cursor("ledgers", newest)


def kraken_holdings(api):
    out = {}
    for code, qty in api.call("Balance").items():
        asset = kraken_asset(code)
        if D(qty):
            out[asset] = out.get(asset, Decimal(0)) + D(qty)
    return out


def kraken_events(arc):
    """The ledger entries grouped by refid: a trade is its two sides, a staking allocation nets to nothing."""
    groups = {}
    for r in arc.records.values():
        groups.setdefault(r["raw"]["refid"], []).append(r["raw"])
    out = []
    for refid, rows in groups.items():
        legs = {}
        for x in rows:
            a = kraken_asset(x["asset"])
            legs[a] = legs.get(a, Decimal(0)) + D(x["amount"]) - D(x["fee"])
        legs = {a: q for a, q in legs.items() if q}
        if not legs:
            continue
        types = {x["type"] for x in rows}
        positive, negative = all(q > 0 for q in legs.values()), all(q < 0 for q in legs.values())
        if types & {"trade", "spend", "receive", "conversion"} or not (positive or negative):
            kind = "trade"
        elif "deposit" in types:
            kind = "deposit"
        elif "withdrawal" in types or negative:
            kind = "withdrawal"
        else:  # staking, earn rewards, dividend, airdrop, credit...
            kind = "reward"
        if all(a in FIAT for a in legs) and kind in ("deposit", "withdrawal"):
            kind = "fiat"
        out.append(dict(time=int(min(float(x["time"]) for x in rows) * 1000), type=kind, legs=legs,
                        key="kraken:%s" % refid, label=",".join(sorted(types)),
                        fee=sum((D(x["fee"]) for x in rows), Decimal(0))))
    return out


# --------------------------------------------------------------------------- prices and replay

class Pricer:
    """EUR price of an asset at a time: close of the hourly Binance candle of <asset>EUR, else through USDT or BTC.
    Cached in prices.json (public data, no key needed)."""

    def __init__(self, api):
        self.api = api
        self.path = os.path.join(DATA_DIR, "prices.json")
        self.cache = read_json(self.path, {})
        self.dirty = False

    def close(self, symbol, t):
        key = "%s:%d" % (symbol, t // HOUR)
        if key not in self.cache:
            if self.cache.get("invalid:" + symbol):
                return None
            try:
                rows = self.api.call("/api/v3/klines", signed=False, symbol=symbol, interval="1h",
                                     startTime=t // HOUR * HOUR, limit=1)
            except RuntimeError as e:
                if "Invalid symbol" in str(e):
                    self.cache["invalid:" + symbol] = True
                    self.dirty = True
                    return None
                raise
            self.cache[key] = float(rows[0][4]) if rows and rows[0][0] <= t < rows[0][0] + HOUR else None
            self.dirty = True
        return self.cache[key]

    def eur(self, asset, t):
        if asset == "EUR":
            return 1.0
        p = self.close(asset + "EUR", t)
        if p:
            return p
        if asset in ("USDT", "USD"):
            p = self.close("EURUSDT", t)
            return 1 / p if p else None
        for via in ("USDT", "BTC"):
            if asset != via:
                p, q = self.close(asset + via, t), None
                if p:
                    q = self.eur(via, t)
                if p and q:
                    return p * q
        return None

    def save(self):
        if self.dirty:
            write_json(self.path, self.cache)


TRANSFER_WINDOW = 3 * DAY
TRANSFER_FEE_MAX = Decimal("0.03")  # what is received may be up to 3 % less than what left (network fee)


def single_leg(e):
    if len(e["legs"]) != 1:
        return None, None
    return next(iter(e["legs"].items()))


def match_transfers(events):
    """Pairs each withdrawal with the deposit it became on another exchange: same asset, the same quantity or a
    little less, arrived within three days. The same transaction id wins, then the closest in time."""
    deposits = [e for e in events if e["type"] == "deposit"]
    candidates = []
    for w in events:
        if w["type"] != "withdrawal":
            continue
        asset, sent = single_leg(w)
        if asset is None or asset in FIAT:
            continue
        sent = -sent
        for d in deposits:
            got_asset, got = single_leg(d)
            if got_asset != asset or d["key"].split(":")[0] == w["key"].split(":")[0]:
                continue
            if not 0 <= d["time"] - w["time"] <= TRANSFER_WINDOW or not sent - sent * TRANSFER_FEE_MAX <= got <= sent:
                continue
            same_tx = bool(w.get("txid")) and w.get("txid") == d.get("txid")
            if w.get("txid") and d.get("txid") and not same_tx:
                continue
            candidates.append((0 if same_tx else 1, d["time"] - w["time"], w["key"], d["key"]))
    used, pairs = set(), {}
    for _, _, wk, dk in sorted(candidates):
        if wk not in used and dk not in used:
            used |= {wk, dk}
            pairs[wk] = dk
    return pairs


METHODS = ("carry", "market")


class Book:
    """Replays the movements with a weighted average cost per asset (EUR is cash, it has no cost). A withdrawal paired
    with a deposit on another exchange carries its cost there: the coins changed place, not owner.

    method "carry": a crypto-to-crypto trade is not a sale, the cost of what was given goes to what was received, so
    the cost is the EUR put in (Koinly's pfu_france does that). "market": every trade is a sale at the market price,
    what was received starts at that price and the gain or loss of what was given is realized."""

    def __init__(self, pricer, method="carry"):
        if method not in METHODS:
            raise ValueError("unknown method %r (%s)" % (method, ", ".join(METHODS)))
        self.pricer, self.method = pricer, method
        self.qty, self.cost = {}, {}
        self.realized = 0.0
        self.transfers_out, self.deposits_in, self.warnings = [], [], []
        self.links, self.linked_in, self.in_transit, self.transfers = {}, set(), {}, []
        # net change of quantity and cost per UTC day and asset: what an account fed day by day receives
        self.days, self.day = {}, None

    def track(self, asset, q, cost):
        if asset not in FIAT:
            row = self.days.setdefault(self.day, {}).setdefault(asset, [Decimal(0), 0.0])
            row[0] += q
            row[1] += cost

    def value(self, asset, q, t):
        p = self.pricer.eur(asset, t)
        return None if p is None else float(q) * p

    def acquire(self, asset, q, cost):
        self.qty[asset] = self.qty.get(asset, Decimal(0)) + q
        if asset not in FIAT:
            self.cost[asset] = self.cost.get(asset, 0.0) + cost
        self.track(asset, q, cost)

    def remove(self, asset, q, e):
        held = self.qty.get(asset, Decimal(0))
        if q > held + Decimal("1e-8") and asset not in FIAT:
            self.warnings.append("%s %s: %s %s removed, only %s held (history missing?)"
                                 % (iso(e["time"]), e["key"], q, asset, held))
        self.qty[asset] = held - q
        if asset in FIAT:
            return 0.0
        cost = self.cost.get(asset, 0.0)
        out = cost * float(min(q, held) / held) if held > 0 else 0.0
        self.cost[asset] = cost - out
        self.track(asset, -q, -out)
        return out

    def apply(self, e):
        t, legs = e["time"], e["legs"]
        self.day = iso(t)[:10]
        gave = {a: -q for a, q in legs.items() if q < 0}
        got = {a: q for a, q in legs.items() if q > 0}
        if e["type"] == "fiat":
            for a, q in legs.items():
                self.qty[a] = self.qty.get(a, Decimal(0)) + q
        elif e["key"] in self.in_transit:  # arrival of a transfer: the cost of what left
            for a, q in got.items():
                w = self.in_transit.pop(e["key"])
                self.acquire(a, q, w["cost"])
                self.transfers.append(dict(w, received=q, arrived=t, to=e["key"].split(":")[0]))
        elif e["type"] in ("deposit", "reward"):
            for a, q in got.items():
                v = self.value(a, q, t)
                if v is None:
                    self.warnings.append("%s %s: no EUR price for %s, counted at 0" % (iso(t), e["key"], a))
                self.acquire(a, q, v or 0.0)
                if e["type"] == "deposit" and a not in FIAT:
                    self.deposits_in.append((t, a, q, v, e.get("txid"), e["key"].split(":")[0]))
        elif e["type"] == "swap":  # the same coins under another name: the cost follows, no gain
            cost = sum(self.remove(a, q, e) for a, q in gave.items())
            for a, q in got.items():
                self.acquire(a, q, cost / len(got))
        elif e["type"] == "withdrawal":
            for a, q in gave.items():
                cost = self.remove(a, q, e)
                if e["key"] in self.links:
                    self.in_transit[self.links[e["key"]]] = dict(asset=a, sent=q, cost=cost, left=t,
                                                                 txid=e.get("txid"), frm=e["key"].split(":")[0])
                elif a not in FIAT:
                    self.transfers_out.append((t, a, q, cost, e.get("txid"), e["key"].split(":")[0]))
        elif self.method == "carry" and e.get("cost_eur") is None and gave and not (set(legs) & FIAT):
            # crypto for crypto: the cost of what was given (fees included) goes to what was received
            cost = sum(self.remove(a, q, e) for a, q in gave.items())
            weights = {a: self.value(a, q, t) or 0.0 for a, q in got.items()}
            wsum = sum(weights.values())
            for a, q in got.items():
                self.acquire(a, q, cost * (weights[a] / wsum if wsum else 1.0 / len(got)))
        else:  # trade: what was given is worth what was received, EUR on either side gives the exact value
            total = e.get("cost_eur")
            for side in ((gave, got) if total is None else ()):
                if "EUR" in side:
                    total = float(side["EUR"])
                    break
            for side in ((gave, got) if total is None else ()):
                vals = [self.value(a, q, t) for a, q in side.items()]
                if vals and None not in vals:
                    total = sum(vals)
                    break
            if total is None:
                self.warnings.append("%s %s: no EUR value, counted at 0" % (iso(t), e["key"]))
                total = 0.0
            for side, gives in ((gave, True), (got, False)):
                weights = {a: (float(q) if a in FIAT else (self.value(a, q, t) or 0.0)) for a, q in side.items()}
                wsum = sum(weights.values())
                for a, q in side.items():
                    part = total * (weights[a] / wsum if wsum else 1.0 / len(side))
                    if gives:
                        cost = self.remove(a, q, e)
                        if a not in FIAT:
                            self.realized += part - cost
                    else:
                        self.acquire(a, q, part)

    def replay(self, events):
        self.links = match_transfers(events)
        self.linked_in = set(self.links.values())
        for e in sorted(events, key=lambda e: (e["time"], e["key"])):
            self.apply(e)
        self.pricer.save()


# --------------------------------------------------------------------------- commands

EXCHANGES = ("binance", "kraken")
EVENTS = {"binance": binance_events, "kraken": kraken_events}


def keys(cfg, name):
    key, secret = cfg.get(name.upper() + "_API_KEY"), cfg.get(name.upper() + "_API_SECRET")
    return (key, secret) if key and secret else None


def cmd_sync(cfg):
    names = [n for n in EXCHANGES if keys(cfg, n)]
    if not names:
        sys.exit("no exchange key: set BINANCE_API_KEY/SECRET and/or KRAKEN_API_KEY/SECRET (read-only) in %s" % ENV_FILE)
    archives = {}
    for name in names:
        arc = Archive(name)
        before = len(arc.records)
        if name == "binance":
            BinanceCollector(Binance(*keys(cfg, name)), arc, cfg.get("CRYPTO_LEDGER_BINANCE_START", BINANCE_START)).sync()
        else:
            KrakenCollector(Kraken(*keys(cfg, name)), arc).sync()
        print("%s: %d new movement(s) %s, %d archived" % (
            name, len(arc.records) - before, json.dumps(arc.added, sort_keys=True) if arc.added else "",
            len(arc.records)))
        archives[name] = arc
    return archives


def live_holdings(cfg, name):
    if not keys(cfg, name):
        return None
    try:
        if name == "binance":
            return binance_holdings(Binance(*keys(cfg, name)))
        return kraken_holdings(Kraken(*keys(cfg, name)))
    except Exception as e:
        print("warning: live balances of %s not read: %s" % (name, e))
        return None


def all_events():
    events = []
    for name in EXCHANGES:
        arc = Archive(name)
        if arc.records:
            events.append((name, EVENTS[name](arc)))
    return events


def cmd_summary(cfg):
    per_exchange = all_events()
    if not per_exchange:
        sys.exit("empty archive: run crypto-ledger.py sync first")
    pricer = Pricer(Binance())
    method = cfg.get("CRYPTO_LEDGER_METHOD", "carry")
    for name, events in per_exchange:
        book = Book(pricer, method)
        book.replay(events)
        live = live_holdings(cfg, name)
        print("%s: replayed quantities against the live balances" % name)
        print("  %-8s %20s %20s %14s" % ("asset", "replayed", "live", "difference"))
        for a in sorted(set(book.qty) | set(live or {})):
            q, l = book.qty.get(a, Decimal(0)), (live or {}).get(a, Decimal(0))
            if q or l:
                diff = "-" if live is None else ("ok" if abs(q - l) < Decimal("1e-8") else str((q - l).normalize()))
                print("  %-8s %20s %20s %14s" % (a, q.normalize(), "-" if live is None else l.normalize(), diff))

    book = Book(pricer, method)
    book.replay([e for _, events in per_exchange for e in events])
    if book.transfers:
        print("transfers between exchanges (the cost follows the coins): %d" % len(book.transfers))
        for x in book.transfers:
            print("  %s  %s -> %s  %s %s sent, %s received  cost %.2f EUR" % (
                iso(x["left"]), x["frm"], x["to"], x["sent"].normalize(), x["asset"], x["received"].normalize(),
                x["cost"]))
    # value at the close of the last complete hour
    now = (int(time.time() * 1000) // HOUR - 1) * HOUR
    total_cost = total_value = 0.0
    print("all exchanges, valued at %s UTC, cost method %s" % (iso(now), method))
    print("  %-8s %18s %12s %12s %12s %12s %12s" % ("asset", "quantity", "cost EUR", "avg cost", "price",
                                                    "value EUR", "latent EUR"))
    for a in sorted(book.qty):
        q, cost = book.qty[a], book.cost.get(a, 0.0)
        if q <= 0 or a in FIAT:
            continue
        price = pricer.eur(a, now)
        value = None if price is None else float(q) * price
        total_cost += cost
        total_value += value or 0.0
        print("  %-8s %18s %12.2f %12.2f %12s %12s %12s" % (
            a, q.normalize(), cost, cost / float(q), "?" if price is None else "%.2f" % price,
            "?" if value is None else "%.2f" % value, "?" if value is None else "%+.2f" % (value - cost)))
    pricer.save()
    print("  %-8s %18s %12.2f %12s %12s %12.2f %+12.2f" % ("total", "", total_cost, "", "", total_value,
                                                         total_value - total_cost))
    if total_cost:
        print("latent gain: %+.2f EUR (%+.1f %%)" % (total_value - total_cost, 100 * (total_value - total_cost) / total_cost))
    print("realized gain: %+.2f EUR (indicative; %s)" % (book.realized, "sales for EUR only" if method == "carry"
                                                         else "every trade"))
    for a, q in sorted(book.qty.items()):
        if q < 0 and a not in FIAT:
            print("  negative balance: %s %s (a movement the API does not list)" % (q.normalize(), a))
    for title, rows in (("deposits from outside (cost = market price that day)", book.deposits_in),
                        ("withdrawals to outside (cost leaving with them)", book.transfers_out)):
        if rows:
            print("%s: %d" % (title, len(rows)))
            for t, a, q, v, txid, ex in rows[-10:]:
                print("  %s  %-8s %s %s  %s EUR  %s" % (iso(t), ex, q.normalize(), a,
                                                        "?" if v is None else "%.2f" % v, (txid or "")[:20]))
    if book.warnings:
        print("warnings: %d" % len(book.warnings))
        for w in book.warnings[:20]:
            print("  " + w)


EXPORT_FILE = "positions.json"


def cmd_export(cfg, quiet=False):
    """Writes positions.json for the Sure bridge: the holdings today and, day by day, the net change of quantity and
    cost of every asset (transfers between exchanges net to nothing but their fee)."""
    per_exchange = all_events()
    if not per_exchange:
        sys.exit("empty archive: run crypto-ledger.py sync first")
    method = cfg.get("CRYPTO_LEDGER_METHOD", "carry")
    book = Book(Pricer(Binance()), method)
    book.replay([e for _, events in per_exchange for e in events])
    days = []
    for day in sorted(book.days):
        changes = {a: {"qty": format(q.normalize(), "f"), "cost": round(c, 8)}
                   for a, (q, c) in sorted(book.days[day].items()) if q or abs(c) > 1e-9}
        if changes:
            days.append({"date": day, "changes": changes})
    out = {
        "date": time.strftime("%Y-%m-%d", time.gmtime()),
        "method": method,
        "assets": {a: {"qty": format(q.normalize(), "f"), "cost": round(book.cost.get(a, 0.0), 8)}
                   for a, q in sorted(book.qty.items()) if q > 0 and a not in FIAT},
        "days": days,
    }
    write_json(os.path.join(DATA_DIR, EXPORT_FILE), out)
    if not quiet:
        print("export: %d asset(s), %d day(s) of changes (%s .. %s), method %s" % (
            len(out["assets"]), len(days), days[0]["date"] if days else "-", days[-1]["date"] if days else "-", method))
    return out


def cmd_events(cfg, asset=None):
    events = [e for _, evs in all_events() for e in evs]
    for e in sorted(events, key=lambda e: (e["time"], e["key"])):
        if asset and asset.upper() not in e["legs"]:
            continue
        legs = " ".join("%+f %s" % (q, a) for a, q in sorted(e["legs"].items()))
        print("%s  %-10s %s  %s" % (iso(e["time"]), e["type"], legs, e["key"]))


def main(argv=None):
    ap = argparse.ArgumentParser(description="Crypto exchanges ledger")
    ap.add_argument("command", choices=["sync", "summary", "events", "export"])
    ap.add_argument("asset", nargs="?", help="events: only this asset")
    args = ap.parse_args(argv)
    cfg = load_env()
    if args.command == "sync":
        cmd_sync(cfg)
        cmd_export(cfg)
    elif args.command == "summary":
        cmd_summary(cfg)
    elif args.command == "export":
        cmd_export(cfg)
    else:
        cmd_events(cfg, args.asset)
    return 0


if __name__ == "__main__":
    sys.exit(main())
