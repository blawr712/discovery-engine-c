"""Small atomic JSON store for local dashboard watchlists."""

from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import tempfile
from threading import RLock
from uuid import uuid4


SCHEMA_VERSION = 1
MAX_WATCHLISTS = 20
MAX_ITEMS = 100
_TICKER_PATTERN = re.compile(r"^[A-Z0-9.^=\-]{1,20}$")


class WatchlistStore:
    """Persist bounded named watchlists without modifying indexed history."""

    def __init__(self, path: Path, clock=None):
        self.path = Path(path)
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self._lock = RLock()

    def list_all(self) -> list[dict]:
        with self._lock:
            return deepcopy(self._load()["watchlists"])

    def create(self, name: str) -> dict:
        name = str(name or "").strip()
        if not 1 <= len(name) <= 50:
            raise ValueError("Watchlist name must contain 1 to 50 characters")
        with self._lock:
            document = self._load()
            lists = document["watchlists"]
            if len(lists) >= MAX_WATCHLISTS:
                raise ValueError(f"Watchlists are limited to {MAX_WATCHLISTS}")
            if any(item["name"].casefold() == name.casefold() for item in lists):
                raise ValueError(f"Watchlist already exists: {name}")
            now = self._now()
            watchlist = {
                "id": uuid4().hex[:12],
                "name": name,
                "created_at": now,
                "updated_at": now,
                "items": [],
            }
            lists.append(watchlist)
            self._save(document)
            return deepcopy(watchlist)

    def delete(self, watchlist_id: str) -> None:
        with self._lock:
            document = self._load()
            watchlist = self._find(document, watchlist_id)
            document["watchlists"].remove(watchlist)
            self._save(document)

    def add_item(self, watchlist_id: str, ticker: str) -> dict:
        ticker = _normalize_ticker(ticker)
        with self._lock:
            document = self._load()
            watchlist = self._find(document, watchlist_id)
            existing = next(
                (item for item in watchlist["items"] if item["ticker"] == ticker),
                None,
            )
            if existing is not None:
                return deepcopy(existing)
            if len(watchlist["items"]) >= MAX_ITEMS:
                raise ValueError(f"A watchlist is limited to {MAX_ITEMS} tickers")
            item = {"ticker": ticker, "added_at": self._now()}
            watchlist["items"].append(item)
            watchlist["updated_at"] = item["added_at"]
            self._save(document)
            return deepcopy(item)

    def remove_item(self, watchlist_id: str, ticker: str) -> None:
        ticker = _normalize_ticker(ticker)
        with self._lock:
            document = self._load()
            watchlist = self._find(document, watchlist_id)
            item = next(
                (item for item in watchlist["items"] if item["ticker"] == ticker),
                None,
            )
            if item is None:
                raise ValueError(f"Ticker is not in watchlist: {ticker}")
            watchlist["items"].remove(item)
            watchlist["updated_at"] = self._now()
            self._save(document)

    def _load(self) -> dict:
        if not self.path.exists():
            return {"schema_version": SCHEMA_VERSION, "watchlists": []}
        try:
            document = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            raise ValueError(f"Unable to read watchlists: {error}") from error
        if (
            not isinstance(document, dict)
            or document.get("schema_version") != SCHEMA_VERSION
            or not isinstance(document.get("watchlists"), list)
        ):
            raise ValueError("Watchlist file has an unsupported format")
        return document

    def _save(self, document: dict) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary_path = None
        try:
            with tempfile.NamedTemporaryFile(
                "w", encoding="utf-8", dir=self.path.parent,
                prefix=f".{self.path.name}.", suffix=".tmp", delete=False,
            ) as handle:
                json.dump(document, handle, indent=2, sort_keys=True)
                handle.write("\n")
                temporary_path = Path(handle.name)
            os.replace(temporary_path, self.path)
        except OSError as error:
            raise ValueError(f"Unable to save watchlists: {error}") from error
        finally:
            if temporary_path is not None and temporary_path.exists():
                temporary_path.unlink()

    @staticmethod
    def _find(document: dict, watchlist_id: str) -> dict:
        watchlist = next(
            (item for item in document["watchlists"] if item.get("id") == watchlist_id),
            None,
        )
        if watchlist is None:
            raise ValueError(f"Watchlist not found: {watchlist_id}")
        return watchlist

    def _now(self) -> str:
        return self.clock().astimezone(timezone.utc).isoformat()


def _normalize_ticker(ticker: str) -> str:
    normalized = str(ticker or "").strip().upper()
    if not _TICKER_PATTERN.fullmatch(normalized):
        raise ValueError("Ticker must contain 1 to 20 supported symbol characters")
    return normalized
