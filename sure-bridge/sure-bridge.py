#!/usr/bin/env python3
"""Kresus/Woob -> Sure bridge.

- Bourse Direct PEA : trades (holdings, average cost, cash) from the export of the boursedirect module.
- Esalia PEE        : one global valuation, or one line per fund with micro-buys (PEE_MODE).
- Savings account   : daily balance read from Kresus.
- Checking account  : Kresus transactions (deduplicated) + daily balance.
- SURE_LINK_<n>     : any number of extra savings accounts, checking accounts or loans, from any
                      Kresus bank (La Banque Postale, Fortuneo...).

Usage:
    sure-bridge.py accounts             list the Sure and Kresus accounts
    sure-bridge.py sync --dry-run       show what would be sent
    sure-bridge.py sync                 send
"""
import argparse
import functools
import glob
import html
import json
import os
import sys
import urllib.error
import urllib.request
from datetime import date, timedelta

ENV_FILE = "/etc/sure-bridge.env"
STATE_FILE = "/var/lib/sure-bridge/state.json"
ESALIA_FILE = "/var/lib/kresus/esalia-positions.json"
ESALIA_HISTORY_FILE = "/var/lib/kresus/esalia-history.json"
BD_GLOB = "/var/lib/kresus/boursedirect-positions-*.json"

# ISIN -> Sure security (ticker|exchange|provider), only used when the security is not held yet
TICKERS = {
    "FR0000120073": "AI.PA|XPAR|yahoo_finance",    # Air Liquide
    "FR0011550185": "ESE.PA|XPAR|yahoo_finance",   # BNP Paribas Easy S&P 500
    "FR0010208488": "ENGI.PA|XPAR|yahoo_finance",  # Engie
    "IE0002XZSHO1": "WPEA.PA|XPAR|yahoo_finance",  # iShares MSCI World Swap PEA
    "FR0000120271": "TTE.PA|XPAR|yahoo_finance",   # TotalEnergies
}

MICRO = 1e-9  # micro-buy used only to hand a unit price to Sure

LINK_KINDS = ("savings", "checking", "loan")


# --------------------------------------------------------------------------- helpers

def load_env():
    cfg = {}
    with open(ENV_FILE, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                cfg[k.strip()] = v.strip().strip('"').strip("'")
    return cfg


CFG = load_env()


def http_json(method, url, body=None, headers=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method, headers=headers or {})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            raw = r.read()
            return json.loads(raw) if raw else {}
    except urllib.error.HTTPError as e:
        raise RuntimeError("HTTP %s on %s: %s" % (e.code, url, e.read().decode(errors="replace")[:300]))


def sure(method, path, body=None):
    return http_json(method, CFG["SURE_URL"].rstrip("/") + path, body, {
        "X-Api-Key": CFG["SURE_API_KEY"], "Content-Type": "application/json", "Accept": "application/json"})


def sure_accounts():
    out, page = [], 1
    while True:
        r = sure("GET", "/api/v1/accounts?per_page=100&page=%d" % page)
        out += r.get("accounts", [])
        if page >= ((r.get("pagination") or {}).get("total_pages") or 1):
            return out
        page += 1


def kresus_all():
    return http_json("GET", CFG.get("KRESUS_URL", "http://127.0.0.1:9876").rstrip("/") + "/api/all/")


def walk(o):
    if isinstance(o, dict):
        yield o
        for v in o.values():
            yield from walk(v)
    elif isinstance(o, list):
        for v in o:
            yield from walk(v)


def kresus_accounts(data):
    accounts = [d for d in walk(data) if "vendorAccountId" in d and "label" in d]
    txs = [d for d in walk(data) if "accountId" in d and isinstance(d.get("amount"), (int, float))]
    # Bank (Woob module: bp, fortuneo...) carried by the account, otherwise by its access
    access_vendor = {d.get("id"): d["vendorId"] for d in walk(data)
                     if "vendorId" in d and "vendorAccountId" not in d and "accountId" not in d}
    out = []
    for a in accounts:
        if isinstance(a.get("balance"), (int, float)):
            bal, src = a["balance"], "balance"
        else:
            bal = (a.get("initialBalance") or 0) + sum(t["amount"] for t in txs if t["accountId"] == a.get("id"))
            src = "computed"
        vendor = a.get("vendorId") or access_vendor.get(a.get("accessId")) or "?"
        out.append({"id": a.get("id"), "label": a.get("customLabel") or a["label"], "vendor": vendor,
                    "balance": round(bal, 2), "src": src})
    return out


def match_kresus_account(data, spec, origin):
    """Kresus account designated by "label word" or "bank:label word" (e.g. bp:livret a)."""
    vendor, _, text = spec.strip().lower().rpartition(":")
    found = [a for a in kresus_accounts(data)
             if text.strip() in a["label"].lower() and (not vendor or a["vendor"].lower() == vendor.strip())]
    if len(found) != 1:
        detail = ", ".join("%s:%s" % (a["vendor"], a["label"]) for a in found)
        raise RuntimeError("%d Kresus account(s) for %r (%s)%s" % (
            len(found), spec, origin, ": " + detail if detail else ""))
    return found[0]


def pee_history_values(history):
    """[(date, value)] of the PEE: parts held x unit price, for every date with a known price.

    history is the esalia-history.json export: "movements" (date, dispositif, support, sens, parts) and
    "supports" (prices: [date, unit price]). SOUSCRIPTION adds parts, RACHAT removes them; the result matches
    the current positions. The price used on a date is the last one known on or before it.
    """
    movements = sorted(history.get("movements", []), key=lambda m: m["date"])
    if not movements:
        return []
    prices = {sid: sorted(s.get("prices", [])) for sid, s in history.get("supports", {}).items()}
    dates = sorted({d for pts in prices.values() for d, _ in pts if d >= movements[0]["date"]})
    parts, last, cursor, out, i = {}, {}, {sid: 0 for sid in prices}, [], 0
    for day in dates:
        while i < len(movements) and movements[i]["date"] <= day:
            m = movements[i]
            key = (m.get("dispositif"), m["support"])
            parts[key] = parts.get(key, 0) + (1 if m.get("sens") == "SOUSCRIPTION" else -1) * m["parts"]
            i += 1
        for sid, pts in prices.items():
            while cursor[sid] < len(pts) and pts[cursor[sid]][0] <= day:
                last[sid] = pts[cursor[sid]][1]
                cursor[sid] += 1
        value = sum(q * last.get(sid, 0) for (_, sid), q in parts.items() if abs(q) > 1e-9)
        if value > 0:
            out.append((day, round(value, 2)))
    return out


def find_kresus_account(data, cfg_key, default):
    return match_kresus_account(data, CFG.get(cfg_key, default), "setting %s" % cfg_key)


def load_state():
    try:
        with open(STATE_FILE) as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def save_state(state):
    os.makedirs(os.path.dirname(STATE_FILE), exist_ok=True)
    tmp = STATE_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(state, f, indent=1)
    os.replace(tmp, STATE_FILE)


# --------------------------------------------------------------------------- bridge

class Bridge:
    def __init__(self, dry):
        self.dry = dry
        self.state = load_state()

    def commit(self):
        if not self.dry:
            save_state(self.state)

    def post(self, path, body, label):
        print(("[dry run] " if self.dry else "") + label)
        if not self.dry:
            sure("POST", path, body)

    @staticmethod
    def trade(account_id, day, **fields):
        return {"trade": dict(account_id=account_id, date=day, currency="EUR", **fields)}

    def valuation(self, account_id, amount, day, label, notes=None):
        v = {"account_id": account_id, "amount": amount, "date": day}
        if notes:
            v["notes"] = notes
        self.post("/api/v1/valuations", {"valuation": v, "upsert": True}, label)

    # ---- Bourse Direct PEA -------------------------------------------------

    def pea_security_ids(self, account_id):
        """Base symbol (AI, ESE...) -> id of the security actually used in Sure."""
        start = (date.today() - timedelta(days=10)).isoformat()
        ids, page = {}, 1
        while True:
            r = sure("GET", "/api/v1/holdings?account_id=%s&start_date=%s&per_page=100&page=%d" % (account_id, start, page))
            for h in r.get("holdings", []):
                sec = h.get("security") or {}
                base = (sec.get("ticker") or "").split(".")[0].upper()
                if base and h.get("qty"):
                    ids[base] = sec["id"]
            if page >= ((r.get("pagination") or {}).get("total_pages") or 1):
                return ids
            page += 1

    def sync_pea(self, account_id):
        files = sorted(glob.glob(BD_GLOB))
        if not files:
            print("PEA: no Bourse Direct positions export")
            return
        with open(files[0]) as f:
            data = json.load(f)
        day = data["date"]
        st = self.state.setdefault("pea", {"positions": {}, "cash": 0.0, "last_date": None})
        if st["last_date"] == day:
            print("PEA: positions of %s already sent" % day)
            return

        # The export keys (libelle, quantite, valeur...) are written by the boursedirect module
        cur, cash = {}, 0.0
        for p in data["positions"]:
            if p.get("isin") == "XX-liquidity":
                cash = p.get("valeur") or 0.0
            elif p.get("quantite"):
                title = html.unescape(p.get("libelle") or "?")
                if p.get("isin") in TICKERS:
                    cur[p["isin"]] = {"qty": p["quantite"], "pru": p["pru"], "cours": p.get("cours"), "nom": title}
                else:
                    print("PEA: line skipped, ISIN missing from TICKERS: %s (%s)" % (title, p.get("isin")))

        old = st["positions"]
        ops = []
        for isin, p in cur.items():
            o = old.get(isin)
            if o is None:
                ops.append(("buy", isin, p["qty"], p["pru"]))
            elif p["qty"] > o["qty"] + 1e-9:
                price = (p["qty"] * p["pru"] - o["qty"] * o["pru"]) / (p["qty"] - o["qty"])
                ops.append(("buy", isin, p["qty"] - o["qty"], price))
            elif p["qty"] < o["qty"] - 1e-9:
                ops.append(("sell", isin, o["qty"] - p["qty"], p.get("cours") or o["pru"]))
        for isin, o in old.items():
            if isin not in cur:
                ops.append(("sell", isin, o["qty"], o.get("cours") or o["pru"]))

        flow = sum((-q * pr if t == "buy" else q * pr) for t, _, q, pr in ops)
        adjust = round(cash - (st["cash"] + flow), 2)
        if adjust > 0:
            self.post("/api/v1/trades", self.trade(account_id, day, type="deposit", amount=adjust),
                      "PEA: deposit of %.2f EUR" % adjust)
            st["cash"] = round(st["cash"] + adjust, 2)
            self.commit()

        sec_ids = self.pea_security_ids(account_id) if ops else {}
        for t, isin, q, pr in ops:
            title = (cur.get(isin) or old.get(isin) or {}).get("nom", isin)
            base = TICKERS[isin].split("|")[0].split(".")[0].upper()
            ident = {"security_id": sec_ids[base]} if base in sec_ids else {"ticker": TICKERS[isin]}
            self.post("/api/v1/trades",
                      self.trade(account_id, day, type=t, qty=round(q, 6), price=round(pr, 6), **ident),
                      "PEA: %s of %g %s at %.4f EUR (%s)" % (t, q, title, pr,
                                                             "existing security" if base in sec_ids else "ticker lookup"))
            st["cash"] = round(st["cash"] + (-q * pr if t == "buy" else q * pr), 2)
            if isin in cur:
                st["positions"][isin] = cur[isin]
            else:
                st["positions"].pop(isin, None)
            self.commit()

        if adjust < 0:
            self.post("/api/v1/trades", self.trade(account_id, day, type="withdrawal", amount=-adjust),
                      "PEA: withdrawal of %.2f EUR" % -adjust)
        if not ops and abs(adjust) < 0.01:
            print("PEA: no change")
        st.update(positions=cur, cash=round(cash, 2), last_date=day)
        self.commit()

    # ---- Esalia PEE ----------------------------------------------------------

    def sync_pee(self, account_id):
        if CFG.get("PEE_MODE", "valuation").lower() == "lines":
            return self.sync_pee_lines(account_id)
        return self.sync_pee_valuation(account_id)

    def backfill_pee_history(self, account_id):
        """PEE_HISTORY=1: one valuation per past date (parts held x unit price), from the first operation on."""
        try:
            with open(ESALIA_HISTORY_FILE) as f:
                history = json.load(f)
        except (OSError, ValueError):
            print("PEE history: no export yet (esalia-history.json), run an Esalia sync in Kresus first")
            return
        st = self.state.setdefault("pee", {})
        until, today = st.get("history_until", ""), date.today().isoformat()
        points = [(d, v) for d, v in pee_history_values(history) if until < d < today]
        if not points:
            print("PEE history: already up to date")
            return
        print(("[dry run] " if self.dry else "") + "PEE history: %d valuation(s) from %s to %s"
              % (len(points), points[0][0], points[-1][0]))
        if self.dry:
            return
        for n, (day, value) in enumerate(points):
            sure("POST", "/api/v1/valuations", {"valuation": {
                "account_id": account_id, "amount": value, "date": day,
                "notes": "Rebuilt from the Esalia operations and unit price history"}, "upsert": True})
            st["history_until"] = day
            if n % 50 == 49:
                self.commit()
        self.commit()

    def sync_pee_valuation(self, account_id):
        if CFG.get("PEE_HISTORY", "0") == "1":
            self.backfill_pee_history(account_id)
        # The export keys (valeur, plus_value, parts, fonds...) are written by the esalia module
        with open(ESALIA_FILE) as f:
            data = json.load(f)
        total = round(sum(p["valeur"] for p in data["positions"]), 2)
        pv = round(sum(p.get("plus_value") or 0 for p in data["positions"]), 2)
        st = self.state.setdefault("pee", {})
        if st.get("last") == [data["date"], total]:
            print("PEE: already up to date (%s)" % data["date"])
            return
        self.valuation(account_id, total, data["date"],
                       "PEE: valuation of %.2f EUR on %s (gain %+.2f EUR)" % (total, data["date"], pv),
                       notes="Gain %+.2f EUR; invested %.2f EUR" % (pv, total - pv))
        st["last"] = [data["date"], total]
        self.commit()

    def sync_pee_lines(self, account_id):
        with open(ESALIA_FILE) as f:
            data = json.load(f)
        day = data["date"]
        st = self.state.setdefault("pee_lines", {"positions": {}, "opened": False})
        if st.get("last_date") == day:
            print("PEE: positions of %s already sent" % day)
            return
        cur = {}
        for p in data["positions"]:
            if p.get("parts"):
                cur[p.get("isin") or p["support_id"]] = {"parts": p["parts"], "pru": p["pru"], "vl": p["vl"], "nom": p["fonds"]}
        old = st["positions"]
        first = not st["opened"]
        # Opening dated the day before: the first micro-buy of the day then sets the price
        open_day = (date.fromisoformat(day) - timedelta(days=1)).isoformat() if first else day
        traded_today = set()

        def deposit(amount, when, label):
            if amount >= 0.01:
                self.post("/api/v1/trades", self.trade(account_id, when, type="deposit", amount=round(amount, 2)),
                          "PEE: deposit of %.2f EUR (%s)" % (amount, label))

        def trade(t, code, qty, price, when, label):
            self.post("/api/v1/trades",
                      self.trade(account_id, when, type=t, qty=qty, price=round(price, 6), manual_ticker=code),
                      "PEE: %s of %g units %s at %.4f EUR" % (t, qty, label, price))

        for code, p in cur.items():
            o = old.get(code)
            if o is None:
                deposit(p["parts"] * p["pru"], open_day, p["nom"])
                trade("buy", code, p["parts"], p["pru"], open_day, p["nom"])
                o = {"parts": p["parts"], "pru": p["pru"], "price": p["pru"]}
                if not first:
                    traded_today.add(code)
            elif p["parts"] > o["parts"] + 1e-9:
                q = p["parts"] - o["parts"]
                price = (p["parts"] * p["pru"] - o["parts"] * o["pru"]) / q
                deposit(q * price, day, p["nom"])
                trade("buy", code, q, price, day, p["nom"])
                o = {"parts": p["parts"], "pru": p["pru"], "price": price}
                traded_today.add(code)
            elif p["parts"] < o["parts"] - 1e-9:
                q = o["parts"] - p["parts"]
                trade("sell", code, q, p["vl"], day, p["nom"])
                self.post("/api/v1/trades", self.trade(account_id, day, type="withdrawal", amount=round(q * p["vl"], 2)),
                          "PEE: withdrawal of %.2f EUR (%s)" % (q * p["vl"], p["nom"]))
                o = {"parts": p["parts"], "pru": p["pru"], "price": p["vl"]}
                traded_today.add(code)
            st["positions"][code] = o
            self.commit()

        for code, o in list(old.items()):
            if code not in cur:
                trade("sell", code, o["parts"], o["price"], day, code)
                self.post("/api/v1/trades",
                          self.trade(account_id, day, type="withdrawal", amount=round(o["parts"] * o["price"], 2)),
                          "PEE: withdrawal of %.2f EUR (%s)" % (o["parts"] * o["price"], code))
                st["positions"].pop(code)
                self.commit()

        for code, p in cur.items():
            o = st["positions"][code]
            if code not in traded_today and abs(p["vl"] - o["price"]) > 1e-6:
                self.post("/api/v1/trades",
                          self.trade(account_id, day, type="buy", qty=MICRO, price=round(p["vl"], 6), manual_ticker=code),
                          "PEE: unit price of %s = %.4f EUR (micro-buy)" % (p["nom"], p["vl"]))
                o["price"] = p["vl"]
                self.commit()

        st.update(opened=True, last_date=day)
        self.commit()

    # ---- Savings account (also used for loans) -----------------------------------

    def sync_savings(self, account_id, spec=None, state_key="savings", tag="Savings", liability=False):
        if spec is None:
            acc = find_kresus_account(kresus_all(), "KRESUS_SAVINGS_MATCH", "livret a")
        else:
            acc = match_kresus_account(kresus_all(), spec, tag)
        # Loan: Kresus reports it as negative, Sure expects the outstanding principal as positive
        today, bal = date.today().isoformat(), abs(acc["balance"]) if liability else acc["balance"]
        st = self.state.setdefault(state_key, {})
        if st.get("last") == [today, bal]:
            print("%s: already up to date" % tag)
            return
        self.valuation(account_id, bal, today, "%s: balance of %.2f EUR on %s" % (tag, bal, today))
        st["last"] = [today, bal]
        self.commit()

    # ---- Checking account ----------------------------------------------------------

    def sync_checking(self, account_id, spec=None, state_key="checking", tag="Checking"):
        data = kresus_all()
        if spec is None:
            acc = find_kresus_account(data, "KRESUS_CHECKING_MATCH", "compte courant")
        else:
            acc = match_kresus_account(data, spec, tag)
        txs = [t for t in walk(data) if t.get("accountId") == acc["id"]
               and isinstance(t.get("amount"), (int, float)) and t.get("date")]
        st = self.state.setdefault(state_key, {"sent": [], "anchored": False})

        # Starting balance: makes the history consistent with the current balance
        if not st["anchored"] and txs:
            oldest = min(t["date"][:10] for t in txs)
            start = round(acc["balance"] - sum(t["amount"] for t in txs), 2)
            when = (date.fromisoformat(oldest) - timedelta(days=1)).isoformat()
            self.valuation(account_id, start, when, "%s: starting balance of %.2f EUR on %s" % (tag, start, when))
            st["anchored"] = True
            self.commit()

        sent = set(st["sent"])
        new = [t for t in sorted(txs, key=lambda t: t["date"]) if str(t.get("id")) not in sent]
        print(("[dry run] " if self.dry else "") + "%s: %d new transaction(s)" % (tag, len(new)))
        for i, t in enumerate(new):
            amt = t["amount"]
            label = (t.get("customLabel") or t.get("label") or t.get("rawLabel") or "Transaction").strip()
            if i < 5:
                print("    %s %+10.2f EUR  %s" % (t["date"][:10], amt, label[:60]))
            elif i == 5:
                print("    ...")
            if not self.dry:
                # external_id: Sure rejects duplicates even if the bridge is run again
                sure("POST", "/api/v1/transactions", {"transaction": {
                    "account_id": account_id, "date": t["date"][:10], "amount": round(abs(amt), 2),
                    "nature": "income" if amt > 0 else "expense", "name": label[:255], "currency": "EUR",
                    "external_id": "kresus-%s" % t.get("id"), "source": "kresus"}})
                st["sent"].append(str(t.get("id")))
                if i % 20 == 0:
                    self.commit()
        self.commit()

        today, bal = date.today().isoformat(), acc["balance"]
        if st.get("last") != [today, bal]:
            self.valuation(account_id, bal, today, "%s: balance of %.2f EUR on %s" % (tag, bal, today))
            st["last"] = [today, bal]
            self.commit()


# --------------------------------------------------------------------------- main

def main():
    ap = argparse.ArgumentParser(description="Kresus/Woob to Sure bridge")
    ap.add_argument("command", choices=["accounts", "sync", "pee-history"])
    ap.add_argument("--dry-run", action="store_true", help="show what would be sent, send nothing")
    args = ap.parse_args()

    if args.command == "pee-history":
        # Preview only: reads the Esalia exports, computes the curve, contacts nothing (neither Sure nor Kresus)
        with open(ESALIA_HISTORY_FILE) as f:
            points = pee_history_values(json.load(f))
        print("PEE history: %d date(s)" % len(points))
        for day, value in (points if len(points) <= 6 else points[:3] + [("...", None)] + points[-3:]):
            print("  %s  %s" % (day, "" if value is None else "%.2f EUR" % value))
        moves = sorted(((abs(b[1] / a[1] - 1), b[0]) for a, b in zip(points, points[1:]) if a[1]), reverse=True)
        if moves:
            print("Largest one-day moves (a jump far above the usual ones would mean a wrong parts count):")
            for change, day in moves[:5]:
                print("  %s  %.2f%%" % (day, 100 * change))
        try:
            with open(ESALIA_FILE) as f:
                today = json.load(f)
            total = round(sum(p["valeur"] for p in today["positions"]), 2)
            print("Valuation from today's positions (%s): %.2f EUR; last point of the curve: %.2f EUR (%s)"
                  % (today["date"], total, points[-1][1] if points else 0, points[-1][0] if points else "-"))
        except (OSError, ValueError, KeyError):
            print("(no esalia-positions.json to compare with)")
        return 0

    if args.command == "accounts":
        print("Sure accounts:")
        for x in sure_accounts():
            print("  - %r (%s)" % (x["name"], x.get("account_type")))
        print("Kresus accounts:")
        for k in kresus_accounts(kresus_all()):
            print("  - %s:%r: %.2f EUR (%s)" % (k["vendor"], k["label"], k["balance"], k["src"]))
        return 0

    bridge = Bridge(args.dry_run)
    ids = {x["name"].strip().lower(): x["id"] for x in sure_accounts()}
    errors = 0
    tasks = [(key, CFG.get(key), fn) for key, fn in (("SURE_ACCOUNT_PEA", bridge.sync_pea),
                                                     ("SURE_ACCOUNT_PEE", bridge.sync_pee),
                                                     ("SURE_ACCOUNT_SAVINGS", bridge.sync_savings),
                                                     ("SURE_ACCOUNT_CHECKING", bridge.sync_checking))]
    # SURE_LINK_<n>=savings|checking|loan | [bank:]Kresus label word | Sure account name
    links = sorted((k for k in CFG if k.startswith("SURE_LINK_")),
                   key=lambda k: (len(k), k))
    for key in links:
        parts = [p.strip() for p in CFG[key].split("|")]
        if len(parts) != 3 or parts[0].lower() not in LINK_KINDS or not all(parts):
            print("ERROR %s: expected format savings|checking|loan | [bank:]label | Sure account" % key)
            errors += 1
            continue
        kind, spec, name = parts
        kind = kind.lower()
        state_key = "%s:%s" % (kind, name.lower())
        if kind == "checking":
            fn = functools.partial(bridge.sync_checking, spec=spec, state_key=state_key, tag=name)
        else:
            fn = functools.partial(bridge.sync_savings, spec=spec, state_key=state_key, tag=name,
                                   liability=kind == "loan")
        tasks.append((key, name, fn))

    for key, name, fn in tasks:
        if not name:
            print("%s not set, skipped" % key)
            continue
        acc = ids.get(name.strip().lower())
        if not acc:
            print("ERROR: Sure account %r not found (%s)" % (name, key))
            errors += 1
            continue
        try:
            fn(acc)
        except Exception as e:
            print("ERROR %s: %s" % (key, e))
            errors += 1
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())
