"""Tests of the crypto part of sure-bridge.py against a fake Sure API.

    python -m unittest discover -s sure-bridge
"""
import importlib.util
import json
import os
import shutil
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))


def day(date, **changes):
    return {"date": date, "changes": {a: {"qty": q, "cost": c} for a, (q, c) in changes.items()}}


# 500 EUR of BTC, a tiny reward, 100 EUR of ETH staked for WBETH then exchanged for BTC, a transfer fee, and a buy today
DAYS = [
    day("2025-01-01", BTC=("0.01", 500.0)),
    day("2025-01-02", BTC=("0.0000001", 0.01)),
    day("2025-01-03", ETH=("0.03", 100.0)),
    day("2025-01-04", ETH=("-0.03", -100.0), WBETH=("0.028", 100.0)),
    day("2025-01-05", WBETH=("-0.028", -100.0), BTC=("0.001", 100.0)),
    day("2025-01-06", BTC=("-0.000015", 0.0)),
    day("2025-02-01", BTC=("0.002", 150.0)),
]


class Sure:
    def __init__(self):
        self.trades, self.holdings, self.other_holdings = [], [], []

    def handler(self):
        sure = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def reply(self, body):
                data = json.dumps(body).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(data)

            def do_GET(self):
                if self.path.startswith("/api/v1/accounts"):
                    return self.reply({"accounts": [{"id": "A1", "name": "Crypto", "account_type": "crypto"},
                                                    {"id": "A2", "name": "Kraken", "account_type": "crypto"}],
                                       "pagination": {"total_pages": 1}})
                rows = sure.holdings if "account_id=A1" in self.path else sure.other_holdings
                return self.reply({"holdings": rows, "pagination": {"total_pages": 1}})

            def do_POST(self):
                sure.trades.append(json.loads(self.rfile.read(int(self.headers["Content-Length"])))["trade"])
                self.reply({})

        return H


class CryptoSyncTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.sure = Sure()
        self.srv = HTTPServer(("127.0.0.1", 0), self.sure.handler())
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        env = os.path.join(self.dir, "bridge.env")
        with open(env, "w") as f:
            f.write("SURE_URL=http://127.0.0.1:%d\nSURE_API_KEY=x\nSURE_ACCOUNT_CRYPTO=Crypto\n" % self.srv.server_port)
        os.environ["SURE_BRIDGE_ENV"] = env
        os.environ["SURE_BRIDGE_STATE"] = os.path.join(self.dir, "state.json")
        os.environ["CRYPTO_LEDGER_EXPORT"] = self.export = os.path.join(self.dir, "positions.json")
        self.write_export("2025-02-01")
        spec = importlib.util.spec_from_file_location("sure_bridge", os.path.join(HERE, "sure-bridge.py"))
        self.m = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.m)

    def tearDown(self):
        self.srv.shutdown()
        self.srv.server_close()
        shutil.rmtree(self.dir)

    def set_minimum(self, eur):
        self.m.CFG["CRYPTO_MIN_EUR"] = str(eur)

    def write_export(self, today):
        with open(self.export, "w") as f:
            json.dump({"date": today, "method": "carry", "assets": {"BTC": {"qty": "0.0129851", "cost": 750.01}},
                       "days": DAYS}, f)

    def run_sync(self, dry=False):
        self.sure.trades.clear()
        bridge = self.m.Bridge(dry)
        bridge.sync_crypto("A1")
        return [(t["type"], t.get("qty"), t.get("amount"), t["date"]) for t in self.sure.trades]

    def test_every_day_by_default(self):
        trades = self.run_sync()
        # the 0.01 EUR reward of 2025-01-02 is a buy on its date
        self.assertIn(("buy", 1e-07, None, "2025-01-02"), trades)
        self.assertEqual(sum(1 for t in trades if t[0] == "buy"), 4)

    def test_history_replayed_with_real_dates(self):
        self.set_minimum(1)
        self.assertEqual(self.run_sync(dry=True), [])  # a dry run sends nothing
        trades = self.run_sync()
        self.assertEqual(trades, [
            ("deposit", None, 500.0, "2025-01-01"),
            ("buy", 0.01, None, "2025-01-01"),
            # 2025-01-02: the 0.01 EUR reward waits; it rides along with the next deposit
            ("deposit", None, 100.01, "2025-01-03"),
            ("buy", 0.03, None, "2025-01-03"),
            # 2025-01-04: ETH staked for WBETH (not sent): its cost becomes cash, no deposit
            ("sell", 0.03, None, "2025-01-04"),
            # 2025-01-05: WBETH exchanged for BTC, which gets its 100 EUR (and the pending reward)
            ("buy", 0.0010001, None, "2025-01-05"),
            ("sell", 0.000015, None, "2025-01-06"),  # transfer fee: coins gone, cost kept
        ])
        btc = [t for t in self.sure.trades if t["type"] == "buy" and t["date"] == "2025-01-05"][0]
        self.assertAlmostEqual(btc["qty"] * btc["price"], 100.01, places=2)
        self.assertEqual(btc["ticker"], "CRYPTO:BTC")

        self.assertEqual(self.run_sync(), [])  # today (2025-02-01) waits until it is complete
        self.write_export("2025-02-02")
        self.assertEqual(self.run_sync(), [("deposit", None, 150.0, "2025-02-01"), ("buy", 0.002, None, "2025-02-01")])

    def test_reuses_the_crypto_securities_of_sure(self):
        # another account (synced by CoinStats) holds CRYPTO:BTC: its security is reused, its price with it
        self.sure.other_holdings = [{"security": {"ticker": "CRYPTO:BTC", "id": "S-BTC"}, "qty": "0.04"}]
        self.run_sync()
        btc = [t for t in self.sure.trades if t["type"] == "buy" and t["date"] == "2025-01-01"][0]
        self.assertEqual(btc.get("security_id"), "S-BTC")
        self.assertNotIn("ticker", btc)
        eth = [t for t in self.sure.trades if t["type"] == "buy" and t["date"] == "2025-01-03"][0]
        self.assertEqual(eth.get("ticker"), "CRYPTO:ETH")  # not held anywhere: Sure resolves the ticker

    def test_first_send_needs_an_empty_account(self):
        self.sure.holdings = [{"security": {"ticker": "CRYPTO:BTC", "id": "S1"}, "qty": "0.01"}]
        with self.assertRaises(RuntimeError):
            self.run_sync()
        self.assertEqual(self.sure.trades, [])


if __name__ == "__main__":
    unittest.main()
