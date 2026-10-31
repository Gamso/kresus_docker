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
        self.fail_at = None

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
                trade = json.loads(self.rfile.read(int(self.headers["Content-Length"])))["trade"]
                if sure.fail_at is not None and len(sure.trades) == sure.fail_at:
                    sure.fail_at = None
                    data = b'{"error":"validation_failed"}'
                    self.send_response(422)
                    self.send_header("Content-Type", "application/json")
                    self.end_headers()
                    self.wfile.write(data)
                    return
                if trade["type"] in ("buy", "sell") and not trade.get("price", 0) > 0:
                    self.send_response(422)
                    self.end_headers()
                    return
                sure.trades.append(trade)
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

    def test_coins_that_cost_nothing(self):
        # airdropped tokens (cost 0) exchanged for BTC: a positive price all the same, Sure refuses 0
        DAYS.insert(6, day("2025-01-07", BTC=("0.00000278", 0.0)))
        try:
            self.write_export("2025-02-01")
            trades = self.run_sync()
        finally:
            DAYS.pop(6)
        buy = [t for t in self.sure.trades if t["date"] == "2025-01-07"][0]
        self.assertEqual((buy["type"], buy["qty"], buy["price"]), ("buy", 2.78e-06, 0.000001))
        self.assertIn(("buy", 2.78e-06, None, "2025-01-07"), trades)

    def test_resumes_after_a_failure_without_sending_twice(self):
        # Sure refuses the 4th trade: the buy of the reward of 2025-01-02, after the deposit of that same day
        self.sure.fail_at = 3
        with self.assertRaises(RuntimeError):
            self.run_sync()
        first = list(self.sure.trades)
        self.assertEqual([(t["type"], t["date"]) for t in first],
                         [("deposit", "2025-01-01"), ("buy", "2025-01-01"), ("deposit", "2025-01-02")])
        self.sure.trades = first  # keep what Sure accepted
        bridge = self.m.Bridge(False)
        bridge.sync_crypto("A1")
        types = [(t["type"], t["date"]) for t in self.sure.trades]
        # the deposit of 2025-01-02 is not sent again, the buy that failed is; then the rest of the history
        self.assertEqual(types.count(("deposit", "2025-01-02")), 1)
        self.assertEqual(types.count(("buy", "2025-01-02")), 1)
        self.assertEqual(len(self.sure.trades), 9)
        state = json.load(open(os.environ["SURE_BRIDGE_STATE"]))["crypto"]
        self.assertNotIn("progress", state)
        self.assertAlmostEqual(state["held"]["BTC"], 0.0109851, places=9)

    def test_first_send_needs_an_empty_account(self):
        self.sure.holdings = [{"security": {"ticker": "CRYPTO:BTC", "id": "S1"}, "qty": "0.01"}]
        with self.assertRaises(RuntimeError):
            self.run_sync()
        self.assertEqual(self.sure.trades, [])


if __name__ == "__main__":
    unittest.main()
