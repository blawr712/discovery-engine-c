"""Persistent run manifests and atomic per-company checkpoints."""

from __future__ import annotations

from collections import Counter
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import tempfile
from typing import Callable
from uuid import uuid4
import re


RUN_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")


def build_run_fingerprint(
    universe: list[dict],
    strategy: dict,
    settings: dict,
) -> str:
    """Fingerprint every input that can materially affect a run."""
    payload = {
        "universe": universe,
        "strategy": strategy,
        "settings": settings,
    }
    encoded = json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        default=_json_default,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


class RunState:
    """Create, resume, checkpoint, and complete one engine run."""

    def __init__(
        self,
        run_directory: Path,
        manifest: dict,
        clock: Callable[[], datetime],
        resumed: bool = False,
    ) -> None:
        self.run_directory = run_directory
        self.checkpoint_directory = run_directory / "checkpoints"
        self.manifest_path = run_directory / "manifest.json"
        self.manifest = manifest
        self.clock = clock
        self.resumed = resumed

    @property
    def run_id(self) -> str:
        return str(self.manifest["run_id"])

    @classmethod
    def start_or_resume(
        cls,
        root_directory: Path,
        fingerprint: str,
        universe_size: int,
        resume_enabled: bool = True,
        clock: Callable[[], datetime] | None = None,
    ) -> "RunState":
        """Resume the newest compatible incomplete run or create a new one."""
        clock = clock or (lambda: datetime.now(timezone.utc))
        root_directory = Path(root_directory)
        root_directory.mkdir(parents=True, exist_ok=True)

        if resume_enabled:
            match = cls._find_resumable_run(
                root_directory,
                fingerprint,
                universe_size,
            )
            if match is not None:
                run_directory, manifest = match
                manifest["status"] = "in_progress"
                manifest["completed_at"] = None
                manifest["report_path"] = None
                manifest["candidate_report_path"] = None
                manifest["analytics_path"] = None
                manifest["calibration_csv_path"] = None
                manifest["calibration_json_path"] = None
                manifest["research_artifacts"] = None
                manifest["resume_count"] = manifest.get("resume_count", 0) + 1
                state = cls(
                    run_directory,
                    manifest,
                    clock,
                    resumed=True,
                )
                state.checkpoint_directory.mkdir(parents=True, exist_ok=True)
                state._refresh_checkpoint_count()
                return state

        now = _utc_iso(clock())
        run_id = f"{clock().strftime('%Y%m%dT%H%M%SZ')}-{uuid4().hex[:8]}"
        run_directory = root_directory / run_id
        checkpoint_directory = run_directory / "checkpoints"
        checkpoint_directory.mkdir(parents=True, exist_ok=False)
        manifest = {
            "run_id": run_id,
            "fingerprint": fingerprint,
            "status": "in_progress",
            "started_at": now,
            "updated_at": now,
            "completed_at": None,
            "duration_seconds": None,
            "universe_size": universe_size,
            "completed_count": 0,
            "summary": None,
            "report_path": None,
            "candidate_report_path": None,
            "analytics_path": None,
            "calibration_csv_path": None,
            "calibration_json_path": None,
            "research_artifacts": None,
            "resume_count": 0,
        }
        state = cls(run_directory, manifest, clock)
        state._write_manifest()
        return state

    def load_results(self, retry_errors: bool = True) -> dict[int, dict]:
        """Load valid checkpoint rows, optionally excluding provider errors."""
        results: dict[int, dict] = {}

        for path in sorted(self.checkpoint_directory.glob("*.json")):
            try:
                with path.open("r", encoding="utf-8") as file:
                    checkpoint = json.load(file)
                index = checkpoint["index"]
                result = checkpoint["result"]

                if not isinstance(index, int) or not isinstance(result, dict):
                    continue
                if retry_errors and result.get("status") == "ERROR":
                    continue

                results[index] = result
            except (OSError, json.JSONDecodeError, KeyError, TypeError):
                continue

        return results

    def record_result(self, index: int, result: dict) -> None:
        """Atomically checkpoint a terminal result and update the manifest."""
        ticker = str(result.get("ticker", "UNKNOWN"))
        ticker_hash = hashlib.sha256(ticker.encode("utf-8")).hexdigest()[:10]
        path = self.checkpoint_directory / f"{index:08d}-{ticker_hash}.json"
        is_new_checkpoint = not path.exists()
        _atomic_write_json(
            path,
            {"index": index, "ticker": ticker, "result": result},
        )
        if is_new_checkpoint:
            self.manifest["completed_count"] += 1
        self.manifest["updated_at"] = _utc_iso(self.clock())
        self._write_manifest()

    def complete(
        self,
        results: list[dict],
        report_path: str,
        candidate_report_path: str | None = None,
        analytics_path: str | None = None,
        calibration_csv_path: str | None = None,
        calibration_json_path: str | None = None,
        research_artifacts: dict | None = None,
    ) -> None:
        """Mark the run complete and add aggregate manifest statistics."""
        completed_at = self.clock()
        started_at = datetime.fromisoformat(self.manifest["started_at"])
        summary = summarize_results(results)
        error_count = summary["statuses"].get("ERROR", 0)
        self.manifest.update(
            {
                "status": (
                    "completed_with_errors" if error_count else "complete"
                ),
                "updated_at": _utc_iso(completed_at),
                "completed_at": _utc_iso(completed_at),
                "duration_seconds": round(
                    (completed_at - started_at).total_seconds(),
                    3,
                ),
                "completed_count": len(results),
                "summary": summary,
                "report_path": report_path,
                "candidate_report_path": candidate_report_path,
                "analytics_path": analytics_path,
                "calibration_csv_path": calibration_csv_path,
                "calibration_json_path": calibration_json_path,
                "research_artifacts": research_artifacts,
            }
        )
        self._write_manifest()

    def _refresh_checkpoint_count(self) -> None:
        self.manifest["completed_count"] = sum(
            1 for _ in self.checkpoint_directory.glob("*.json")
        )
        self.manifest["updated_at"] = _utc_iso(self.clock())
        self._write_manifest()

    def _write_manifest(self) -> None:
        _atomic_write_json(self.manifest_path, self.manifest)

    @staticmethod
    def _find_resumable_run(
        root_directory: Path,
        fingerprint: str,
        universe_size: int,
    ) -> tuple[Path, dict] | None:
        matches: list[tuple[Path, dict]] = []

        for path in root_directory.glob("*/manifest.json"):
            try:
                with path.open("r", encoding="utf-8") as file:
                    manifest = json.load(file)
            except (OSError, json.JSONDecodeError, TypeError):
                continue

            if (
                manifest.get("status")
                in {"in_progress", "completed_with_errors"}
                and manifest.get("fingerprint") == fingerprint
                and manifest.get("universe_size") == universe_size
            ):
                matches.append((path.parent, manifest))

        if not matches:
            return None

        return max(
            matches,
            key=lambda match: str(match[1].get("updated_at", "")),
        )


def summarize_results(results: list[dict]) -> dict:
    """Build stable aggregate counts for a completed run manifest."""
    statuses = Counter(str(row.get("status", "UNKNOWN")) for row in results)
    countries = Counter(str(row.get("country") or "UNKNOWN") for row in results)
    exchanges = Counter(str(row.get("exchange") or "UNKNOWN") for row in results)
    failure_stages = Counter()

    for row in results:
        if row.get("status") != "ERROR":
            continue
        reason = str(row.get("reason_flags", "Unknown"))
        stage = reason.split(":", maxsplit=1)[0] or "Unknown"
        failure_stages[stage] += 1

    return {
        "statuses": dict(sorted(statuses.items())),
        "countries": dict(sorted(countries.items())),
        "exchanges": dict(sorted(exchanges.items())),
        "failure_stages": dict(sorted(failure_stages.items())),
    }


def load_saved_run(
    root_directory: Path,
    run_id: str,
) -> tuple[dict, list[dict]]:
    """Load a complete run without initializing any market-data provider."""
    manifest = load_saved_manifest(root_directory, run_id)
    run_id = str(run_id).strip()
    run_directory = Path(root_directory) / run_id

    checkpoint_directory = run_directory / "checkpoints"
    indexed_results = {}
    for path in sorted(checkpoint_directory.glob("*.json")):
        try:
            with path.open("r", encoding="utf-8") as file:
                checkpoint = json.load(file)
            index = checkpoint["index"]
            result = checkpoint["result"]
        except (OSError, json.JSONDecodeError, KeyError, TypeError):
            continue
        if isinstance(index, int) and isinstance(result, dict):
            indexed_results[index] = result

    expected = int(manifest.get("completed_count", 0))
    if len(indexed_results) != expected:
        raise ValueError(
            f"Saved run has {len(indexed_results)} readable checkpoints; "
            f"expected {expected}."
        )
    return manifest, [indexed_results[index] for index in sorted(indexed_results)]


def load_saved_manifest(root_directory: Path, run_id: str) -> dict:
    """Load and validate a completed run manifest without checkpoints."""
    run_id = str(run_id).strip()
    if not RUN_ID_PATTERN.fullmatch(run_id):
        raise ValueError("Run ID contains invalid characters.")
    run_directory = Path(root_directory) / run_id
    manifest_path = run_directory / "manifest.json"
    if not manifest_path.is_file():
        raise FileNotFoundError(f"Saved run not found: {run_id}")
    try:
        with manifest_path.open("r", encoding="utf-8") as file:
            manifest = json.load(file)
    except (OSError, json.JSONDecodeError, TypeError) as error:
        raise ValueError(f"Saved run manifest is unreadable: {run_id}") from error
    if manifest.get("status") not in {"complete", "completed_with_errors"}:
        raise ValueError(f"Saved run is not complete: {run_id}")
    if manifest.get("run_id") != run_id:
        raise ValueError(f"Saved run manifest ID does not match: {run_id}")
    return manifest


def record_recalibration(
    root_directory: Path,
    run_id: str,
    artifacts: dict,
    clock: Callable[[], datetime] | None = None,
) -> str:
    """Record offline artifact provenance on an existing run manifest."""
    manifest = load_saved_manifest(root_directory, run_id)
    clock = clock or (lambda: datetime.now(timezone.utc))
    manifest["calibration_csv_path"] = artifacts.get("calibration_csv_path")
    manifest["calibration_json_path"] = artifacts.get("calibration_json_path")
    manifest["recalibration"] = {
        "completed_at": _utc_iso(clock()),
        **artifacts,
    }
    manifest_path = Path(root_directory) / run_id / "manifest.json"
    _atomic_write_json(manifest_path, manifest)
    return str(manifest_path)


def record_research_packets(
    root_directory: Path,
    run_id: str,
    artifacts: dict,
    clock: Callable[[], datetime] | None = None,
) -> str:
    """Record deterministic research-packet provenance on a saved run."""
    manifest = load_saved_manifest(root_directory, run_id)
    clock = clock or (lambda: datetime.now(timezone.utc))
    manifest["research_packet_artifacts"] = {
        "completed_at": _utc_iso(clock()),
        **artifacts,
    }
    manifest_path = Path(root_directory) / run_id / "manifest.json"
    _atomic_write_json(manifest_path, manifest)
    return str(manifest_path)


def record_moonshot_analysis(
    root_directory: Path,
    run_id: str,
    artifacts: dict,
    clock: Callable[[], datetime] | None = None,
) -> str:
    """Record offline Moonshot shadow-analysis provenance."""
    manifest = load_saved_manifest(root_directory, run_id)
    clock = clock or (lambda: datetime.now(timezone.utc))
    manifest["moonshot_artifacts"] = {
        "completed_at": _utc_iso(clock()),
        **artifacts,
    }
    manifest_path = Path(root_directory) / run_id / "manifest.json"
    _atomic_write_json(manifest_path, manifest)
    return str(manifest_path)


def record_moonshot_calibration(
    root_directory: Path,
    run_id: str,
    artifacts: dict,
    clock: Callable[[], datetime] | None = None,
) -> str:
    """Record offline Moonshot calibration and forward-baseline provenance."""
    manifest = load_saved_manifest(root_directory, run_id)
    clock = clock or (lambda: datetime.now(timezone.utc))
    manifest["moonshot_calibration_artifacts"] = {
        "completed_at": _utc_iso(clock()),
        **artifacts,
    }
    manifest_path = Path(root_directory) / run_id / "manifest.json"
    _atomic_write_json(manifest_path, manifest)
    return str(manifest_path)


def record_research_audit(
    root_directory: Path,
    run_id: str,
    artifacts: dict,
    clock: Callable[[], datetime] | None = None,
) -> str:
    """Record offline research-audit provenance on a saved run."""
    manifest = load_saved_manifest(root_directory, run_id)
    clock = clock or (lambda: datetime.now(timezone.utc))
    manifest["research_audit_artifacts"] = {
        "completed_at": _utc_iso(clock()),
        **artifacts,
    }
    manifest_path = Path(root_directory) / run_id / "manifest.json"
    _atomic_write_json(manifest_path, manifest)
    return str(manifest_path)


def record_research_acceptance(
    root_directory: Path,
    run_id: str,
    artifacts: dict,
    clock: Callable[[], datetime] | None = None,
) -> str:
    """Record the explicit human research-review decision."""
    manifest = load_saved_manifest(root_directory, run_id)
    clock = clock or (lambda: datetime.now(timezone.utc))
    manifest["research_acceptance"] = {
        "completed_at": _utc_iso(clock()),
        **artifacts,
    }
    manifest_path = Path(root_directory) / run_id / "manifest.json"
    _atomic_write_json(manifest_path, manifest)
    return str(manifest_path)


def _atomic_write_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, name = tempfile.mkstemp(
        prefix=f".{path.stem}-",
        suffix=".tmp",
        dir=path.parent,
    )
    temporary_path = Path(name)

    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as file:
            json.dump(
                data,
                file,
                ensure_ascii=False,
                indent=2,
                sort_keys=True,
                default=_json_default,
            )
        os.replace(temporary_path, path)
    finally:
        temporary_path.unlink(missing_ok=True)


def _json_default(value):
    if hasattr(value, "item"):
        return value.item()
    if hasattr(value, "isoformat"):
        return value.isoformat()
    return str(value)


def _utc_iso(value: datetime) -> str:
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat()
