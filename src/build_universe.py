import pandas as pd
from pathlib import Path

from src.config import BASE_DIR

NASDAQ_LISTED_URL = "https://www.nasdaqtrader.com/dynamic/symdir/nasdaqlisted.txt"
OTHER_LISTED_URL = "https://www.nasdaqtrader.com/dynamic/symdir/otherlisted.txt"

UNIVERSE_DIR = BASE_DIR / "data" / "raw" / "universe"
OUTPUT_PATH = UNIVERSE_DIR / "universe_us_auto.csv"


def load_nasdaq_listed() -> pd.DataFrame:
    df = pd.read_csv(NASDAQ_LISTED_URL, sep="|")

    df = df[df["Symbol"].notna()]
    df = df[df["Test Issue"] == "N"]
    df = df[df["ETF"] == "N"]

    df = df.rename(columns={
        "Symbol": "ticker",
        "Security Name": "company_name"
    })

    df["exchange"] = "NASDAQ"
    df["country"] = "US"
    df["enabled"] = "TRUE"

    return df[["ticker", "company_name", "exchange", "country", "enabled"]]


def load_other_listed() -> pd.DataFrame:
    df = pd.read_csv(OTHER_LISTED_URL, sep="|")

    df = df[df["ACT Symbol"].notna()]
    df = df[df["Test Issue"] == "N"]
    df = df[df["ETF"] == "N"]

    exchange_map = {
        "N": "NYSE",
        "A": "AMEX",
        "P": "NYSE_ARCA",
        "Z": "CBOE",
        "V": "IEXG",
    }

    df["exchange"] = df["Exchange"].map(exchange_map).fillna(df["Exchange"])

    df = df.rename(columns={
        "ACT Symbol": "ticker",
        "Security Name": "company_name"
    })

    df["country"] = "US"
    df["enabled"] = "TRUE"

    return df[["ticker", "company_name", "exchange", "country", "enabled"]]


def clean_universe(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()

    bad_terms = [
        "Warrant",
        "Right",
        "Unit",
        "Preferred",
        "Depositary",
        "Notes",
        "Bond",
        "ETF",
        "Fund",
    ]

    pattern = "|".join(bad_terms)
    df = df[~df["company_name"].str.contains(pattern, case=False, na=False)]

    df["ticker"] = df["ticker"].astype(str).str.strip()
    df = df[~df["ticker"].str.contains(r"\$", regex=True)]
    df = df.drop_duplicates(subset=["ticker"])

    return df.sort_values(["exchange", "ticker"])


def main():
    UNIVERSE_DIR.mkdir(parents=True, exist_ok=True)

    nasdaq = load_nasdaq_listed()
    other = load_other_listed()

    universe = pd.concat([nasdaq, other], ignore_index=True)
    universe = clean_universe(universe)

    universe.to_csv(OUTPUT_PATH, index=False)

    print(f"US universe created: {OUTPUT_PATH}")
    print(f"Total tickers: {len(universe)}")
    print(universe["exchange"].value_counts())


if __name__ == "__main__":
    main()