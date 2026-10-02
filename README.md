# IADSS — IBKR

TradingView **sequence complete** alerts from the [IADSS Confluence Monitor](https://www.tradingview.com/script/GzeIM5db-IADSS-Confluence-Monitor/) place stock orders on Interactive Brokers immediately.

This repo is a copy of [ptos-ibkr](https://github.com/ballzac81/ptos-ibkr) with only the signal side changed. That repo is unchanged — keep it if you still want the arm-then-trend flow (`/buy-signal` then `/trend-up`).

This is **not** the Kraken bot. [IADSS-Signal-Tracker](https://github.com/ballzac81/IADSS-Signal-Tracker) stays on crypto. Do not point those alerts at this host.

## What changed vs PTOS-IBKR

| Keep | Change |
| --- | --- |
| `ibkr_trader.py`, IB Gateway compose, paper port **4004** | Webhooks are `/lb-buy` and `/lb-sell` |
| `EXCHANGE` / `CURRENCY` / market hours | No arm window (`/buy-signal` then `/trend-up` is gone) |
| `POSITION_SIZE_PCT` — percent of **IBKR equity**, not a Kraken ledger | Body is `{"pair":"BHP","token":"..."}` or `{"ticker":"{{ticker}}","token":"..."}` |
| Trailing stop | Sequence Complete fires the order immediately |

One buy alert and one sell alert per ticker. Same JSON, different path.

`/lb-sell` always closes the long (`SELL_PCT`, default 100%). It does not open a short and it does not rotate a paired ETF. That behaviour still lives in ptos-ibkr.

## Do not mix the hosts

| Market | Repo | Hostname |
| --- | --- | --- |
| Stocks (IBKR) | this repo | `https://ibkr.ballzac.uk` |
| Crypto (Kraken / Freqtrade) | IADSS-Signal-Tracker | the existing signals hostname |

A crypto body like `{"pair":"HYPE/USD"}` sent here would be read as a stock named `HYPE`. Don't.

## How an order fires

```
IADSS Confluence Monitor
  BUY Sequence Complete  →  POST /lb-buy   →  buy that symbol now
  SELL Sequence Complete →  POST /lb-sell  →  close that long now
```

Early-warning alerts (`/confirm-buy`, `/confirm-sell`) are not implemented. Create two alerts per ticker, not four.

The trailing stop still runs in the background during market hours. Set `TRAILING_STOP_PCT=0` to disable it.

## TradingView alerts

Paid plan (webhooks) required. On each stock chart, two alerts, **Once per bar close**, condition = sequence complete.

Message (identical on both alerts):

```json
{"pair": "{{ticker}}", "token": "YOUR_SECRET_TOKEN"}
```

Hard-coding the code is fine too:

```json
{"pair": "BHP", "token": "YOUR_SECRET_TOKEN"}
```

`{{ticker}}` is substituted by TradingView before the request is sent (`BHP`, `CBA`, `AAPL`). `ASX:BHP`, `BHP/AUD`, and `BHP.AX` are also accepted and reduced to `BHP`.

| Alert | Webhook URL |
| --- | --- |
| BUY Sequence Complete | `https://ibkr.ballzac.uk/lb-buy` |
| SELL Sequence Complete | `https://ibkr.ballzac.uk/lb-sell` |

Token may also be sent as `?token=`, `X-Token`, or `X-Webhook-Secret`. Prefer the JSON body or a header — query strings land in proxy logs.

There is no "fire now" on an existing alert. To test without TradingView (paper account):

```bash
curl -X POST https://ibkr.ballzac.uk/lb-buy \
  -H "Content-Type: application/json" \
  -d '{"pair":"BHP","token":"YOUR_SECRET_TOKEN"}'
```

## TradingView timeout — read this before resending

TradingView gives up after about 3 seconds and paints the webhook **red**. An IBKR order is slower than that (price snapshot, then the order), so a red alert can still have filled. The same rule as the crypto bot:

**Check positions before you resend.**

```bash
curl -H "X-Token: YOUR_SECRET_TOKEN" https://ibkr.ballzac.uk/positions
```

A second `/lb-buy` while the first order is still in flight, or while the shares are already held, is ignored on purpose. Resending a red buy without looking is how you double up. After a restart the in-memory guard is empty — `/positions` (the broker) is the check that matters.

## Paper first

`TRADING_MODE=paper` and `IB_PORT=4004`. Do not flip those to live until a paper buy and sell have shown up in `/positions`.

Telegram lines are prefixed `IADSS IBKR`. They are not the Kraken `IADSS BUY executed` messages, and they are not proof of a live fill while `TRADING_MODE=paper`.

## Setup

Docker on the Unraid host. IBKR paper account. Cloudflare tunnel already running (same one as the Kraken tracker). TradingView with the IADSS indicator.

```bash
git clone https://github.com/ballzac81/iadss-ibkr.git
cd iadss-ibkr
cp .env.example .env
openssl rand -hex 24    # paste as SECRET_TOKEN
```

`.env.example` is set for **ASX** (`EXCHANGE=ASX`, `CURRENCY=AUD`, Sydney 10:00–16:00) because the alert example is `BHP`. For US names switch to:

```
EXCHANGE=SMART
CURRENCY=USD
MARKET_TZ=America/New_York
MARKET_OPEN=09:30
MARKET_CLOSE=16:00
```

Sizing is a fraction of IBKR net liquidation in `CURRENCY`, capped by buying power. There is no per-pair ledger and no `/deposit`.

```
POSITION_SIZE_PCT=0.10
MAX_POSITION_SIZE_PCT=0.50
```

Market orders are rejected outside `MARKET_OPEN`–`MARKET_CLOSE` (weekdays). Nothing is queued — sequence complete either trades now or returns an error. A daily bar that closes a minute after the bell will fail; widen `MARKET_CLOSE` slightly or set `ORDER_TYPE=limit` if you need outside-RTH.

### Cloudflare

In the existing tunnel, add a public hostname. Leave the path empty.

| Hostname | Service |
| --- | --- |
| `ibkr.ballzac.uk` | `HTTP` → `iadss-ibkr:5005` |

The bot joins `DOCKER_NETWORK` (default `ballzac`) so that name resolves. Fallback if the tunnel is not on that network: `HTTP` → `127.0.0.1:5006`.

Do not attach the Kraken alerts, and do not point `ibkr.ballzac.uk` at `signal-tracker`.

### Start

Stop the **ptos-ibkr** stack first if it is running. Both compose files publish paper port 4004, VNC `5901`, and noVNC `6080`. One gateway login, not two.

```bash
docker compose up -d --build
```

First start needs the IB Gateway 2FA login. Open `http://UNRAID_IP:6080` (or VNC port `5901`, password `VNC_PASSWORD` / `ibkr_vnc`). After that the container re-logins itself. Sometimes an update still needs a manual login.

```bash
curl http://127.0.0.1:5006/health
curl -H "X-Token: YOUR_SECRET_TOKEN" http://127.0.0.1:5006/status
curl -H "X-Token: YOUR_SECRET_TOKEN" http://127.0.0.1:5006/positions
```

`/health` is open (no positions, no equity). `/status` and `/positions` need the token.

### Go live

Only after paper webhooks, size, and the red-alert check look right:

```
TRADING_MODE=live
IB_PORT=4002
```

```bash
docker compose up -d
```

Complete the live 2FA prompt if the gateway asks. Live Telegram lines look the same as paper — trust `/positions` and the IBKR account, not the alert colour.

## Endpoints

| Endpoint | Method | Auth | What it does |
| --- | --- | --- | --- |
| `/lb-buy` | POST | token | Sequence complete → buy `POSITION_SIZE_PCT` of equity |
| `/lb-sell` | POST | token | Sequence complete → close the long (`SELL_PCT`) |
| `/positions` | GET | token | Broker positions, equity, buying power |
| `/status` | GET | token | In-flight flags, cooldowns, config |
| `/health` | GET | no | Process up, gateway connected, market hours |

`/buy-signal`, `/trend-up`, `/sell-signal`, and `/trend-down` are not on this server.

## Config

All of it is `.env`. See [.env.example](.env.example).

| Variable | Default in example | Meaning |
| --- | --- | --- |
| `TRADING_MODE` | `paper` | `paper` or `live` |
| `IB_PORT` | `4004` | `4004` paper, `4002` live |
| `EXCHANGE` / `CURRENCY` | `ASX` / `AUD` | IBKR contract. US: `SMART` / `USD` |
| `MARKET_TZ` / `MARKET_OPEN` / `MARKET_CLOSE` | Sydney 10:00–16:00 | When market orders are allowed |
| `POSITION_SIZE_PCT` | `0.10` | Fraction of equity per buy |
| `MAX_POSITION_SIZE_PCT` | `0.50` | Notional cap |
| `SELL_PCT` | `1.0` | Fraction of the long to close |
| `TRAILING_STOP_PCT` | `0.05` | Pullback from peak. `0` disables |
| `COOLDOWN_SECONDS` | `0` | Pause before the next buy after a close |
| `DOCKER_NETWORK` | `ballzac` | Existing cloudflared network |
| `SECRET_TOKEN` | | Required on trade, status, and positions |

`WINDOW_SECONDS` and `SELL_MODE` from ptos-ibkr are ignored. A warning is logged if `SELL_MODE` is set to anything other than `close_long`.

## Running next to the Kraken bot

Safe. Different repo, different compose project, different hostname, different port (`5006` here, `5000` on the Kraken tracker). They must not share a webhook URL.

Not safe: running this compose **and** ptos-ibkr together. They both want the gateway ports.

## Tests

No IBKR needed:

```bash
python3 -m unittest tests.test_signals
```

## Security

- Do not commit `.env`.
- API key on the IBKR account should not be your only control — the gateway logs in as you. Use a paper account until the alerts are right, and don't keep more in the live account than you intend to trade.
- Port `5006` is bound to `127.0.0.1`. The public path is the Cloudflare hostname.
- `/positions` is not anonymous.
- One Gunicorn worker. Raising it duplicates the IB connection and the trailing stop.

## Troubleshooting

**`ibkr_connected: false`** — gateway wants a login. noVNC on port `6080`.

**Red webhook, no idea if it filled** — `GET /positions` before any resend. See the timeout section.

**`Market closed`** — sequence complete does not wait for the open. Check the clock against `MARKET_TZ`.

**`Already long` / ignored `already_long`** — the buy is already on. That is the retry guard, not a failed sequence.

**`Insufficient buying power`** — equity × `POSITION_SIZE_PCT` is below one share, or buying power is short. `/positions` shows both.

**Compose fails on `ballzac`** — `DOCKER_NETWORK` must be the network your cloudflared container is already attached to.

**Compose fails on port 4004 or 6080** — ptos-ibkr (or another gateway) is still up. Stop that stack.

## Disclaimer

Educational software, not financial advice. You can lose money. Paper trade first. You are responsible for the orders this process sends.
