"""IADSS sequence-complete webhooks → IBKR.

One buy alert and one sell alert per ticker. Same JSON, different path.
Sequence Complete places the order immediately. There is no arm window.

Do not point Kraken / IADSS-Signal-Tracker alerts at this process.
"""

import os
import logging
import threading
from dotenv import load_dotenv

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)

from flask import Flask, request, jsonify
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address

from signal_tracker import SignalTracker, symbol_from_payload
from ibkr_trader import IBKRTrader
from position_monitor import PositionMonitor
from telegram_notify import TelegramNotify

SECRET_TOKEN = os.environ.get("SECRET_TOKEN", "")
if not SECRET_TOKEN:
    logger.warning("SECRET_TOKEN is not set -- all webhook requests will be accepted!")

if os.environ.get("WINDOW_SECONDS"):
    logger.info("WINDOW_SECONDS is ignored — sequence complete fires the order immediately")

_sell_mode = os.environ.get("SELL_MODE", "close_long")
if _sell_mode != "close_long":
    logger.warning(
        "SELL_MODE=%s is not used by /lb-sell. This bot always closes the long "
        "(SELL_PCT). The arm-then-trend and paired-ETF flow stays in ptos-ibkr.",
        _sell_mode,
    )

notifier = TelegramNotify()
trader = IBKRTrader()
tracker = SignalTracker()
monitor = PositionMonitor(trader, tracker, notifier)

_monitor_thread = threading.Thread(target=monitor.run, daemon=True, name="position-monitor")
_monitor_thread.start()


def _cf_aware_ip():
    return request.headers.get("CF-Connecting-IP") or get_remote_address()


app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 16 * 1024

limiter = Limiter(
    app=app,
    key_func=_cf_aware_ip,
    default_limits=["30 per minute"],
    storage_uri="memory://",
)


def _payload():
    data = request.get_json(silent=True) or {}
    token = (
        request.args.get("token")
        or request.headers.get("X-Token")
        or request.headers.get("X-Webhook-Secret")
        or data.get("token")
        or ""
    )
    return data, token


def _authorized(token):
    if not SECRET_TOKEN:
        return True
    return token == SECRET_TOKEN


def _bad(msg, code=400):
    return jsonify({"error": msg}), code


def _ignored(claim):
    reason = claim.get("reason") or claim.get("status")
    ticker = claim.get("ticker")
    notifier.send("IADSS IBKR ignored (%s): %s" % (reason, ticker))
    logger.info("[webhook] ignored: %s", claim)
    return jsonify(claim)


@app.route("/lb-buy", methods=["POST"])
@limiter.limit("10 per minute")
def lb_buy():
    data, token = _payload()
    if not _authorized(token):
        return _bad("unauthorized", 401)
    ticker = symbol_from_payload(data)
    if not ticker:
        return _bad('valid pair or ticker is required (e.g. {"pair":"BHP"} or {"ticker":"{{ticker}}"})')

    claim = tracker.begin_buy(ticker)
    if not claim["ok"]:
        return _ignored(claim)

    logger.info("[webhook] lb-buy sequence complete: %s", ticker)
    notifier.send("IADSS IBKR BUY sequence complete — firing %s" % ticker)
    try:
        result = trader.buy(ticker)
        tracker.on_bought(ticker)
        if result.get("action") == "buy" and result.get("price"):
            monitor.on_entry(result["ticker"], result["price"])
        notifier.send(
            "IADSS IBKR BUY: %s %s sh @ ~%s"
            % (ticker, result.get("shares"), result.get("price"))
        )
        logger.info("[webhook] lb-buy executed: %s", result)
        return jsonify({"status": "executed", "ticker": ticker, "result": result})
    except ValueError as exc:
        return _handle_trade_error("buy", ticker, exc)
    except Exception as exc:
        return _handle_trade_error("buy", ticker, exc)


@app.route("/lb-sell", methods=["POST"])
@limiter.limit("10 per minute")
def lb_sell():
    data, token = _payload()
    if not _authorized(token):
        return _bad("unauthorized", 401)
    ticker = symbol_from_payload(data)
    if not ticker:
        return _bad('valid pair or ticker is required (e.g. {"pair":"BHP"} or {"ticker":"{{ticker}}"})')

    claim = tracker.begin_sell(ticker)
    if not claim["ok"]:
        return _ignored(claim)

    logger.info("[webhook] lb-sell sequence complete: %s", ticker)
    notifier.send("IADSS IBKR SELL sequence complete — firing %s" % ticker)
    try:
        result = trader.close_long(ticker)
        tracker.on_sold(ticker)
        monitor.on_exit(ticker)
        notifier.send(
            "IADSS IBKR SELL: %s closed %s sh"
            % (ticker, result.get("shares"))
        )
        logger.info("[webhook] lb-sell executed: %s", result)
        return jsonify({"status": "executed", "ticker": ticker, "result": result})
    except ValueError as exc:
        return _handle_trade_error("sell", ticker, exc)
    except Exception as exc:
        return _handle_trade_error("sell", ticker, exc)


def _handle_trade_error(kind, ticker, exc):
    msg = str(exc)
    if kind == "buy" and "Already long" in msg:
        tracker.mark_long(ticker)
        body = {"status": "ignored", "reason": "already_long", "ticker": ticker, "detail": msg}
        notifier.send("IADSS IBKR BUY ignored (already long): %s — check /positions before resending" % ticker)
        logger.info("[webhook] lb-buy already long: %s", msg)
        return jsonify(body)
    if kind == "sell" and "No long position" in msg:
        tracker.mark_flat(ticker)
        body = {"status": "ignored", "reason": "flat", "ticker": ticker, "detail": msg}
        notifier.send("IADSS IBKR SELL ignored (flat): %s" % ticker)
        logger.info("[webhook] lb-sell flat: %s", msg)
        return jsonify(body)

    if kind == "buy":
        tracker.on_buy_failed(ticker)
    else:
        tracker.on_sell_failed(ticker)
    code = 400 if isinstance(exc, ValueError) else 500
    logger.error("[webhook] %s failed for %s: %s", kind, ticker, exc)
    notifier.send("IADSS IBKR %s FAILED: %s -- %s" % (kind.upper(), ticker, exc))
    return _bad(msg, code)


def _config():
    return {
        "position_size_pct": float(os.environ.get("POSITION_SIZE_PCT", "0.10")),
        "max_position_size_pct": float(os.environ.get("MAX_POSITION_SIZE_PCT", "0.50")),
        "sell_pct": float(os.environ.get("SELL_PCT", "1.0")),
        "trailing_stop_pct": float(os.environ.get("TRAILING_STOP_PCT", "0.05")),
        "cooldown_seconds": int(os.environ.get("COOLDOWN_SECONDS", "0")),
        "exchange": os.environ.get("EXCHANGE", "SMART"),
        "currency": os.environ.get("CURRENCY", "USD"),
        "ib_port": int(os.environ.get("IB_PORT", "4004")),
        "order_type": os.environ.get("ORDER_TYPE", "market"),
    }


@app.route("/status", methods=["GET"])
def status():
    _data, token = _payload()
    if not _authorized(token):
        return _bad("unauthorized", 401)
    return jsonify({
        "ibkr_connected": trader.is_connected(),
        "market_hours": trader.is_market_hours(),
        "signals": tracker.status(),
        "config": _config(),
    })


@app.route("/positions", methods=["GET"])
def positions():
    _data, token = _payload()
    if not _authorized(token):
        return _bad("unauthorized", 401)
    try:
        return jsonify({
            "positions": trader.get_all_positions(),
            "equity": trader.get_equity(),
            "buying_power": trader.get_buying_power(),
            "market_hours": trader.is_market_hours(),
        })
    except Exception as exc:
        return _bad(str(exc), 500)


@app.route("/health", methods=["GET"])
def health():
    return jsonify({
        "status": "ok",
        "ibkr_connected": trader.is_connected(),
        "market_hours": trader.is_market_hours(),
    })


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "5005"))
    app.run(host="0.0.0.0", port=port)
