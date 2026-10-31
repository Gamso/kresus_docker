"""Tests of crypto-ledger.py against a fake Binance API.

    python -m unittest discover -s crypto-ledger
"""
import base64
import contextlib
import hashlib
import hmac
import io
import importlib.util
import json
import os
import shutil
import tempfile
import threading
import unittest
import urllib.parse
from datetime import datetime, timezone
from decimal import Decimal
from http.server import BaseHTTPRequestHandler, HTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
KEY, SECRET = "key", "secret"


def t(day, hour=12):
    return int(datetime.strptime(day, "%Y-%m-%d").replace(hour=hour, tzinfo=timezone.utc).timestamp() * 1000)


class Fake:
    """State of the fake exchange; every list is filtered on the time window of the request."""

    def __init__(self):
        self.deposits = [{"id": "d0", "coin": "BTC", "amount": "0.002", "status": 0, "insertTime": t("2024-05-01"),
                          "txId": "txin"}]
        self.withdrawals = [{"id": "w1", "coin": "BTC", "amount": "0.005", "transactionFee": "0.0001", "status": 6,
                             "applyTime": "2024-03-01 12:00:00", "txId": "txout"}]
        self.fiat_orders = [{"orderNo": "f1", "fiatCurrency": "EUR", "amount": "1000", "indicatedAmount": "1001", "totalFee": "1",
                             "status": "Successful", "createTime": t("2024-01-10")}]
        self.trades = {"BTCEUR": [
            {"id": 1, "isBuyer": True, "qty": "0.01", "quoteQty": "500", "commission": "0.00001",
             "commissionAsset": "BTC", "time": t("2024-01-11")},
            {"id": 2, "isBuyer": True, "qty": "0.01", "quoteQty": "400", "commission": "0.5",
             "commissionAsset": "EUR", "time": t("2024-02-01")}]}
        self.dividends = [{"id": 7, "asset": "BNB", "amount": "0.1", "divTime": t("2024-03-05"), "enInfo": "Earn"}]
        self.converts = [{"orderId": 9, "fromAsset": "BNB", "fromAmount": "0.05", "toAsset": "USDT", "toAmount": "15",
                          "createTime": t("2024-04-01"), "orderStatus": "SUCCESS"}]
        # bought with the EUR balance, then staked for WBETH
        self.payments = [{"orderNo": "p1", "cryptoCurrency": "ETH", "fiatCurrency": "EUR", "obtainAmount": "0.03",
                          "sourceAmount": "90", "paymentMethod": "Cash Balance", "status": "Completed",
                          "totalFee": "0", "createTime": t("2024-01-12")}]
        self.eth_stakes = [{"time": t("2024-01-13"), "asset": "ETH", "amount": "0.03", "status": "SUCCESS",
                            "distributeAsset": "WBETH", "distributeAmount": "0.028"}]
        self.realtime = [{"asset": "BNB", "rewards": "0.001", "productId": "BNB001", "type": "REALTIME",
                          "time": t("2024-03-06")}]
        self.balances = {"BTC": "0.01489", "BNB": "0.051", "USDT": "15", "EUR": "9.5", "WBETH": "0.028"}
        self.prices = {"BNBEUR": 300.0, "EURUSDT": 1.1, "BTCEUR": 40000.0}
        self.calls = []


def handler(fake):
    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def reply(self, body, code=200):
            data = json.dumps(body).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(data)

        def handle_any(self):
            url = urllib.parse.urlparse(self.path)
            q = dict(urllib.parse.parse_qsl(url.query))
            fake.calls.append(url.path)
            public = url.path in ("/api/v3/exchangeInfo", "/api/v3/klines")
            if not public:
                unsigned = url.query.rsplit("&signature=", 1)[0]
                good = hmac.new(SECRET.encode(), unsigned.encode(), hashlib.sha256).hexdigest()
                if self.headers.get("X-MBX-APIKEY") != KEY or q.get("signature") != good:
                    return self.reply({"code": -1022, "msg": "Signature for this request is not valid."}, 400)

            def window(rows, field, conv=lambda v: v):
                lo, hi = int(q.get("startTime") or q.get("beginTime")), int(q.get("endTime"))
                return [r for r in rows if lo <= conv(r[field]) <= hi]

            def applytime(v):
                return int(datetime.strptime(v, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc).timestamp() * 1000)

            p = url.path
            if p == "/sapi/v1/capital/deposit/hisrec":
                return self.reply(window(fake.deposits, "insertTime"))
            if p == "/sapi/v1/capital/withdraw/history":
                return self.reply(window(fake.withdrawals, "applyTime", applytime))
            if p == "/sapi/v1/fiat/orders":
                rows = fake.fiat_orders if q["transactionType"] == "0" else []
                return self.reply({"data": window(rows, "createTime")})
            if p == "/sapi/v1/fiat/payments":
                rows = fake.payments if q["transactionType"] == "0" else []
                return self.reply({"data": window(rows, "createTime")})
            if p == "/sapi/v1/eth-staking/eth/history/stakingHistory":
                return self.reply({"rows": window(fake.eth_stakes, "time")})
            if p.startswith("/sapi/v1/eth-staking/"):
                return self.reply({"rows": []})
            if p == "/sapi/v1/simple-earn/flexible/history/rewardsRecord":
                return self.reply({"rows": window(fake.realtime, "time") if q["type"] == "REALTIME" else []})
            if p == "/sapi/v1/convert/tradeFlow":
                return self.reply({"list": window(fake.converts, "createTime")})
            if p == "/sapi/v1/asset/dribblet":
                return self.reply({"userAssetDribblets": []})
            if p == "/sapi/v1/asset/assetDividend":
                return self.reply({"rows": window(fake.dividends, "divTime")})
            if p == "/api/v3/exchangeInfo":
                return self.reply({"symbols": [
                    {"symbol": "BTCEUR", "baseAsset": "BTC", "quoteAsset": "EUR"},
                    {"symbol": "BNBEUR", "baseAsset": "BNB", "quoteAsset": "EUR"},
                    {"symbol": "ETHBTC", "baseAsset": "ETH", "quoteAsset": "BTC"},
                    {"symbol": "DOGEEUR", "baseAsset": "DOGE", "quoteAsset": "EUR"}]})
            if p == "/api/v3/myTrades":
                rows = [r for r in fake.trades.get(q["symbol"], []) if r["id"] >= int(q["fromId"])]
                return self.reply(rows)
            if p == "/api/v3/account":
                return self.reply({"balances": [{"asset": a, "free": v, "locked": "0"} for a, v in fake.balances.items()]})
            if p == "/sapi/v1/asset/get-funding-asset":
                return self.reply([])
            if p in ("/sapi/v1/simple-earn/flexible/position", "/sapi/v1/simple-earn/locked/position"):
                return self.reply({"rows": []})
            if p == "/api/v3/klines":
                price = fake.prices.get(q["symbol"])
                if price is None:
                    return self.reply({"code": -1121, "msg": "Invalid symbol."}, 400)
                start = int(q["startTime"])
                return self.reply([[start, "0", "0", "0", str(price), "0", start + 3599999]])
            return self.reply({"msg": "unknown " + p}, 404)

        do_GET = do_POST = handle_any

    return H


KKEY, KSECRET = "kkey", base64.b64encode(b"kraken secret").decode()


def kraken_handler(ledger, balances):
    """Fake Kraken private API: checks the signature, serves the ledger newest first, 2 entries per page."""

    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def reply(self, result=None, error=()):
            data = json.dumps({"error": list(error), "result": result}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(data)

        def do_POST(self):
            post = self.rfile.read(int(self.headers["Content-Length"])).decode()
            q = dict(urllib.parse.parse_qsl(post))
            digest = self.path.encode() + hashlib.sha256((q["nonce"] + post).encode()).digest()
            good = base64.b64encode(hmac.new(base64.b64decode(KSECRET), digest, hashlib.sha512).digest()).decode()
            if self.headers.get("API-Key") != KKEY or self.headers.get("API-Sign") != good:
                return self.reply(error=["EAPI:Invalid signature"])
            if self.path == "/0/private/Balance":
                return self.reply(balances)
            if self.path == "/0/private/Ledgers":
                rows = sorted(((k, v) for k, v in ledger.items() if v["time"] > float(q.get("start", 0))),
                              key=lambda kv: -kv[1]["time"])
                ofs = int(q.get("ofs", 0))
                return self.reply({"ledger": dict(rows[ofs:ofs + 2]), "count": len(rows)})
            return self.reply(error=["EGeneral:Unknown method"])

    return H


def entry(refid, day, kind, asset, amount, fee="0", subtype=""):
    return {"refid": refid, "time": t(day) / 1000, "type": kind, "subtype": subtype, "aclass": "currency",
            "asset": asset, "amount": amount, "fee": fee, "balance": "0"}


class LedgerTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        os.environ["CRYPTO_LEDGER_DIR"] = self.dir
        os.environ["CRYPTO_LEDGER_ENV"] = os.path.join(self.dir, "none.env")
        os.environ["BINANCE_API_KEY"], os.environ["BINANCE_API_SECRET"] = KEY, SECRET
        os.environ["CRYPTO_LEDGER_BINANCE_START"] = "2024-01-01"
        for name in ("KRAKEN_API_KEY", "KRAKEN_API_SECRET"):
            os.environ.pop(name, None)
        spec = importlib.util.spec_from_file_location("crypto_ledger", os.path.join(HERE, "crypto-ledger.py"))
        self.m = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.m)
        self.fake = Fake()
        self.srv = HTTPServer(("127.0.0.1", 0), handler(self.fake))
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        self.m.Binance.BASE = "http://127.0.0.1:%d" % self.srv.server_port
        self.m.Binance.__init__.__defaults__ = (None, None, 0)
        self.m.Kraken.BASE = "http://127.0.0.1:9"  # never the real API, even by mistake

    def tearDown(self):
        self.srv.shutdown()
        self.srv.server_close()
        shutil.rmtree(self.dir)

    def sync(self):
        return self.m.cmd_sync(self.m.load_env())["binance"]

    def test_sync_archives_once_and_waits_for_pending(self):
        arc = self.sync()
        kinds = sorted(r["kind"] for r in arc.records.values())
        self.assertEqual(kinds, ["card_buy", "convert", "dividend", "earn_realtime", "eth_stake", "fiat_deposit",
                                 "trade", "trade", "withdrawal"])
        # the pending deposit holds the deposits cursor before it
        self.assertLess(arc.cursors["deposits"], t("2024-05-01"))
        self.assertEqual(arc.cursors["trades:BTCEUR"], 2)
        self.assertNotIn("trades:DOGEEUR", arc.cursors)  # DOGE never seen in the account: pair not queried

        again = self.sync()
        self.assertEqual(again.added, {})
        self.assertEqual(len(again.records), 9)

        # the deposit becomes final and a new trade happens: only those two are added
        self.fake.deposits[0]["status"] = 1
        self.fake.trades["BTCEUR"].append({"id": 3, "isBuyer": False, "qty": "0.001", "quoteQty": "60",
                                           "commission": "0.06", "commissionAsset": "EUR", "time": t("2024-06-01")})
        third = self.sync()
        self.assertEqual(third.added, {"deposit": 1, "trade": 1})
        with open(os.path.join(self.dir, "binance.jsonl")) as f:
            self.assertEqual(len(f.readlines()), 11)

    def test_replay_quantities_and_average_cost(self):
        arc = self.sync()
        book = self.m.Book(self.m.Pricer(self.m.Binance()))
        book.replay(self.m.binance_events(arc))
        self.assertEqual(book.qty["BTC"], Decimal("0.01489"))
        self.assertEqual(book.qty["EUR"], Decimal("9.5"))
        self.assertEqual(book.qty["BNB"], Decimal("0.051"))
        self.assertEqual(book.qty["ETH"], Decimal("0"))
        self.assertEqual(book.qty["WBETH"], Decimal("0.028"))
        self.assertAlmostEqual(book.cost["WBETH"], 90.0, places=6)  # the staking carries the cost of the ETH
        self.assertEqual(book.qty["USDT"], Decimal("15"))
        # BTC: 0.00999 for 500 EUR, then 0.01 for 400.5 EUR (fee in EUR included), then 0.0051 left at average cost
        self.assertAlmostEqual(book.cost["BTC"], 900.5 * 0.01489 / 0.01999, places=6)
        self.assertAlmostEqual(book.transfers_out[0][3], 900.5 * 0.0051 / 0.01999, places=6)
        # BNB rewards at 300 EUR (0.101 -> 30.30 EUR), 0.05 converted to 15 USDT valued 15 EUR: no gain
        self.assertAlmostEqual(book.cost["BNB"], 15.3, places=6)
        self.assertAlmostEqual(book.cost["USDT"], 15.0, places=6)
        self.assertAlmostEqual(book.realized, 0.0, places=6)
        self.assertEqual(book.warnings, [])
        live = self.m.binance_holdings(self.m.Binance(KEY, SECRET))
        for asset, qty in book.qty.items():
            self.assertEqual(qty, live.get(asset, Decimal(0)), asset)

    def kraken(self, ledger, balances):
        self.kraken_ledger = ledger
        srv = HTTPServer(("127.0.0.1", 0), kraken_handler(ledger, balances))
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        self.addCleanup(srv.server_close)
        self.addCleanup(srv.shutdown)
        self.m.Kraken.BASE = "http://127.0.0.1:%d" % srv.server_port
        self.m.Kraken.__init__.__defaults__ = (None, None, 0)
        os.environ["KRAKEN_API_KEY"], os.environ["KRAKEN_API_SECRET"] = KKEY, KSECRET

    def test_kraken_ledger(self):
        del os.environ["BINANCE_API_KEY"]
        ledger = {
            "L1": entry("D1", "2024-01-10", "deposit", "ZEUR", "100.0000"),
            "L2": entry("T1", "2024-01-11", "trade", "ZEUR", "-50.0000", "0.1300"),
            "L3": entry("T1", "2024-01-11", "trade", "XXBT", "0.0010000000"),
            # moving coins to Earn: two entries, same refid, nothing changes once the suffix is dropped
            "L4": entry("A1", "2024-01-12", "earn", "XXBT", "-0.0005000000", subtype="allocation"),
            "L5": entry("A1", "2024-01-12", "earn", "XBT.M", "0.0005000000", subtype="allocation"),
            "L6": entry("R1", "2024-02-01", "earn", "XBT.M", "0.0000100000", subtype="reward"),
            "L7": entry("W1", "2024-03-01", "withdrawal", "XXBT", "-0.0002000000", "0.0000100000"),
        }
        balances = {"ZEUR": "49.8700", "XXBT": "0.0002900000", "XBT.M": "0.0005100000"}
        self.kraken(ledger, balances)
        arc = self.m.cmd_sync(self.m.load_env())["kraken"]
        self.assertEqual(len(arc.records), 7)
        self.assertEqual(self.m.cmd_sync(self.m.load_env())["kraken"].added, {})

        events = {e["key"]: e for e in self.m.kraken_events(arc)}
        self.assertNotIn("kraken:A1", events)  # the Earn allocation nets to nothing
        self.assertEqual(events["kraken:T1"]["type"], "trade")
        self.assertEqual(events["kraken:T1"]["legs"], {"EUR": Decimal("-50.13"), "BTC": Decimal("0.001")})
        self.assertEqual(events["kraken:R1"]["type"], "reward")
        self.assertEqual(events["kraken:D1"]["type"], "fiat")

        book = self.m.Book(self.m.Pricer(self.m.Binance()))
        book.replay(list(events.values()))
        live = self.m.kraken_holdings(self.m.Kraken(KKEY, KSECRET))
        self.assertEqual(live, {"EUR": Decimal("49.87"), "BTC": Decimal("0.0008")})
        self.assertEqual({a: q for a, q in book.qty.items() if q}, live)
        # 0.001 BTC for 50.13 EUR, + 0.00001 reward at 40000 EUR (0.40 EUR), - 0.00021 at the average cost
        self.assertAlmostEqual(book.cost["BTC"], 50.53 * 0.0008 / 0.00101, places=6)

        # a new entry after the cursor: only it is fetched and added
        ledger["L8"] = entry("R2", "2024-04-01", "earn", "XBT.M", "0.0000100000", subtype="reward")
        self.assertEqual(self.m.cmd_sync(self.m.load_env())["kraken"].added, {"ledger": 1})

    def test_summary_both_exchanges(self):
        self.kraken({"L1": entry("D1", "2024-01-10", "deposit", "ZEUR", "10.0000")}, {"ZEUR": "10.0000"})
        self.m.cmd_sync(self.m.load_env())
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.m.cmd_summary(self.m.load_env())
        text = out.getvalue()
        self.assertIn("binance: replayed quantities", text)
        self.assertIn("kraken: replayed quantities", text)
        self.assertNotIn("warning", text)
        # Binance: BTC, BNB, USDT, EUR, WBETH match; Kraken: EUR
        self.assertEqual(text.count(" ok"), 6)
        # BTC 0.01489 valued at 40000 EUR
        self.assertRegex(text, r"BTC\s+0\.01489 .* 595\.60")
        self.assertIn("latent gain:", text)

    def test_transfer_carries_the_cost(self):
        # the 0.005 BTC (+ 0.0001 fee) withdrawn from Binance on 2024-03-01 12:00 arrive on Kraken an hour later
        self.kraken({"L1": entry("D9", "2024-03-01", "deposit", "XXBT", "0.0050000000")}, {"XXBT": "0.005"})
        self.kraken_ledger_time("L1", t("2024-03-01", 13))
        self.m.cmd_sync(self.m.load_env())
        events = [e for _, evs in self.m.all_events() for e in evs]
        self.assertEqual(self.m.match_transfers(events), {"binance:withdrawal:w1": "kraken:D9"})
        book = self.m.Book(self.m.Pricer(self.m.Binance()))
        book.replay(events)
        self.assertEqual(book.deposits_in, [])
        self.assertEqual(book.transfers_out, [])
        self.assertEqual(len(book.transfers), 1)
        # nothing left the owner: the whole cost of the BTC bought stays (900.5 EUR), fee included
        self.assertEqual(book.qty["BTC"], Decimal("0.01989"))
        self.assertAlmostEqual(book.cost["BTC"], 900.5, places=6)

    def test_no_transfer_when_amounts_differ(self):
        self.kraken({"L1": entry("D9", "2024-03-01", "deposit", "XXBT", "0.0040000000")}, {"XXBT": "0.004"})
        self.kraken_ledger_time("L1", t("2024-03-01", 13))
        self.m.cmd_sync(self.m.load_env())
        events = [e for _, evs in self.m.all_events() for e in evs]
        self.assertEqual(self.m.match_transfers(events), {})

    def test_methods_on_a_crypto_trade(self):
        # 100 EUR of ETH, exchanged for BTC when it is worth 58 EUR, then half the BTC sold for 40 EUR
        class Prices:
            def eur(self, asset, t):
                return {"ETH": 58 / 0.03, "BTC": 58 / 0.001}.get(asset)

            def save(self):
                pass

        events = [
            dict(time=1, type="trade", legs={"EUR": Decimal("-100"), "ETH": Decimal("0.03")}, key="x:1"),
            dict(time=2, type="trade", legs={"ETH": Decimal("-0.03"), "BTC": Decimal("0.001")}, key="x:2"),
            dict(time=3, type="trade", legs={"BTC": Decimal("-0.0005"), "EUR": Decimal("40")}, key="x:3"),
        ]
        carry = self.m.Book(Prices(), "carry")
        carry.replay(events)
        self.assertAlmostEqual(carry.cost["BTC"], 50.0)  # the 100 EUR follow the coins, half sold
        self.assertAlmostEqual(carry.realized, 40 - 50.0)  # the gain only comes with the sale for EUR
        market = self.m.Book(Prices(), "market")
        market.replay(events)
        self.assertAlmostEqual(market.cost["BTC"], 29.0)  # the BTC starts at its 58 EUR
        self.assertAlmostEqual(market.realized, (58 - 100) + (40 - 29))
        # same total result either way: what is left plus what was realized
        self.assertAlmostEqual(carry.realized - carry.cost["BTC"], market.realized - market.cost["BTC"])
        with self.assertRaises(ValueError):
            self.m.Book(Prices(), "fifo")

    def kraken_ledger_time(self, lid, ms_time):
        self.kraken_ledger[lid]["time"] = ms_time / 1000


if __name__ == "__main__":
    unittest.main()
