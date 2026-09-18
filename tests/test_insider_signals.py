from datetime import date
import unittest

from src.insider_signals import (
    InsiderHistory,
    SIGNAL_NAMES,
    build_insider_index,
    quarter_end,
)


def _tx(filed, kind, shares, price, owners, officer=True, issuer="0000001234"):
    return {
        "issuer_cik": issuer, "filed": filed, "kind": kind, "shares": shares,
        "price": price, "value": shares * price if price is not None else None,
        "owner_ciks": owners, "officer_or_director": officer,
    }


TRANSACTIONS = [
    _tx("2025-01-10", "purchase", 1000, 2.0, ["A"]),
    _tx("2025-03-01", "purchase", 500, 2.0, ["B"]),
    _tx("2025-04-15", "sale", 300, 3.0, ["A"]),
    _tx("2025-05-20", "purchase", 100, 2.5, ["C"], officer=False),
    _tx("2024-01-05", "purchase", 5000, 1.0, ["A"]),  # too old for both windows
    _tx("2025-06-30", "purchase", 100, 2.0, ["D"]),   # filed after the as-of date
]


class SignalTests(unittest.TestCase):
    def setUp(self):
        self.history = InsiderHistory(TRANSACTIONS, data_through=date(2025, 6, 30))

    def test_windows_count_only_filings_public_on_the_date(self):
        signals = self.history.signals_as_of(date(2025, 6, 1), market_cap=1_000_000)

        self.assertEqual(signals["ins_purchase_count_short"], 3.0)
        self.assertEqual(signals["ins_sale_count_short"], 1.0)
        self.assertEqual(signals["ins_net_count_short"], 2.0)
        self.assertEqual(signals["ins_distinct_buyers_short"], 3.0)
        self.assertEqual(signals["ins_officer_purchase_count_short"], 2.0)
        self.assertEqual(signals["ins_purchase_value_short"], 2000 + 1000 + 250)
        self.assertEqual(signals["ins_net_value_short"], 3250 - 900)
        self.assertAlmostEqual(signals["ins_net_value_to_market_cap_short"], 2350 / 1_000_000, places=9)
        self.assertEqual(signals["ins_cluster_buy_short"], 1.0)
        self.assertEqual(signals["ins_purchase_count_long"], 3.0)
        self.assertEqual(signals["ins_days_since_last_purchase"], 12.0)
        self.assertEqual(signals["ins_data_through"], "2025-06-30")

    def test_short_window_excludes_older_filings(self):
        signals = self.history.signals_as_of(date(2025, 8, 1))
        # 180 days before 2025-08-01 is 2025-02-02: the January purchase drops out.
        self.assertEqual(signals["ins_purchase_count_short"], 3.0)  # Mar, May, Jun
        self.assertEqual(signals["ins_purchase_count_long"], 4.0)
        self.assertIsNone(signals["ins_net_value_to_market_cap_short"])
        self.assertEqual(signals["ins_cluster_buy_short"], 1.0)

    def test_single_buyer_is_not_a_cluster(self):
        history = InsiderHistory([
            _tx("2025-02-01", "purchase", 100, 1.0, ["A"]),
            _tx("2025-03-01", "purchase", 100, 1.0, ["A"]),
        ])
        signals = history.signals_as_of(date(2025, 4, 1))
        self.assertEqual(signals["ins_distinct_buyers_short"], 1.0)
        self.assertEqual(signals["ins_cluster_buy_short"], 0.0)

    def test_dates_far_past_data_end_yield_nothing(self):
        signals = self.history.signals_as_of(date(2027, 1, 1), market_cap=1.0)
        self.assertIsNone(signals["ins_purchase_count_short"])
        self.assertEqual(signals["ins_data_through"], "2025-06-30")

    def test_minimum_transaction_value_filter(self):
        history = InsiderHistory(TRANSACTIONS, {"minimum_transaction_value": 500})
        signals = history.signals_as_of(date(2025, 6, 1))
        self.assertEqual(signals["ins_purchase_count_short"], 2.0)  # the $250 buy drops

    def test_empty_history(self):
        history = InsiderHistory([])
        self.assertFalse(history.has_data)
        signals = history.signals_as_of(date(2025, 1, 1))
        self.assertEqual(set(signals), set(SIGNAL_NAMES))
        self.assertTrue(all(value is None for value in signals.values()))


class IndexTests(unittest.TestCase):
    def test_groups_by_issuer_and_pads_cik(self):
        index = build_insider_index(
            TRANSACTIONS + [_tx("2025-02-02", "purchase", 1, 1.0, ["Z"], issuer="99")],
            data_through=date(2025, 6, 30),
        )
        self.assertEqual(sorted(index), ["0000000099", "0000001234"])
        self.assertTrue(index["0000000099"].has_data)
        self.assertEqual(index["0000001234"].data_through, date(2025, 6, 30))

    def test_quarter_end(self):
        self.assertEqual(quarter_end("2025q1"), date(2025, 3, 31))
        self.assertEqual(quarter_end("2025q4"), date(2025, 12, 31))
        self.assertEqual(quarter_end("2024q2"), date(2024, 6, 30))


if __name__ == "__main__":
    unittest.main()
