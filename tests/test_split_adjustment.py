from datetime import date
import unittest

import pandas as pd

from src.split_adjustment import SplitEvent, SplitHistory


def _frame(rows):
    return pd.DataFrame(rows)


class SplitHistoryTests(unittest.TestCase):
    def test_factor_multiplies_only_later_events(self):
        history = SplitHistory([
            SplitEvent(date(2020, 1, 15), 0.1),
            SplitEvent(date(2024, 6, 1), 0.5),
        ])

        self.assertAlmostEqual(history.factor_after(date(2019, 12, 31)), 0.05)
        self.assertAlmostEqual(history.factor_after(date(2020, 6, 30)), 0.5)
        self.assertAlmostEqual(history.factor_after(date(2025, 1, 1)), 1.0)
        # The boundary itself is already reflected in the count.
        self.assertAlmostEqual(history.factor_after(date(2020, 1, 15)), 0.5)

    def test_forward_and_reverse_splits_compose(self):
        history = SplitHistory([
            SplitEvent(date(2021, 3, 1), 4.0),
            SplitEvent(date(2023, 3, 1), 0.25),
        ])

        self.assertAlmostEqual(history.factor_after(date(2020, 1, 1)), 1.0)
        self.assertAlmostEqual(history.factor_after(date(2022, 1, 1)), 0.25)

    def test_empty_history_is_neutral(self):
        self.assertEqual(SplitHistory().factor_after(date(2020, 1, 1)), 1.0)
        self.assertEqual(SplitHistory([]).factor_after(None), 1.0)
        self.assertFalse(SplitHistory().has_events)

    def test_degenerate_ratios_are_ignored(self):
        history = SplitHistory([
            SplitEvent(date(2020, 1, 1), 0.0),
            SplitEvent(date(2020, 2, 1), 1.0),
            SplitEvent(date(2020, 3, 1), float("nan")),
            SplitEvent(date(2020, 4, 1), -2.0),
            SplitEvent(date(2020, 5, 1), 0.5),
        ])

        self.assertEqual(len(history.events), 1)
        self.assertAlmostEqual(history.factor_after(date(2019, 1, 1)), 0.5)

    def test_reads_events_from_a_price_frame(self):
        history = SplitHistory.from_price_history(_frame([
            {"Date": pd.Timestamp("2024-01-02"), "Close": 1.0, "Stock Splits": 0.0},
            {"Date": pd.Timestamp("2024-03-15"), "Close": 20.0, "Stock Splits": 0.05},
            {"Date": pd.Timestamp("2024-06-01"), "Close": 18.0, "Stock Splits": 0.0},
        ]))

        self.assertEqual(len(history.events), 1)
        self.assertEqual(history.events[0].on, date(2024, 3, 15))
        self.assertAlmostEqual(history.factor_after(date(2024, 1, 31)), 0.05)

    def test_frames_without_the_column_or_rows_are_empty(self):
        self.assertFalse(SplitHistory.from_price_history(None).has_events)
        self.assertFalse(SplitHistory.from_price_history(pd.DataFrame()).has_events)
        self.assertFalse(SplitHistory.from_price_history(
            _frame([{"Date": pd.Timestamp("2024-01-02"), "Close": 1.0}])
        ).has_events)

    def test_ambiguous_window_is_detected(self):
        """A split between a period end and its filing may or may not be
        restated in the filing, and the extract does not say which."""
        history = SplitHistory([SplitEvent(date(2026, 8, 14), 0.0333)])

        self.assertTrue(
            history.has_event_between(date(2026, 6, 30), date(2026, 9, 1))
        )
        self.assertFalse(
            history.has_event_between(date(2026, 6, 30), date(2026, 8, 1))
        )
        self.assertFalse(
            history.has_event_between(date(2026, 9, 1), date(2026, 11, 1))
        )
        self.assertFalse(history.has_event_between(None, date(2026, 9, 1)))


if __name__ == "__main__":
    unittest.main()
