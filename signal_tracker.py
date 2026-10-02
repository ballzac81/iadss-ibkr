"""
signal_tracker.py
Per-ticker bookkeeping for immediate IADSS sequence-complete orders.

There is no arm window. /lb-buy and /lb-sell place the IBKR order immediately.
This tracker only stops a second webhook (TradingView timeout retry, or a
manual resend) from firing again while the first order is still in flight or
the shares are already held. Broker positions remain the source of truth after
a process restart — check GET /positions before resending a red webhook.
"""

import os
import re
import time
import logging
from threading import Lock

logger = logging.getLogger(__name__)

_SYMBOL_RE = re.compile(r"^[A-Z0-9]{1,10}(?:\.[A-Z]{1,2})?$")
# Yahoo-style venue tags. TradingView {{ticker}} is usually the bare symbol;
# strip these so BHP.AX does not get sent to IBKR as a different contract.
_VENUE_SUFFIXES = (".AX", ".US")


def parse_symbol(raw):
    """Turn a webhook symbol into an IBKR local symbol, or None if it is unusable.

    Accepts BHP, {{ticker}} after TradingView substitutes it, ASX:BHP, or
    BHP/AUD. A literal unsubstitued {{ticker}} is rejected.
    """
    if raw is None:
        return None
    text = str(raw).strip().upper()
    if not text or "{{" in text or "}}" in text:
        return None
    if ":" in text:
        text = text.split(":")[-1].strip()
    if "/" in text:
        text = text.split("/")[0].strip()
    for suffix in _VENUE_SUFFIXES:
        if text.endswith(suffix) and len(text) > len(suffix):
            text = text[: -len(suffix)]
            break
    if not _SYMBOL_RE.match(text):
        return None
    return text


def symbol_from_payload(data):
    """Pick a symbol from an IADSS webhook body.

    Same JSON as the crypto bot: {"pair":"BHP","token":"..."}.
    Also accepts {"ticker":"{{ticker}}"} once TradingView has substituted it.
    `pair` wins when it parses; otherwise ticker, then symbol.
    """
    if not isinstance(data, dict):
        return None
    for key in ("pair", "ticker", "symbol"):
        if key not in data:
            continue
        parsed = parse_symbol(data.get(key))
        if parsed:
            return parsed
    return None


class TickerState:
    def __init__(self):
        self.in_position = False
        self.buy_inflight = False
        self.sell_inflight = False
        self.cooldown_until = None


class SignalTracker:
    def __init__(self):
        self._states = {}
        self._lock = Lock()
        self.cooldown_seconds = int(os.environ.get("COOLDOWN_SECONDS", "0"))

    def _get(self, ticker):
        if ticker not in self._states:
            self._states[ticker] = TickerState()
        return self._states[ticker]

    def begin_buy(self, ticker):
        """Claim the buy, or explain why this webhook must not place another order."""
        with self._lock:
            state = self._get(ticker)
            now = time.time()
            if state.cooldown_until and now < state.cooldown_until:
                remaining = int(state.cooldown_until - now)
                logger.info("%s: buy ignored — cooldown (%ss remaining)", ticker, remaining)
                return {
                    "ok": False,
                    "status": "cooldown",
                    "ticker": ticker,
                    "cooldown_remaining": remaining,
                }
            if state.in_position:
                logger.info("%s: buy ignored — already long", ticker)
                return {"ok": False, "status": "ignored", "reason": "already_long", "ticker": ticker}
            if state.buy_inflight:
                logger.info("%s: buy ignored — order already in flight", ticker)
                return {"ok": False, "status": "ignored", "reason": "buy_in_flight", "ticker": ticker}
            state.buy_inflight = True
            return {"ok": True, "status": "accepted", "ticker": ticker}

    def begin_sell(self, ticker):
        with self._lock:
            state = self._get(ticker)
            if state.sell_inflight:
                logger.info("%s: sell ignored — order already in flight", ticker)
                return {"ok": False, "status": "ignored", "reason": "sell_in_flight", "ticker": ticker}
            # Do not trust in_position == False. After a restart the flag is
            # empty while IBKR may still hold the shares. The broker check in
            # close_long is the source of truth; this only blocks a double send.
            state.sell_inflight = True
            return {"ok": True, "status": "accepted", "ticker": ticker}

    def on_bought(self, ticker):
        with self._lock:
            state = self._get(ticker)
            state.buy_inflight = False
            state.in_position = True
            logger.info("%s: marked long", ticker)

    def on_buy_failed(self, ticker):
        with self._lock:
            state = self._get(ticker)
            state.buy_inflight = False
            logger.info("%s: buy in-flight cleared after failure", ticker)

    def mark_long(self, ticker):
        """Broker already holds the name. Sync the flag so retries stay ignored."""
        with self._lock:
            state = self._get(ticker)
            state.buy_inflight = False
            state.in_position = True

    def on_sold(self, ticker):
        with self._lock:
            state = self._get(ticker)
            state.sell_inflight = False
            state.buy_inflight = False
            state.in_position = False
            if self.cooldown_seconds > 0:
                state.cooldown_until = time.time() + self.cooldown_seconds
                logger.info("%s: cooldown set for %ss", ticker, self.cooldown_seconds)
            logger.info("%s: position closed", ticker)

    def on_sell_failed(self, ticker):
        with self._lock:
            state = self._get(ticker)
            state.sell_inflight = False
            logger.info("%s: sell in-flight cleared after failure", ticker)

    def mark_flat(self, ticker):
        with self._lock:
            state = self._get(ticker)
            state.sell_inflight = False
            state.in_position = False

    def on_stop_hit(self, ticker):
        self.on_sold(ticker)

    def status(self):
        with self._lock:
            result = {}
            now = time.time()
            for ticker, state in self._states.items():
                result[ticker] = {
                    "in_position": state.in_position,
                    "buy_inflight": state.buy_inflight,
                    "sell_inflight": state.sell_inflight,
                    "cooldown_until": state.cooldown_until,
                    "cooldown_remaining_s": (
                        max(0, int(state.cooldown_until - now)) if state.cooldown_until else 0
                    ),
                }
            return result
