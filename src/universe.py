from pathlib import Path
import pandas as pd

from src.config import BASE_DIR, SETTINGS


class UniverseBuilder:
    def __init__(self, universe_directory=None):
        if universe_directory is None:
            configured_dir = SETTINGS.get("application", {}).get(
                "universe_directory",
                "data/raw/universe",
            )
            self.universe_directory = BASE_DIR / configured_dir
        else:
            self.universe_directory = Path(universe_directory)

    def build_universe(self) -> list[dict]:
        universe_files = sorted(self.universe_directory.glob("*.csv"))

        if not universe_files:
            raise FileNotFoundError(
                f"No universe CSV files found in: {self.universe_directory}"
            )

        frames = []

        for path in universe_files:
            df = self._load_universe(path)
            frames.append(df)

        universe = pd.concat(frames, ignore_index=True)
        universe = self._filter_enabled(universe)
        universe = self._clean_tickers(universe)

        print(f"Universe directory: {self.universe_directory}")
        print(f"Universe files loaded: {len(universe_files)}")
        print(f"Universe tickers after cleaning: {len(universe)}")

        return universe.to_dict("records")

    @staticmethod
    def _load_universe(path: Path) -> pd.DataFrame:
        df = pd.read_csv(path)

        required_columns = {"ticker", "exchange", "country", "enabled"}
        missing = required_columns - set(df.columns)

        if missing:
            raise ValueError(f"{path} missing required columns: {missing}")

        df["source_file"] = path.name

        return df

    @staticmethod
    def _filter_enabled(df: pd.DataFrame) -> pd.DataFrame:
        return df[df["enabled"].astype(str).str.upper() == "TRUE"].copy()

    @staticmethod
    def _clean_tickers(df: pd.DataFrame) -> pd.DataFrame:
        df = df.copy()

        df["ticker"] = df["ticker"].astype(str).str.strip().str.upper()
        df = df[df["ticker"] != ""]
        df = df.drop_duplicates(subset=["ticker"])

        return df.sort_values(["country", "exchange", "ticker"])