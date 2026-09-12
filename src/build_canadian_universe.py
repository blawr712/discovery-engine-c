from __future__ import annotations

import re
from pathlib import Path

import pandas as pd

from src.config import BASE_DIR


INPUT_DIR = BASE_DIR / "data" / "raw" / "canada"
UNIVERSE_DIR = BASE_DIR / "data" / "raw" / "universe"

DEFAULT_INPUT_FILE = INPUT_DIR / "tsx-and-tsxv-listed-companies.xlsx"
OUTPUT_FILE = UNIVERSE_DIR / "universe_ca_auto.csv"

MIN_SOURCE_MARKET_CAP = 10_000_000
MAX_SOURCE_MARKET_CAP = 1_000_000_000

EXCLUDED_SECTORS = {
    "ETP",
    "CDR",
    "Closed-End Funds",
    "SPAC",
    "CPC",
}

EXCLUDED_NAME_TERMS = [
    r"\bETF\b",
    r"\bETN\b",
    r"\bfund\b",
    r"\bclosed[- ]end\b",
    r"\bwarrant\b",
    r"\brights?\b",
    r"\bdebenture\b",
    r"\bpreferred\b",
    r"\bdepositary receipt\b",
    r"\bsubscription receipt\b",
    r"\bcapital pool\b",
]

OUTPUT_COLUMNS = [
    "ticker",
    "root_ticker",
    "company_name",
    "exchange",
    "country",
    "sector",
    "sub_sector",
    "technology_sub_sector",
    "life_sciences_sub_sector",
    "consumer_sub_sector",
    "clean_technology_sub_sector",
    "real_estate_sub_sector",
    "hq_location",
    "hq_region",
    "listing_type",
    "listing_date",
    "interlisted",
    "otc_listing",
    "former_cpc",
    "trust",
    "market_cap_source_cad",
    "shares_outstanding_source",
    "volume_ytd_source",
    "value_traded_ytd_source_cad",
    "trades_ytd_source",
    "source",
    "source_date",
    "enabled",
]


def normalize_column_name(value: object) -> str:
    text = str(value).strip().lower()
    text = text.replace("\n", " ")
    text = re.sub(r"\s+", " ", text)
    return text


def find_header_row(
    workbook_path: Path,
    sheet_name: str,
    max_rows_to_scan: int = 25,
) -> int:
    preview = pd.read_excel(
        workbook_path,
        sheet_name=sheet_name,
        header=None,
        nrows=max_rows_to_scan,
    )

    for row_index, row in preview.iterrows():
        normalized_values = {
            normalize_column_name(value)
            for value in row.dropna().tolist()
        }

        has_exchange = "exchange" in normalized_values
        has_name = "name" in normalized_values
        has_ticker = any("root ticker" in value for value in normalized_values)

        if has_exchange and has_name and has_ticker:
            return int(row_index)

    raise ValueError(
        f"Could not locate the header row in worksheet: {sheet_name}"
    )


def find_column(
    df: pd.DataFrame,
    *possible_names: str,
) -> str | None:
    normalized_lookup = {
        normalize_column_name(column): column
        for column in df.columns
    }

    for possible_name in possible_names:
        normalized_name = normalize_column_name(possible_name)

        if normalized_name in normalized_lookup:
            return normalized_lookup[normalized_name]

    return None


def find_prefixed_column(
    df: pd.DataFrame,
    *possible_prefixes: str,
) -> object | None:
    """Find a TMX metric column while allowing its trailing as-of date to vary."""
    normalized_columns = [
        (normalize_column_name(column), column) for column in df.columns
    ]
    for possible_prefix in possible_prefixes:
        prefix = normalize_column_name(possible_prefix)
        for normalized, original in normalized_columns:
            if normalized == prefix or normalized.startswith(f"{prefix} "):
                return original
    return None


def get_prefixed_series(
    df: pd.DataFrame,
    *possible_prefixes: str,
    default: object = pd.NA,
) -> tuple[pd.Series, object | None]:
    column = find_prefixed_column(df, *possible_prefixes)
    if column is None:
        return pd.Series(default, index=df.index), None
    return df[column], column


def source_date_from_column(column: object | None) -> str | None:
    """Extract an ISO source date from a dated TMX metric header."""
    if column is None:
        return None
    match = re.search(
        r"\b\d{1,2}-[A-Za-z]+-\d{4}\b",
        str(column).replace("\n", " "),
    )
    if match is None:
        return None
    parsed = pd.to_datetime(match.group(0), format="%d-%B-%Y", errors="coerce")
    return None if pd.isna(parsed) else parsed.date().isoformat()


def get_series(
    df: pd.DataFrame,
    *possible_names: str,
    default: object = pd.NA,
) -> pd.Series:
    column = find_column(df, *possible_names)

    if column is None:
        return pd.Series(default, index=df.index)

    return df[column]


def clean_text(series: pd.Series) -> pd.Series:
    return (
        series.astype("string")
        .str.strip()
        .replace(
            {
                "": pd.NA,
                "nan": pd.NA,
                "None": pd.NA,
                "<NA>": pd.NA,
            }
        )
    )


def combine_text_columns(*series_list: pd.Series) -> pd.Series:
    if not series_list:
        raise ValueError("At least one series is required.")

    result = clean_text(series_list[0])

    for series in series_list[1:]:
        candidate = clean_text(series)
        result = result.fillna(candidate)

    return result


def create_yahoo_ticker(
    root_ticker: str,
    exchange: str,
) -> str:
    root = str(root_ticker).strip().upper()

    if exchange == "TSX":
        return f"{root}.TO"

    if exchange == "TSXV":
        return f"{root}.V"

    return root


def classify_asset_type(
    company_name: object,
    sector: object,
    trust_flag: object,
) -> str:
    name = "" if pd.isna(company_name) else str(company_name).strip()
    sector_value = "" if pd.isna(sector) else str(sector).strip()
    trust_value = "" if pd.isna(trust_flag) else str(trust_flag).strip()

    if sector_value.upper() == "ETP":
        return "ETP"

    if sector_value.upper() == "CDR":
        return "CDR"

    if sector_value.lower() == "closed-end funds":
        return "Fund"

    if sector_value.upper() == "SPAC":
        return "SPAC"

    if sector_value.upper() == "CPC":
        return "CPC"

    if trust_value.upper() == "Y":
        return "Trust"

    for pattern in EXCLUDED_NAME_TERMS:
        if re.search(pattern, name, flags=re.IGNORECASE):
            return "Non-Common Equity"

    return "Equity"


def load_and_standardize_sheet(
    workbook_path: Path,
    sheet_name: str,
) -> pd.DataFrame:
    header_row = find_header_row(workbook_path, sheet_name)

    raw = pd.read_excel(
        workbook_path,
        sheet_name=sheet_name,
        header=header_row,
    )

    raw = raw.dropna(how="all").copy()

    exchange = clean_text(get_series(raw, "Exchange"))
    company_name = clean_text(get_series(raw, "Name"))
    root_ticker = clean_text(
        get_series(raw, "Root Ticker", "Root\nTicker")
    )

    sector = clean_text(get_series(raw, "Sector"))

    sub_sector = combine_text_columns(
        get_series(raw, "Sub-Sector"),
        get_series(raw, "Sub Sector"),
        get_series(raw, "Sub\nSector"),
    )

    technology_sub_sector = combine_text_columns(
        get_series(raw, "Technology Sub-Sector"),
        get_series(raw, "Technology Sub-Sector "),
    )

    life_sciences_sub_sector = clean_text(
        get_series(raw, "Life Sciences Sub-Sector")
    )

    consumer_sub_sector = clean_text(
        get_series(
            raw,
            "Consumer Products & Services Sub-Sector",
        )
    )

    clean_technology_sub_sector = combine_text_columns(
        get_series(raw, "Clean Technology Sub-Sector"),
        get_series(raw, "Cleantech Sub-Sector"),
    )

    real_estate_sub_sector = combine_text_columns(
        get_series(raw, "Real Estate Sub-Sector"),
        get_series(raw, "Real Estate Sub-Sector "),
    )

    listing_date = pd.to_datetime(
        get_series(raw, "Listing Date"),
        format="%Y%m%d",
        errors="coerce",
    )

    interlisted = combine_text_columns(
        get_series(raw, "Interlisted"),
        get_series(raw, "Interlisted I"),
        get_series(raw, "Interlisted II"),
    )

    otc_listing = combine_text_columns(
        get_series(raw, "Trading on OTC"),
        get_series(raw, "Trading on OTC "),
        get_series(raw, "Trading  on OTC"),
    )

    former_cpc = combine_text_columns(
        get_series(raw, "Former CPC"),
        get_series(raw, "CPC/ Former CPC"),
        get_series(raw, "CPC/\nFormer\nCPC"),
    )

    trust = clean_text(get_series(raw, "Trust"))

    market_cap_values, market_cap_column = get_prefixed_series(
        raw, "Market Cap (C$)"
    )
    market_cap = pd.to_numeric(market_cap_values, errors="coerce")

    shares_outstanding = pd.to_numeric(
        get_prefixed_series(raw, "O/S Shares")[0],
        errors="coerce",
    )

    volume_ytd = pd.to_numeric(
        get_prefixed_series(raw, "Volume YTD")[0],
        errors="coerce",
    )

    value_traded_ytd = pd.to_numeric(
        get_prefixed_series(raw, "Value (C$) YTD")[0],
        errors="coerce",
    )

    trades_ytd = pd.to_numeric(
        get_prefixed_series(raw, "Number of Trades YTD")[0],
        errors="coerce",
    )

    result = pd.DataFrame(
        {
            "root_ticker": root_ticker,
            "company_name": company_name,
            "exchange": exchange,
            "country": "CA",
            "sector": sector,
            "sub_sector": sub_sector,
            "technology_sub_sector": technology_sub_sector,
            "life_sciences_sub_sector": life_sciences_sub_sector,
            "consumer_sub_sector": consumer_sub_sector,
            "clean_technology_sub_sector": clean_technology_sub_sector,
            "real_estate_sub_sector": real_estate_sub_sector,
            "hq_location": clean_text(
                get_series(raw, "HQ Location", "HQ\nLocation")
            ),
            "hq_region": clean_text(
                get_series(raw, "HQ Region", "HQ\nRegion")
            ),
            "listing_type": clean_text(
                get_series(raw, "Listing Type")
            ),
            "listing_date": listing_date,
            "interlisted": interlisted,
            "otc_listing": otc_listing,
            "former_cpc": former_cpc,
            "trust": trust,
            "market_cap_source_cad": market_cap,
            "shares_outstanding_source": shares_outstanding,
            "volume_ytd_source": volume_ytd,
            "value_traded_ytd_source_cad": value_traded_ytd,
            "trades_ytd_source": trades_ytd,
            "source_date": source_date_from_column(market_cap_column),
        }
    )

    result = result.dropna(
        subset=["root_ticker", "company_name", "exchange"]
    )

    result["ticker"] = result.apply(
        lambda row: create_yahoo_ticker(
            row["root_ticker"],
            row["exchange"],
        ),
        axis=1,
    )

    result["asset_type"] = result.apply(
        lambda row: classify_asset_type(
            row["company_name"],
            row["sector"],
            row["trust"],
        ),
        axis=1,
    )

    return result


def clean_canadian_universe(
    universe: pd.DataFrame,
) -> pd.DataFrame:
    universe = universe.copy()

    universe["root_ticker"] = (
        universe["root_ticker"]
        .astype("string")
        .str.strip()
        .str.upper()
    )

    universe["ticker"] = (
        universe["ticker"]
        .astype("string")
        .str.strip()
        .str.upper()
    )

    universe = universe[
        universe["exchange"].isin(["TSX", "TSXV"])
    ]

    universe = universe[
        universe["asset_type"] == "Equity"
    ]

    universe = universe[
        ~universe["sector"].isin(EXCLUDED_SECTORS)
    ]

    universe = universe[
        ~universe["root_ticker"].str.endswith(
            (".P", ".R", ".W", ".U"),
            na=False,
        )
    ]

    universe = universe[
        ~universe["company_name"].str.contains(
            "|".join(EXCLUDED_NAME_TERMS),
            case=False,
            regex=True,
            na=False,
        )
    ]

    # We retain only companies that were inside the desired market-cap
    # range in the source workbook. Current market cap will still be
    # checked again by yfinance during weekly scoring.
    universe = universe[
        universe["market_cap_source_cad"].between(
            MIN_SOURCE_MARKET_CAP,
            MAX_SOURCE_MARKET_CAP,
            inclusive="both",
        )
    ]

    universe = universe.drop_duplicates(
        subset=["ticker"],
        keep="first",
    )

    universe["source"] = "TMX Listed Company Directory"
    if "source_date" not in universe.columns:
        universe["source_date"] = pd.NA
    universe["enabled"] = "TRUE"

    for column in OUTPUT_COLUMNS:
        if column not in universe.columns:
            universe[column] = pd.NA

    return universe[OUTPUT_COLUMNS].sort_values(
        ["exchange", "sector", "ticker"],
        na_position="last",
    )


def locate_input_workbook() -> Path:
    preferred_path = DEFAULT_INPUT_FILE

    if preferred_path.exists():
        return preferred_path

    candidates = sorted(INPUT_DIR.glob("*.xlsx"))

    if not candidates:
        raise FileNotFoundError(
            "No TMX workbook was found. Place the workbook in "
            f"{INPUT_DIR}"
        )

    if len(candidates) > 1:
        print(
            "Multiple Excel workbooks found. "
            f"Using: {candidates[0].name}"
        )

    return candidates[0]


def main() -> None:
    workbook_path = locate_input_workbook()
    UNIVERSE_DIR.mkdir(parents=True, exist_ok=True)

    print(f"Reading TMX workbook: {workbook_path}")

    workbook = pd.ExcelFile(workbook_path)

    expected_sheets = [
        sheet_name
        for sheet_name in workbook.sheet_names
        if "tsx issuers" in sheet_name.lower()
        or "tsxv issuers" in sheet_name.lower()
    ]

    if not expected_sheets:
        raise ValueError(
            "No TSX or TSXV issuer worksheets were found."
        )

    frames = []

    for sheet_name in expected_sheets:
        print(f"Processing worksheet: {sheet_name}")
        frames.append(
            load_and_standardize_sheet(
                workbook_path,
                sheet_name,
            )
        )

    combined = pd.concat(frames, ignore_index=True)
    cleaned = clean_canadian_universe(combined)

    cleaned.to_csv(OUTPUT_FILE, index=False)

    print("\nCanadian universe created.")
    print(f"Output: {OUTPUT_FILE}")
    print(f"Total companies: {len(cleaned):,}")
    print("\nBy exchange:")
    print(cleaned["exchange"].value_counts().to_string())
    print("\nBy sector:")
    print(cleaned["sector"].value_counts().head(15).to_string())


if __name__ == "__main__":
    main()
