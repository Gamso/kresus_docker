# Crypto ledger

Archives the movements of crypto exchange accounts through their API, then replays them to give, per asset, the
quantity held and the average cost in EUR. Plain Python 3, no dependency. It knows nothing of Sure or Kresus: it writes
`positions.json`, which the Sure bridge replays (see `sure-bridge/README.md`, "Crypto").

Exchanges: **Binance** and **Kraken**, with the transfers between them paired: coins moved from one to the other keep
their cost.

## Why

When coins move from one exchange to another, each exchange only sees its own side: the receiving one shows a deposit,
and a tracker fed by it counts the coins at the price of that day. The real cost is lost. Collecting every movement of
every exchange, and pairing a withdrawal with the deposit it became, keeps the cost of the coins wherever they go.

## Setup

Create **read-only** API keys and put them in `config/sure-bridge.env` (an exchange without a key is skipped):

- Binance, API Management: only "Enable Reading" (no trading, no withdrawals; an IP restriction if you can);
- Kraken, Settings → API: only "Query Funds" and "Query Ledger Entries".

```ini
BINANCE_API_KEY=...
BINANCE_API_SECRET=...
KRAKEN_API_KEY=...
KRAKEN_API_SECRET=...
```

Fetch the history of an exchange before closing its account: once closed, its API is gone, the archive stays.

## Usage

```bash
docker compose run --rm sure-bridge crypto-ledger.py sync           # fetch what is new
docker compose run --rm sure-bridge crypto-ledger.py summary        # quantity and average cost per asset
docker compose run --rm sure-bridge crypto-ledger.py events BTC     # the movements of one asset, oldest first
docker compose run --rm sure-bridge crypto-ledger.py export         # write positions.json (sync does it too)
```

The `sure-bridge` service runs `crypto-ledger.py sync` every day just before the bridge, when a key is set.

`summary` first compares, per exchange, the replayed quantities with the live balances (Binance: spot, funding,
Simple Earn flexible and locked; Kraken: the balance, staked and Earn included). A difference means a kind of movement
is missing from the archive: that is the check to make after the first sync. Then it replays all the exchanges together
and prints the transfers it paired, then per asset the quantity, the cost, the average cost, the price of the last
complete hour, the value and the latent gain, with the totals and the realized gain of past trades (indicative).

## The archive

`/var/lib/crypto-ledger` (volume `crypto-ledger`):

| File | Content |
|---|---|
| `binance.jsonl`, `kraken.jsonl` | One line per movement, the raw record as the exchange returned it, keyed by its id. Append-only. |
| `binance-cursors.json`, `kraken-cursors.json` | How far each history was fetched (time, or last trade id per pair) |
| `prices.json` | EUR prices already looked up (hourly candles, public data) |
| `positions.json` | For the Sure bridge: the coins held today and, day by day, the net change of quantity and cost of each |

- A movement already archived is never fetched or processed again. A sync only asks for what is newer than the
  cursors, with one day of overlap; a pending deposit or withdrawal keeps its cursor before it until it is final.
- The first sync walks back the whole history from `CRYPTO_LEDGER_BINANCE_START` (2017-07-01 by default), in the
  windows Binance allows (90 days, 30 for Convert): a few minutes.
- The raw records are kept, so the replay can be fixed later without asking Binance again.
- Exchanges do not keep everything forever: back the volume up (see the top-level README). Deleting a
  `*-cursors.json` makes the next sync go through that whole history again; nothing is archived twice.

## What is collected (Binance)

| Kind | Endpoint |
|---|---|
| Trades | `/api/v3/myTrades`, per pair: the pairs whose two assets were seen in the account, or whose quote is a common one (EUR, USDT, USDC, FDUSD, BUSD, BTC, ETH, BNB) |
| Convert | `/sapi/v1/convert/tradeFlow` |
| Crypto deposits and withdrawals | `/sapi/v1/capital/deposit/hisrec`, `/sapi/v1/capital/withdraw/history` (with their transaction id) |
| Fiat deposits and withdrawals | `/sapi/v1/fiat/orders` |
| Card buys and sells | `/sapi/v1/fiat/payments` |
| Dust converted to BNB | `/sapi/v1/asset/dribblet` |
| Distributions (Earn, staking, airdrops) | `/sapi/v1/asset/assetDividend` |
| Simple Earn real-time rewards (not in the distributions) | `/sapi/v1/simple-earn/flexible/history/rewardsRecord` |
| ETH staking and WBETH wrapping | `/sapi/v1/eth-staking/...` |

Not collected: margin, futures, P2P, pairs that no longer exist on Binance (their trades are not listed).

## What is collected (Kraken)

Everything comes from the ledger (`/0/private/Ledgers`): one entry per balance change, with its fee. The entries
sharing a `refid` form one movement: the two sides of a trade, or a move between spot and staking or Earn, which nets
to nothing once the suffix of the staked asset is dropped (`XBT.M` and `XXBT` are both BTC). Asset codes become the
usual tickers (`XXBT` → BTC, `ZEUR` → EUR). Other kinds of entries count as a reward when they add coins, as a
withdrawal when they remove some. Margin positions are not handled.

## The replay

Weighted average cost per asset, in EUR (EUR itself is cash). `CRYPTO_LEDGER_METHOD` chooses how a trade of a crypto
for another crypto counts:

| Method | Crypto for crypto (Convert, dust, a trade without EUR) | Realized gain |
|---|---|---|
| `carry` (default) | not a sale: the cost of what was given, fees included, goes to what was received | only on a sale for EUR |
| `market` | a sale at the market price: what was received starts at that price | on every trade |

`carry` shows the EUR put in against the value today: an ETH bought 100 EUR and exchanged for BTC when worth 58 EUR
gives a BTC that cost 100 EUR (it is also what Koinly does with its `pfu_france` method). `market` treats each coin as
a separate line: that BTC cost 58 EUR and the 42 EUR lost on the ETH are realized. Neither is a tax calculation; both give the same total (latent + realized),
split differently. Then:

- a trade with EUR on one side is valued at that EUR, fees included;
- a buy through "Buy crypto" costs the EUR paid, taken from the EUR balance or from the card;
- a reward or a deposit from outside counts at the market price of that day;
- a withdrawal paired with a deposit on the other exchange is a transfer: the deposit gets the cost that left, the
  transfer fee included (the coins changed place, not owner). Pairing: same asset, received the same quantity or up to
  3 % less, within three days; the same transaction id wins when both sides have one, then the closest in time;
- a withdrawal that is not paired (to a wallet outside) takes its share of the cost with it (listed by `summary`);
- ETH staked for WBETH (and back) keeps its cost, without gain.

Prices: close of the hourly Binance candle of `<asset>EUR`, otherwise through USDT or BTC. It is not a tax
calculation: the French tax form (2086) works on the whole portfolio, not per asset.

## Tests

```bash
python -m unittest discover -s crypto-ledger
```

Against fake Binance and Kraken APIs: archiving once, pending movements, signatures, Kraken ledger grouping, replay
of quantities and costs, transfer pairing, carry and market methods, summary of both exchanges.
