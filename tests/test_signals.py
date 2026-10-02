"""Parser and in-flight guard. No IBKR connection."""

import unittest

from signal_tracker import SignalTracker, parse_symbol, symbol_from_payload


class ParseSymbolTests(unittest.TestCase):
    def test_bare_asx_code(self):
        self.assertEqual(parse_symbol("BHP"), "BHP")
        self.assertEqual(parse_symbol("  bhp "), "BHP")

    def test_tradingview_exchange_prefix(self):
        self.assertEqual(parse_symbol("ASX:BHP"), "BHP")
        self.assertEqual(parse_symbol("NASDAQ:BRK.B"), "BRK.B")

    def test_pair_with_quote(self):
        self.assertEqual(parse_symbol("BHP/AUD"), "BHP")

    def test_yahoo_suffix(self):
        self.assertEqual(parse_symbol("BHP.AX"), "BHP")

    def test_unsubstituted_placeholder(self):
        self.assertIsNone(parse_symbol("{{ticker}}"))
        self.assertIsNone(parse_symbol(""))
        self.assertIsNone(parse_symbol(None))

    def test_rejects_junk(self):
        self.assertIsNone(parse_symbol("../etc/passwd"))
        self.assertIsNone(parse_symbol("BHP;DROP"))


class PayloadTests(unittest.TestCase):
    def test_iadss_pair_json(self):
        self.assertEqual(
            symbol_from_payload({"pair": "BHP", "token": "secret"}),
            "BHP",
        )

    def test_ticker_placeholder_after_substitution(self):
        self.assertEqual(
            symbol_from_payload({"ticker": "CBA", "token": "secret"}),
            "CBA",
        )

    def test_pair_wins_over_ticker(self):
        self.assertEqual(
            symbol_from_payload({"pair": "BHP", "ticker": "CBA"}),
            "BHP",
        )

    def test_falls_through_unsubstituted_pair(self):
        self.assertEqual(
            symbol_from_payload({"pair": "{{ticker}}", "ticker": "WBC"}),
            "WBC",
        )


class TrackerTests(unittest.TestCase):
    def test_second_buy_while_in_flight_is_ignored(self):
        tracker = SignalTracker()
        tracker.cooldown_seconds = 0
        first = tracker.begin_buy("BHP")
        second = tracker.begin_buy("BHP")
        self.assertTrue(first["ok"])
        self.assertEqual(second["reason"], "buy_in_flight")

    def test_failed_buy_can_be_retried(self):
        tracker = SignalTracker()
        tracker.cooldown_seconds = 0
        tracker.begin_buy("CBA")
        tracker.on_buy_failed("CBA")
        self.assertTrue(tracker.begin_buy("CBA")["ok"])

    def test_filled_buy_blocks_another(self):
        tracker = SignalTracker()
        tracker.cooldown_seconds = 0
        tracker.begin_buy("BHP")
        tracker.on_bought("BHP")
        again = tracker.begin_buy("BHP")
        self.assertEqual(again["reason"], "already_long")

    def test_sell_in_flight_blocks_a_second_sell(self):
        tracker = SignalTracker()
        tracker.begin_sell("BHP")
        second = tracker.begin_sell("BHP")
        self.assertEqual(second["reason"], "sell_in_flight")
        tracker.on_sell_failed("BHP")
        self.assertTrue(tracker.begin_sell("BHP")["ok"])

    def test_cooldown_after_close(self):
        tracker = SignalTracker()
        tracker.cooldown_seconds = 60
        tracker.begin_sell("BHP")
        tracker.on_sold("BHP")
        claim = tracker.begin_buy("BHP")
        self.assertEqual(claim["status"], "cooldown")
        self.assertGreater(claim["cooldown_remaining"], 0)

    def test_other_ticker_is_independent(self):
        tracker = SignalTracker()
        tracker.cooldown_seconds = 0
        tracker.begin_buy("BHP")
        tracker.on_bought("BHP")
        self.assertTrue(tracker.begin_buy("CBA")["ok"])


if __name__ == "__main__":
    unittest.main()
