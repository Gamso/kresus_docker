# Kresus/Woob → Sure bridge

Sends the data collected by Kresus to Sure every morning. Plain Python 3, no dependency. In the Docker
project it runs in the `sure-bridge` service (see the top-level README); the paths below are those of the container.

| Sure account | Source | What is sent |
|---|---|---|
| Bourse Direct PEA (Investment, PEA) | `/var/lib/kresus/boursedirect-positions-*.json` | Opening deposit then buys at average cost; afterwards buys (price derived from the average cost), sells, cash adjustment |
| Crypto (Crypto) | `/var/lib/crypto-ledger/positions.json` (`crypto-ledger.py`) | The history of Binance and Kraken replayed day by day with its real dates, then the new days |
| Esalia PEE (Investment) | `/var/lib/kresus/esalia-positions.json` | `PEE_MODE=valuation`: one global balance. `PEE_MODE=lines`: one line per fund + micro-buys for the unit price |
| Fortuneo savings account (Savings) | Kresus API | Daily balance |
| Fortuneo checking account (Checking) | Kresus API | Transactions (deduplicated via `external_id`) + daily balance |
| `SURE_LINK_<n>` (La Banque Postale or any other Kresus bank) | Kresus API | `savings`: daily balance; `checking`: transactions + daily balance; `loan`: daily outstanding principal, as a positive amount |

## Designating a Kresus account

`KRESUS_SAVINGS_MATCH`, `KRESUS_CHECKING_MATCH` and the 2nd field of `SURE_LINK_<n>` designate a Kresus account
by a word of its label, prefixed with the bank if needed (Woob module name): `fortuneo:livret a`,
`bp:compte courant`. The prefix is required as soon as two banks share a label; the bridge refuses a filter
matching zero or several accounts and lists the candidates. `sure-bridge.py accounts` prints every account as
`bank:label`.

```ini
SURE_LINK_1=loan|bp:my loan 1|Home loan 1
SURE_LINK_2=loan|bp:my loan 2|Home loan 2
```

A `loan` link targets a Sure account of type Loan: Kresus reports a loan as negative, the bridge sends the
outstanding principal as a positive amount, which Sure counts as a debt. The `bp` module provides no
transactions for loans, only the balance is tracked.

Every link has its own state (`<type>:<Sure account>` in `state.json`): renaming the Sure account of a
`checking` link sends its transactions again, which Sure deduplicates by `external_id`.

## Usage

```bash
docker compose run --rm sure-bridge sure-bridge.py accounts          # check the account names
docker compose run --rm sure-bridge sure-bridge.py sync --dry-run    # dry run
docker compose run --rm sure-bridge sure-bridge.py sync              # real send
```

Sure accounts must be created by hand (the API cannot create them), with a starting balance of 0.

## State

`/var/lib/sure-bridge/state.json` (volume `sure-bridge-state`) remembers what has been sent (positions, cash,
transactions). Deleting it makes the bridge send everything again: back it up.

State keys were renamed from the French version (`livret` → `savings`, `courant` → `checking`, `pret:` → `loan:`).
Carrying over an old `state.json` resends balances (harmless, they are upserted) and re-checks transactions
(deduplicated); the PEA and PEE keys are unchanged.

## Crypto

`crypto-ledger.py` (see `crypto-ledger/README.md`) archives the Binance and Kraken movements and writes
`positions.json`: day by day, the net change of quantity and cost of every coin, the transfers between the two
exchanges netting to their fee. With `SURE_ACCOUNT_CRYPTO` set, the bridge replays those days in that Sure account:

- coins added: a buy at the cost they were added at (EUR paid, or the cost carried from the coin exchanged for them);
  coins gone: a sell at the cost they leave with, so that Sure holds the quantities and the cost of the exchanges;
- a deposit before the buys when the account lacks cash, a withdrawal after the sells when it has too much: the cash
  stands for the cost of the coins not sent (airdropped tokens, WBETH...) until they are exchanged for a sent one;
- every day is sent: each reward is a buy on its date (the rewards of one coin on one day make one buy).
  `CRYPTO_MIN_EUR=1` makes the additions under 1 EUR wait and add up instead, for fewer trades;
- today is sent tomorrow, once complete.

The first send replays the whole history, with its real dates, so that the curve of the account starts at the first
buy: **the Sure account must be empty** (the bridge refuses otherwise). Afterwards each sync sends the days not sent yet
(`crypto.last_day` in `state.json`). The scheduler runs `crypto-ledger.py sync` before the bridge when an exchange key
is set.

Coins sent: BTC, ETH, BNB, USDC, as the `CRYPTO:BTC`... securities Sure uses for its crypto accounts: when one is held
in any account (a crypto account synced by Sure, for instance), its id is reused, with its price. Another coin:
`CRYPTO_TICKER_<COIN>=CRYPTO:<COIN>`. If Sure creates a coin "offline" (no price), fix it with "Edit security" as for
the PEA. The Sure account is of type Crypto.

```bash
docker compose run --rm sure-bridge sure-bridge.py sync --dry-run     # the plan, nothing sent
```

## PEE history

With `PEE_HISTORY=1` (valuation mode), the bridge also fills the past of the PEE curve. The `esalia` module writes
`/var/lib/kresus/esalia-history.json` during its sync: every operation of the account (parts bought or sold per fund,
from `GET /transactions` and its `instructions`) and the unit price history of every fund (`historique_valorisations`).
The bridge then sends, for each date with a known price since the first operation, one valuation equal to
**parts held × unit price**. The reconstruction was checked against the real positions: it gives exactly the parts held today.

- Sent once; the next syncs only send the missing days (`pee.history_until` in `state.json`). Valuations are upserted,
  so running it twice is harmless.
- Funds that disappeared (fund merger) are valued with their own price history until they were sold or merged.
- The history file is refreshed when the number of operations changes, or once a day.
- `sure-bridge.py pee-history` prints a preview of the curve (first and last dates, and the last point compared with
  today's valuation) without contacting Sure or Kresus. `sync --dry-run` prints how many valuations would be sent.
- Not done in `lines` mode, where the holdings are built from trades.

## PEA: replaying the last two months with their real dates

The Bourse Direct history page only shows about two months, and the bridge normally sends the PEA as differences
between two exports of the positions, dated the day it notices them. `pea-rebuild` replaces that with the real
operations: the `boursedirect` module writes the rows of the history page (date, operation, security, quantity,
price, net amount) to `/var/lib/kresus/boursedirect-history-<account>.json`, merged across syncs so rows that
left the window are kept.

The replay rebuilds the state at the start of the window backwards from today's positions: quantity = quantity now
− bought + sold, opening average cost solved so that replaying the buys and sells gives today's PRU, opening cash =
today's cash − the net cash flows. Then it sends the opening deposit and positions, and every operation with its date:
buys and sells as trades, transfers and coupons as deposits (a coupon counts as a deposit, as before). Operations
without cash (listing changes) are ignored. Afterwards `state.json` holds today's positions, so the daily sync goes
back to sending only differences.

**It writes to Sure and needs an empty PEA account**, otherwise the positions would be counted twice:

1. Run a Bourse Direct sync in Kresus (this writes the history file), then
   `sure-bridge.py pea-rebuild --dry-run`: it prints the plan (positions at the start of the window, opening deposit,
   the operations) and tells whether the Sure account is empty. It writes nothing and fixes nothing.
2. Empty the PEA account in Sure (delete its trades, or recreate the account with the same name and a balance of 0).
3. `sure-bridge.py pea-rebuild --yes` writes it. Without `--yes` the command refuses; with a non-empty Sure account it
   refuses too, and it refuses to run twice.

The command stops with an error, writing nothing, if a security of the history is not among today's positions
(a fully sold line), if an operation with a quantity is unknown, or if a buy is larger than the position.

## Caveats

- **Securities created offline**: if Yahoo is rate-limited during a first buy, Sure creates the security "offline"
  (no price). Fix it with "Edit security" in the holding's page. The bridge then uses the id of the security
  actually held, never the old one.
- **One security = one price provider** in Sure. Providers are set in `/etc/sure/.env`
  (`SECURITIES_PROVIDERS=yahoo_finance,eodhd`).
- **PEE in `lines` mode**: first delete the existing valuation of the PEE account in Sure. The funds show up under
  their QS code (unlisted); one "Buy 0.000000001" line per fund is added each time the unit price changes.
- **Limits**: a PEA dividend is counted as a deposit; a sell is recorded at the day's price rather than its exact price.
- The JSON files exported by the `boursedirect` and `esalia` modules keep their French keys (`libelle`, `quantite`,
  `valeur`, `plus_value`, `fonds`...): they are the contract between the modules and the bridge.

## To do

- Crypto: send a sale for EUR at its real price (today it goes at the cost of the coins sold, so its gain does not
  show in Sure; there has been none so far).
