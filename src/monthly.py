"""The monthly research cycle: one command from fresh universe to resolved cohorts.

Forward baselines are the only evidence in this project that survivorship
cannot inflate, and they accumulate only if a run is made, frozen, and later
resolved every month without gaps. Done by hand that is about seven commands
across a day, any one of which is easy to forget. This module runs them in
order and remembers what has been done.

Each cycle has a label (``2026-11``) and a state file. Steps that finished are
never repeated, so a cycle interrupted anywhere -- the machine sleeping
through the discovery run, a provider outage during Moonshot collection --
continues from where it stopped when the command is run again. A step that
fails does not stop steps that do not depend on it; steps that do depend on
it wait for the next attempt.

The cycle label is also mixed into the discovery run's fingerprint. Without
it, an unchanged universe and configuration would fingerprint identically
next month, and the engine would resume last month's run rather than start
this month's.

Nothing here talks to a provider. The steps are supplied by the caller, which
keeps the orchestration testable and the provider wiring in ``main.py``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import re
import tempfile
from typing import Callable


STEP_ORDER = (
    "universe",
    "discovery",
    "index",
    "moonshot",
    "calibrate",
    "freeze",
    "resolve",
)

# A step runs only once everything it depends on is done. The universe
# rebuild is deliberately not a dependency of discovery: if the listing
# download fails, last month's universe files are a better input than no run.
DEPENDENCIES = {
    "universe": (),
    "discovery": (),
    "index": ("discovery",),
    "moonshot": ("discovery",),
    "calibrate": ("moonshot",),
    "freeze": ("discovery",),
    "resolve": (),
}

CYCLE_PATTERN = re.compile(r"^\d{4}-(0[1-9]|1[0-2])(-[A-Za-z0-9]+)?$")
RUN_ID_IN_NAME = re.compile(r"(\d{8}T\d{6}Z-[0-9a-f]{8})")

# An unfinished cycle touched within this window is continued by default
# rather than replaced by a new month's label, so a cycle that starts on the
# 30th and finishes on the 1st stays one cycle.
CONTINUE_WINDOW = timedelta(days=7)

# A full cycle has never taken close to this; a lock older than it was left
# by a process that died, as the first October run did, without a traceback.
STALE_LOCK = timedelta(hours=48)

# The TMX workbook is published monthly and has to be downloaded by hand.
TMX_STALE_AFTER = timedelta(days=45)

# A rate-limit episode leaves rows as provider errors for a later resume. A
# baseline is immutable, so freezing one on a run that is only partly
# collected would make the gap permanent; the October run had 1 error in
# 6,426. Above this share the discovery step fails, everything that depends
# on it waits, and the next attempt resumes the run and retries those rows.
MAX_DISCOVERY_ERROR_RATE = 0.02


class CycleLocked(RuntimeError):
    """Another process appears to be running this cycle."""


@dataclass
class StepContext:
    """What a step can see: the cycle, the run it belongs to, and the clock."""

    cycle: str
    now: datetime
    run_id: str | None = None
    warnings: list[str] = field(default_factory=list)
    # Structured results, kept even when the step fails part-way so that work
    # it did finish is not lost; list values accumulate across attempts.
    outputs: dict = field(default_factory=dict)


Step = Callable[[StepContext], dict | None]


def default_cycle(state_directory: Path, now: datetime) -> str:
    """The cycle a bare ``--monthly`` should work on.

    An unfinished cycle updated within the continue window wins; otherwise
    the current UTC month.
    """
    recent = []
    for path in Path(state_directory).glob("*.json"):
        state = _read_json(path)
        if not state or state.get("status") == "completed":
            continue
        updated = _parse_time(state.get("updated_at"))
        if updated is not None and now - updated <= CONTINUE_WINDOW:
            recent.append((updated, str(state.get("cycle") or path.stem)))
    if recent:
        return max(recent)[1]
    return now.strftime("%Y-%m")


def validate_cycle(cycle: str) -> str:
    cycle = str(cycle).strip()
    if not CYCLE_PATTERN.match(cycle):
        raise ValueError(
            f"Cycle label {cycle!r} must look like 2026-11, optionally with a "
            "suffix such as 2026-11-rerun."
        )
    return cycle


def run_cycle(
    cycle: str,
    steps: dict[str, Step],
    state_directory: Path,
    clock: Callable[[], datetime] | None = None,
    log: Callable[[str], None] = print,
) -> dict:
    """Run every unfinished step of a cycle and return the saved state."""
    clock = clock or (lambda: datetime.now(timezone.utc))
    cycle = validate_cycle(cycle)
    missing = [name for name in STEP_ORDER if name not in steps]
    if missing:
        raise ValueError(f"Monthly cycle is missing steps: {missing}")

    state_directory = Path(state_directory)
    state_directory.mkdir(parents=True, exist_ok=True)
    state_path = state_directory / f"{cycle}.json"
    lock_path = state_directory / f"{cycle}.lock"
    _acquire_lock(lock_path, clock())
    try:
        state = _read_json(state_path) or _new_state(cycle, clock())
        if state.get("status") == "completed":
            log(f"Monthly cycle {cycle} is already complete; nothing to do.")
            return state
        state["status"] = "in_progress"
        state["attempts"] = int(state.get("attempts", 0)) + 1
        _save(state_path, state, clock())

        for name in STEP_ORDER:
            record = state["steps"][name]
            if record["status"] == "done":
                log(f"[monthly] {name}: already done")
                continue
            blocked = [
                dependency for dependency in DEPENDENCIES[name]
                if state["steps"][dependency]["status"] != "done"
            ]
            if blocked:
                record.update(status="blocked", detail=f"waiting on {', '.join(blocked)}")
                log(f"[monthly] {name}: blocked, waiting on {', '.join(blocked)}")
                _save(state_path, state, clock())
                continue

            log(f"[monthly] {name}: starting")
            record.update(status="running", started_at=_iso(clock()), detail=None)
            _save(state_path, state, clock())
            context = StepContext(cycle=cycle, now=clock(), run_id=state.get("run_id"))
            try:
                outcome = steps[name](context) or {}
            except (Exception, SystemExit) as error:  # noqa: BLE001 - isolate each step
                message = str(error) or type(error).__name__
                record.update(
                    status="failed", finished_at=_iso(clock()), detail=message,
                    outputs=_merge_outputs(record.get("outputs"), context.outputs),
                )
                state["warnings"].extend(_tagged(name, context.warnings))
                log(f"[monthly] {name}: FAILED: {message}")
                _save(state_path, state, clock())
                continue

            if "run_id" in outcome and outcome["run_id"]:
                state["run_id"] = str(outcome["run_id"])
            returned = {k: v for k, v in outcome.items() if k not in {"detail", "run_id"}}
            record.update(
                status="done",
                finished_at=_iso(clock()),
                detail=outcome.get("detail"),
                outputs=_merge_outputs(
                    record.get("outputs"), {**context.outputs, **returned},
                ),
            )
            state["warnings"].extend(_tagged(name, context.warnings))
            log(f"[monthly] {name}: done" + (f" ({outcome['detail']})" if outcome.get("detail") else ""))
            _save(state_path, state, clock())

        statuses = {record["status"] for record in state["steps"].values()}
        state["status"] = "completed" if statuses == {"done"} else "completed_with_failures"
        state["finished_at"] = _iso(clock())
        _save(state_path, state, clock())
        return state
    finally:
        lock_path.unlink(missing_ok=True)


def discovery_quality(
    results: list[dict],
    max_error_rate: float = MAX_DISCOVERY_ERROR_RATE,
) -> tuple[bool, str]:
    """Whether a completed run is complete enough to freeze, and a summary."""
    total = len(results)
    passed = sum(1 for row in results if row.get("status") == "OK")
    errors = sum(1 for row in results if row.get("status") == "ERROR")
    rate = errors / total if total else 1.0
    detail = f"{passed} passed, {errors} provider errors of {total}"
    if not total or rate > max_error_rate:
        return False, (
            f"{detail} ({rate:.1%} errors, above the {max_error_rate:.0%} limit); "
            "rerun --monthly to resume the run and retry them"
        )
    return True, detail


def due_baselines(
    output_directory: Path,
    now: datetime,
    kinds: dict[str, str] | None = None,
) -> list[dict]:
    """Frozen baselines with a horizon that became eligible since last resolved.

    A horizon is due when its ``eligible_at`` has passed and it was not among
    the eligible horizons the last time that baseline was resolved, so each
    newly matured horizon is measured exactly once by the schedule. Never
    resolved baselines are due as soon as their first horizon matures.
    """
    kinds = kinds or {
        "moonshot": "moonshot_forward_baseline",
        "research": "research_forward_baseline",
    }
    output_directory = Path(output_directory)
    due = []
    for kind, prefix in sorted(kinds.items()):
        for path in sorted(output_directory.glob(f"{prefix}_*.json")):
            match = RUN_ID_IN_NAME.search(path.name)
            baseline = _read_json(path)
            if not match or not baseline or not baseline.get("candidates"):
                continue
            run_id = match.group(1)
            horizons = baseline["candidates"][0].get("horizons") or {}
            eligible = sorted(
                label for label, horizon in horizons.items()
                if (moment := _parse_time(horizon.get("eligible_at"))) is not None
                and moment <= now
            )
            if not eligible:
                continue
            resolved = _last_resolution(output_directory, kind, run_id)
            already = set(resolved.get("eligible_horizons") or []) if resolved else set()
            newly = [label for label in eligible if label not in already]
            if newly:
                due.append({"run_id": run_id, "kind": kind, "horizons": newly})
    return due


def universe_source_age(universe_csv: Path, now: datetime) -> tuple[str | None, timedelta | None]:
    """The TMX source date carried in the Canadian universe file, and its age."""
    try:
        import pandas as pd

        frame = pd.read_csv(universe_csv, usecols=["source_date"])
    except (OSError, ValueError, KeyError):
        return None, None
    dates = pd.to_datetime(frame["source_date"], errors="coerce").dropna()
    if dates.empty:
        return None, None
    latest = dates.max().to_pydatetime().replace(tzinfo=timezone.utc)
    return latest.date().isoformat(), now - latest


def build_cycle_report(state: dict, resolutions: list[dict] | None = None) -> str:
    """A short Markdown record of what a cycle did and what it measured."""
    lines = [
        f"# Monthly cycle {state['cycle']}",
        "",
        f"Status: **{state['status']}** after {state.get('attempts', 1)} attempt(s)",
        f"Discovery run: `{state.get('run_id') or 'not completed'}`",
        "",
        "| Step | Status | Detail |",
        "|---|---|---|",
    ]
    for name in STEP_ORDER:
        record = state["steps"][name]
        detail = str(record.get("detail") or "").replace("|", "/").replace("\n", " ")
        lines.append(f"| {name} | {record['status']} | {detail[:160]} |")
    if state.get("warnings"):
        lines += ["", "## Warnings", ""] + [f"- {warning}" for warning in state["warnings"]]
    if resolutions:
        lines += [
            "",
            "## Newly matured horizons",
            "",
            "Benchmark-relative. Selected and control cohorts as each baseline "
            "defines them. One horizon of one cohort is a single observation.",
            "",
            "| Baseline | Horizon | Selected median | Control median | "
            "Selected win | Control win |",
            "|---|---|---|---|---|---|",
        ]
        for entry in resolutions:
            for horizon in entry.get("horizons", []):
                summary = (entry.get("summary") or {}).get(horizon) or {}
                selected = summary.get("selected") or {}
                control = summary.get("control") or {}
                lines.append(
                    f"| {entry['kind']} `{entry['run_id']}` | {horizon} | "
                    f"{_pct(selected.get('median_excess_percent'))} | "
                    f"{_pct(control.get('median_excess_percent'))} | "
                    f"{_pct(selected.get('win_rate_percent'))} | "
                    f"{_pct(control.get('win_rate_percent'))} |"
                )
    lines += [
        "",
        "This records what was run and measured. It is not a forecast or an "
        "investment recommendation.",
        "",
    ]
    return "\n".join(lines)


def _last_resolution(output_directory: Path, kind: str, run_id: str) -> dict | None:
    # Resolutions written before kinds existed were named without one.
    names = [f"baseline_resolution_{kind}_{run_id}.json"]
    if kind == "moonshot":
        names.append(f"baseline_resolution_{run_id}.json")
    for name in names:
        resolved = _read_json(output_directory / name)
        if resolved:
            return resolved
    return None


def _new_state(cycle: str, now: datetime) -> dict:
    return {
        "schema_version": 1,
        "cycle": cycle,
        "status": "in_progress",
        "started_at": _iso(now),
        "updated_at": _iso(now),
        "finished_at": None,
        "attempts": 0,
        "run_id": None,
        "warnings": [],
        "steps": {name: {"status": "pending"} for name in STEP_ORDER},
    }


def _acquire_lock(lock_path: Path, now: datetime) -> None:
    payload = json.dumps({"pid": os.getpid(), "created_at": _iso(now)})
    for _ in range(2):
        try:
            descriptor = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            existing = _read_json(lock_path) or {}
            created = _parse_time(existing.get("created_at"))
            if created is not None and now - created <= STALE_LOCK:
                raise CycleLocked(
                    f"Monthly cycle lock {lock_path} was created at "
                    f"{existing.get('created_at')} by process {existing.get('pid')}. "
                    "If no other cycle is running, delete that file and retry."
                ) from None
            lock_path.unlink(missing_ok=True)
            continue
        with os.fdopen(descriptor, "w", encoding="utf-8") as file:
            file.write(payload)
        return
    raise CycleLocked(f"Could not acquire monthly cycle lock {lock_path}.")


def _save(path: Path, state: dict, now: datetime) -> None:
    state["updated_at"] = _iso(now)
    descriptor, name = tempfile.mkstemp(prefix=f".{path.stem}-", suffix=".json", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as file:
            json.dump(state, file, indent=2, sort_keys=True, default=str)
        os.replace(name, path)
    finally:
        Path(name).unlink(missing_ok=True)


def _merge_outputs(previous: dict | None, new: dict) -> dict:
    merged = dict(previous or {})
    for key, value in new.items():
        if isinstance(value, list) and isinstance(merged.get(key), list):
            merged[key] = merged[key] + value
        else:
            merged[key] = value
    return merged


def _tagged(step: str, warnings: list[str]) -> list[str]:
    return [f"{step}: {warning}" for warning in warnings]


def _read_json(path: Path) -> dict | None:
    try:
        with Path(path).open("r", encoding="utf-8") as file:
            value = json.load(file)
    except (OSError, json.JSONDecodeError):
        return None
    return value if isinstance(value, dict) else None


def _parse_time(value: object) -> datetime | None:
    if not value:
        return None
    try:
        moment = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return moment if moment.tzinfo else moment.replace(tzinfo=timezone.utc)


def _iso(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).isoformat()


def _pct(value: object) -> str:
    return f"{value:.2f}%" if isinstance(value, (int, float)) else "-"
