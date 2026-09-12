import unittest

import pandas as pd

from src.build_canadian_universe import (
    clean_canadian_universe,
    find_prefixed_column,
    source_date_from_column,
)


class CanadianUniverseBuilderTests(unittest.TestCase):
    def test_dated_metric_columns_are_not_tied_to_one_month(self):
        frame = pd.DataFrame({
            "Market Cap (C$)\n31-July-2026": [25_000_000],
            "O/S Shares 31-July-2026": [10_000_000],
        })

        column = find_prefixed_column(frame, "Market Cap (C$)")

        self.assertEqual(column, "Market Cap (C$)\n31-July-2026")
        self.assertEqual(source_date_from_column(column), "2026-07-31")

    def test_cleaning_preserves_dynamic_source_date(self):
        frame = pd.DataFrame([{
            "ticker": "ABC.TO", "root_ticker": "ABC",
            "company_name": "ABC Mining Inc.", "exchange": "TSX",
            "country": "CA", "sector": "Mining", "asset_type": "Equity",
            "market_cap_source_cad": 25_000_000,
            "source_date": "2026-07-31",
        }])

        result = clean_canadian_universe(frame)

        self.assertEqual(len(result), 1)
        self.assertEqual(result.iloc[0]["source_date"], "2026-07-31")


if __name__ == "__main__":
    unittest.main()
