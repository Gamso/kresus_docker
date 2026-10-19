# Kresus/Woob → Sure bridge

Sends the data collected by Kresus to Sure every morning. Plain Python 3, no dependency. In the Docker
project it runs in the `sure-bridge` service (see the top-level README); the paths below are those of the container.

| Sure account | Source | What is sent |
|---|---|---|
| Bourse Direct PEA (Investment, PEA) | `/var/lib/kresus/boursedirect-positions-*.json` | Opening deposit then buys at average cost; afterwards buys (price derived from the average cost), sells, cash adjustment |
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

- Crypto (Kraken): opening buy at the Koinly cost basis, then buys read from the Kraken API (read-only key).
  Koinly has no public API and Sure has no Koinly connector.
