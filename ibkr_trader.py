import os, time, logging, pytz
from datetime import datetime, time as dtime
from threading import Lock
from ib_insync import IB, Stock, MarketOrder, LimitOrder, util

logger = logging.getLogger(__name__)

IB_HOST                  = os.environ.get("IB_HOST", "ib-gateway")
IB_PORT                  = int(os.environ.get("IB_PORT", "4004"))
IB_CLIENT_ID             = int(os.environ.get("IB_CLIENT_ID", "1"))
POSITION_SIZE_PCT        = float(os.environ.get("POSITION_SIZE_PCT", "0.10"))
MAX_POSITION_SIZE_PCT    = float(os.environ.get("MAX_POSITION_SIZE_PCT", "0.50"))
SELL_MODE                = os.environ.get("SELL_MODE", "close_long")
SELL_PCT                 = float(os.environ.get("SELL_PCT", "1.0"))
EXCHANGE                 = os.environ.get("EXCHANGE", "SMART")
CURRENCY                 = os.environ.get("CURRENCY", "USD")
BULL_TICKER              = os.environ.get("BULL_TICKER", "")
BEAR_TICKER              = os.environ.get("BEAR_TICKER", "")
MARKET_TZ                = pytz.timezone(os.environ.get("MARKET_TZ", "America/New_York"))
_oh, _om                 = map(int, os.environ.get("MARKET_OPEN",  "09:30").split(":"))
_ch, _cm                 = map(int, os.environ.get("MARKET_CLOSE", "16:00").split(":"))
MARKET_OPEN_TIME         = dtime(_oh, _om)
MARKET_CLOSE_TIME        = dtime(_ch, _cm)
ORDER_TYPE               = os.environ.get("ORDER_TYPE", "market").lower()
LIMIT_SLIPPAGE_PCT       = float(os.environ.get("LIMIT_SLIPPAGE_PCT", "0.005"))
PAIRED_ETF_MAX_ROTATIONS = int(os.environ.get("PAIRED_ETF_MAX_ROTATIONS", "1"))


class IBKRTrader:
    def __init__(self):
        util.startLoop()
        self.ib   = IB()
        self.lock = Lock()
        self.ib.errorEvent += self._on_ib_error
        self._connect()

    def _on_ib_error(self, req_id, error_code, error_string, contract):
        if 2100 <= error_code <= 2109:
            logger.debug("IB info %d: %s", error_code, error_string)
        elif error_code in (1100, 1101, 1102):
            logger.warning("IB connectivity %d: %s", error_code, error_string)
        else:
            logger.error("IB error req=%s code=%d: %s contract=%s",
                         req_id, error_code, error_string, contract)

    def _connect(self, retries=10, delay=15):
        for attempt in range(1, retries + 1):
            try:
                if self.ib.isConnected():
                    return
                logger.info("Connecting to IB Gateway %s:%s (attempt %d/%d)",
                            IB_HOST, IB_PORT, attempt, retries)
                self.ib.connect(IB_HOST, IB_PORT, clientId=IB_CLIENT_ID, timeout=30)
                logger.info("Connected to IB Gateway")
                return
            except Exception as exc:
                logger.error("Connection failed: %s", exc)
                if attempt < retries:
                    time.sleep(delay)
        raise ConnectionError("Could not connect after %d attempts" % retries)

    def _ensure_connected(self):
        if not self.ib.isConnected():
            logger.warning("IB Gateway disconnected -- reconnecting")
            self._connect()

    def is_connected(self):
        return self.ib.isConnected()

    @staticmethod
    def is_market_hours():
        now      = datetime.now(MARKET_TZ)
        now_time = now.time().replace(second=0, microsecond=0)
        if now.weekday() >= 5:
            return False
        return MARKET_OPEN_TIME <= now_time <= MARKET_CLOSE_TIME

    def _check_hours(self, ticker):
        if ORDER_TYPE == "limit":
            return
        if not self.is_market_hours():
            raise ValueError(
                "Market closed -- cannot trade %s "
                "(set ORDER_TYPE=limit to enable extended hours trading)" % ticker
            )

    def _make_order(self, action, shares, price):
        if ORDER_TYPE == "limit":
            if action == "BUY":
                limit_price = round(price * (1 + LIMIT_SLIPPAGE_PCT), 4)
            else:
                limit_price = round(price * (1 - LIMIT_SLIPPAGE_PCT), 4)
            order = LimitOrder(action, shares, limit_price)
            order.outsideRth = True
            logger.info("Order type: LIMIT %s %d @ %.4f (slippage=%.2f pct outsideRth=True)",
                        action, shares, limit_price, LIMIT_SLIPPAGE_PCT * 100)
            return order
        logger.info("Order type: MARKET %s %d", action, shares)
        return MarketOrder(action, shares)

    def get_equity(self):
        with self.lock:
            self._ensure_connected()
            return self._equity()

    def get_buying_power(self):
        with self.lock:
            self._ensure_connected()
            return self._buying_power()

    def get_all_positions(self):
        with self.lock:
            self._ensure_connected()
            return self._all_positions()

    def get_position(self, ticker):
        with self.lock:
            self._ensure_connected()
            return self._position(ticker)

    def get_price(self, ticker):
        with self.lock:
            self._ensure_connected()
            return self._price(ticker)

    def execute_buy(self, ticker):
        if SELL_MODE == "paired_etf":
            results = []
            if self.get_position(BEAR_TICKER) > 0:
                logger.info("paired_etf: closing %.0f pct of bear (%s) before buying bull",
                            SELL_PCT * 100, BEAR_TICKER)
                results.append(self._close(BEAR_TICKER, SELL_PCT))
                time.sleep(1)
            results.append(self._open(BULL_TICKER))
            return {"sell_mode": "paired_etf", "actions": results}
        return self.buy(ticker)

    def execute_sell(self, ticker):
        if SELL_MODE == "paired_etf":
            # If already holding bear and max rotations reached, skip
            bear_pos = self.get_position(BEAR_TICKER)
            if bear_pos > 0 and PAIRED_ETF_MAX_ROTATIONS <= 1:
                logger.info(
                    "paired_etf: already holding %s (%.0f shares), "
                    "PAIRED_ETF_MAX_ROTATIONS=%d -- ignoring sell signal",
                    BEAR_TICKER, bear_pos, PAIRED_ETF_MAX_ROTATIONS,
                )
                return {"sell_mode": "paired_etf", "skipped": "already_in_bear", "actions": []}
            results = []
            if self.get_position(BULL_TICKER) > 0:
                logger.info("paired_etf: closing %.0f pct of bull (%s) before buying bear",
                            SELL_PCT * 100, BULL_TICKER)
                results.append(self._close(BULL_TICKER, SELL_PCT))
                time.sleep(1)
            results.append(self._open(BEAR_TICKER))
            return {"sell_mode": "paired_etf", "actions": results}
        if SELL_MODE == "close_long":
            return {"sell_mode": "close_long", "actions": [self.close_long(ticker)]}
        if SELL_MODE == "short":
            results = []
            if self.get_position(ticker) > 0:
                results.append(self.close_long(ticker))
                time.sleep(1)
            results.append(self.open_short(ticker))
            return {"sell_mode": "short", "actions": results}
        if SELL_MODE == "open_short":
            return {"sell_mode": "open_short", "actions": [self.open_short(ticker)]}
        raise ValueError("Unknown SELL_MODE: %s" % SELL_MODE)

    def buy(self, ticker):
        self._check_hours(ticker)
        with self.lock:
            self._ensure_connected()
            pos = self._position(ticker)
            if pos > 0:
                raise ValueError("Already long %s (%.0f shares)" % (ticker, pos))
            return self._open_locked(ticker)

    def close_long(self, ticker, pct=None):
        if pct is None:
            pct = SELL_PCT
        self._check_hours(ticker)
        with self.lock:
            self._ensure_connected()
            if self._position(ticker) <= 0:
                raise ValueError("No long position in %s" % ticker)
            return self._close_locked(ticker, pct)

    def open_short(self, ticker):
        self._check_hours(ticker)
        with self.lock:
            self._ensure_connected()
            equity = self._equity()
            price  = self._price(ticker)
            if not price or price <= 0:
                raise ValueError("Cannot get price for %s" % ticker)
            shares = int(min(equity * POSITION_SIZE_PCT,
                             equity * MAX_POSITION_SIZE_PCT) / price)
            if shares < 1:
                raise ValueError("Insufficient equity to short %s" % ticker)
            contract = self._contract(ticker)
            self.ib.placeOrder(contract, self._make_order("SELL", shares, price))
            time.sleep(2)
            logger.info("SHORT %d x %s @ ~%.4f", shares, ticker, price)
            return {"action": "open_short", "ticker": ticker,
                    "shares": shares, "price": price}

    def _open(self, ticker):
        self._check_hours(ticker)
        with self.lock:
            self._ensure_connected()
            return self._open_locked(ticker)

    def _close(self, ticker, pct=1.0):
        self._check_hours(ticker)
        with self.lock:
            self._ensure_connected()
            if self._position(ticker) <= 0:
                raise ValueError("No long position in %s" % ticker)
            return self._close_locked(ticker, pct)

    def _open_locked(self, ticker):
        logger.info("[_open_locked] getting equity...")
        equity       = self._equity()
        logger.info("[_open_locked] equity=%.2f, getting buying power...", equity)
        buying_power = self._buying_power()
        logger.info("[_open_locked] buying_power=%.2f, getting price for %s...", buying_power, ticker)
        dollar_amt   = min(equity * POSITION_SIZE_PCT,
                           equity * MAX_POSITION_SIZE_PCT,
                           buying_power * 0.99)
        price = self._price(ticker)
        if not price or price <= 0:
            raise ValueError("Cannot get price for %s" % ticker)
        shares = int(dollar_amt / price)
        if shares < 1:
            raise ValueError("Insufficient buying power for %s (price=%.4f available=%.2f)"
                             % (ticker, price, buying_power))
        contract = self._contract(ticker)
        logger.info("[_open_locked] placing BUY order: %d x %s @ ~%.4f", shares, ticker, price)
        self.ib.placeOrder(contract, self._make_order("BUY", shares, price))
        time.sleep(2)
        logger.info("BUY %d x %s @ ~%.4f (%.2f %s notional)",
                    shares, ticker, price, shares * price, CURRENCY)
        return {"action": "buy", "ticker": ticker, "shares": shares, "price": price}

    def _close_locked(self, ticker, pct=1.0):
        pos    = self._position(ticker)
        shares = max(1, int(pos * pct))
        price  = self._price(ticker)
        self.ib.placeOrder(self._contract(ticker), self._make_order("SELL", shares, price))
        time.sleep(2)
        logger.info("SELL %d x %s (closed %d pct)", shares, ticker, int(pct * 100))
        return {"action": "close_long", "ticker": ticker, "shares": shares}

    def _contract(self, ticker):
        # qualifyContracts is optional for SMART-routed US stocks and can hang
        # if the Sec-def farm is unresponsive — skip it entirely
        return Stock(ticker, EXCHANGE, CURRENCY)

    def _equity(self):
        for v in self.ib.accountValues():
            if v.tag == "NetLiquidation" and v.currency == CURRENCY:
                return float(v.value)
        for v in self.ib.accountValues():
            if v.tag == "NetLiquidation":
                return float(v.value)
        return 0.0

    def _buying_power(self):
        for v in self.ib.accountValues():
            if v.tag == "BuyingPower" and v.currency == CURRENCY:
                return float(v.value)
        for v in self.ib.accountValues():
            if v.tag == "BuyingPower":
                return float(v.value)
        return 0.0

    def _position(self, ticker):
        for pos in self.ib.positions():
            if getattr(pos.contract, "symbol", None) == ticker:
                return pos.position
        return 0.0

    def _all_positions(self):
        return [
            {
                "symbol":       pos.contract.symbol,
                "exchange":     getattr(pos.contract, "exchange", ""),
                "position":     pos.position,
                "avg_cost":     pos.avgCost,
                "market_value": round(pos.position * pos.avgCost, 2),
                "currency":     getattr(pos.contract, "currency", ""),
            }
            for pos in self.ib.positions()
            if pos.position != 0
        ]

    def _price(self, ticker):
        contract = self._contract(ticker)
        # Use time.sleep (not ib.sleep) so the event loop thread stays free to process responses
        # Try live data first (type 1), fall back to delayed (type 3) for paper/no-subscription accounts
        for mkt_data_type, label in ((1, "live"), (3, "delayed")):
            try:
                logger.info("[_price] requesting %s market data snapshot for %s", label, ticker)
                self.ib.reqMarketDataType(mkt_data_type)
                td = self.ib.reqMktData(contract, "", True, False)
                time.sleep(10)
                price = td.last or td.close or td.bid or td.ask
                self.ib.cancelMktData(contract)
                logger.info("[_price] %s snapshot for %s: last=%s close=%s bid=%s ask=%s",
                            label, ticker, td.last, td.close, td.bid, td.ask)
                if price and price > 0:
                    logger.info("Price for %s via %s: %.4f", ticker, label, price)
                    return float(price)
                logger.warning("[_price] %s snapshot returned no price for %s -- trying next type", label, ticker)
            except Exception as exc:
                logger.warning("[_price] %s reqMktData failed for %s: %s", label, ticker, exc)
        logger.error("Could not get price for %s after trying live and delayed data", ticker)
        return None
