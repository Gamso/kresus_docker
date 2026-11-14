"""Tests of the daily balance of the savings account: a valuation is sent only when the balance changed.

    python -m unittest discover -s sure-bridge
"""
import datetime
import importlib.util
import os
import shutil
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))


class BalanceSyncTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        env = os.path.join(self.dir, "bridge.env")
        with open(env, "w") as f:
            f.write("SURE_URL=http://127.0.0.1:1\nSURE_API_KEY=x\n")
        os.environ["SURE_BRIDGE_ENV"] = env
        os.environ["SURE_BRIDGE_STATE"] = os.path.join(self.dir, "state.json")
        spec = importlib.util.spec_from_file_location("sure_bridge", os.path.join(HERE, "sure-bridge.py"))
        self.m = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.m)
        self.balance = 10650.59
        self.m.kresus_all = lambda: {}
        self.m.find_kresus_account = lambda data, key, default: {"id": 1, "balance": self.balance}
        self.today = datetime.date(2026, 11, 10)

        today = self

        class FakeDate(datetime.date):
            @classmethod
            def today(cls):
                return today.today

        self.m.date = FakeDate

    def tearDown(self):
        shutil.rmtree(self.dir)

    def sync(self):
        """One daily run; returns the valuations sent to Sure."""
        sent = []
        bridge = self.m.Bridge(False)
        bridge.post = lambda path, body, label: sent.append(body["valuation"])
        bridge.sync_savings("S1")
        return sent

    def test_first_run_sends_the_balance(self):
        self.assertEqual(self.sync(), [{"account_id": "S1", "amount": 10650.59, "date": "2026-11-10"}])

    def test_same_balance_the_next_days_sends_nothing(self):
        self.sync()
        for _ in range(3):
            self.today += datetime.timedelta(days=1)
            self.assertEqual(self.sync(), [])

    def test_changed_balance_is_sent_once(self):
        self.sync()
        self.today += datetime.timedelta(days=1)
        self.balance = 10700.0
        self.assertEqual(self.sync(), [{"account_id": "S1", "amount": 10700.0, "date": "2026-11-11"}])
        self.today += datetime.timedelta(days=1)
        self.assertEqual(self.sync(), [])

    def test_returning_to_a_previous_balance_is_sent_again(self):
        self.sync()
        self.today += datetime.timedelta(days=1)
        self.balance = 10700.0
        self.sync()
        self.today += datetime.timedelta(days=1)
        self.balance = 10650.59
        self.assertEqual(len(self.sync()), 1)

    def test_existing_state_of_the_old_version_is_understood(self):
        # state written by the previous version: last = [date, balance]
        bridge = self.m.Bridge(False)
        bridge.state["savings"] = {"last": ["2026-11-09", 10650.59]}
        bridge.commit()
        self.assertEqual(self.sync(), [])


if __name__ == "__main__":
    unittest.main()
