import os
import time
import logging

logger = logging.getLogger(__name__)

TRAILING_STOP_PCT = float(os.environ.get("TRAILING_STOP_PCT", "0.05"))
MONITOR_INTERVAL  = int(os.environ.get("MONITOR_INTERVAL_SECONDS", "30"))


class PositionMonitor:
    def __init__(self, trader, tracker, notifier):
        self.trader   = trader
        self.tracker  = tracker
        self.notifier = notifier
        self._peaks   = {}

    def on_entry(self, ticker, price):
        self._peaks[ticker] = price
        logger.info("%s: trailing stop baseline @ %.4f", ticker, price)

    def on_exit(self, ticker):
        self._peaks.pop(ticker, None)

    def run(self):
        logger.info("Position monitor started (interval=%ds trail=%.1f pct)",
                    MONITOR_INTERVAL, TRAILING_STOP_PCT * 100)
        while True:
            try:
                self._tick()
            except Exception as exc:
                logger.error("Monitor tick error: %s", exc, exc_info=True)
            time.sleep(MONITOR_INTERVAL)

    def _tick(self):
        if TRAILING_STOP_PCT <= 0:
            return
        if not self.trader.is_market_hours():
            return
        if not self.trader.is_connected():
            return

        for pos in self.trader.get_all_positions():
            ticker = pos["symbol"]
            qty    = pos["position"]
            if qty <= 0:
                continue

            price = self.trader.get_price(ticker)
            if not price or price <= 0:
                logger.warning("%s: could not get price -- skipping", ticker)
                continue

            peak      = max(self._peaks.get(ticker, price), price)
            self._peaks[ticker] = peak
            stop_price = peak * (1 - TRAILING_STOP_PCT)

            logger.debug("%s: price=%.4f peak=%.4f stop=%.4f", ticker, price, peak, stop_price)

            if price <= stop_price:
                logger.info("%s: TRAILING STOP HIT price=%.4f stop=%.4f", ticker, price, stop_price)
                self.notifier.send(
                    "IADSS IBKR TRAILING STOP: %s price=%.4f stop=%.4f peak=%.4f"
                    % (ticker, price, stop_price, peak)
                )
                try:
                    result = self.trader.close_long(ticker)
                    self.tracker.on_stop_hit(ticker)
                    self.on_exit(ticker)
                    self.notifier.send(
                        "IADSS IBKR stop closed: %s (%s shares)"
                        % (ticker, result.get("shares"))
                    )
                except Exception as exc:
                    logger.error("%s: stop close failed: %s", ticker, exc)
                    self.notifier.send("IADSS IBKR stop close FAILED: %s -- %s" % (ticker, exc))
