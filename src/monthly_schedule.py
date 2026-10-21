"""Register the monthly research cycle as a Windows scheduled task.

    python -m src.monthly_schedule show        # print the task definition
    python -m src.monthly_schedule register    # create it
    python -m src.monthly_schedule unregister  # remove it

The task fires at 02:00 on the 16th of every month and again on each day
through the 22nd. ``--monthly`` is resumable and a finished cycle is a no-op,
so the later days only matter when an earlier attempt failed or the machine
was off: each one either finishes the month's cycle or does nothing. The 16th
follows TMX's release of the listed-companies workbook, which arrives around
the 11th to 14th; the workbook still has to be downloaded by hand.

It runs as the current user, only while that user is logged on, with no
stored password and without elevation. A missed start runs as soon as the
machine is available, a second instance is never started while one runs,
and an attempt is stopped after 36 hours so a hung run cannot block the next
day's retry.
"""

from __future__ import annotations

import argparse
from datetime import datetime
import os
from pathlib import Path
import subprocess
import tempfile
from xml.sax.saxutils import escape

TASK_NAME = "Discovery Engine monthly cycle"
RUN_DAYS = tuple(range(16, 23))
RUN_TIME = "02:00:00"
TIME_LIMIT = "PT36H"
MONTHS = (
    "January", "February", "March", "April", "May", "June", "July",
    "August", "September", "October", "November", "December",
)
REPOSITORY = Path(__file__).resolve().parent.parent
WRAPPER = REPOSITORY / "scripts" / "run_monthly.cmd"


def first_start(now: datetime) -> datetime:
    """When the schedule should begin.

    Registering on or after the first run day means this month's cycle may
    already be done or under way by hand, so the schedule starts next month.
    """
    if now.day >= RUN_DAYS[0]:
        year, month = (now.year + 1, 1) if now.month == 12 else (now.year, now.month + 1)
        return datetime(year, month, 1)
    return datetime(now.year, now.month, now.day)


def build_task_xml(
    user_id: str,
    command: Path,
    working_directory: Path,
    start: datetime,
) -> str:
    """The Task Scheduler 1.2 definition for the monthly cycle."""
    days = "".join(f"<Day>{day}</Day>" for day in RUN_DAYS)
    months = "".join(f"<{month} />" for month in MONTHS)
    description = (
        "Runs python main.py --monthly for Discovery Engine. Fires on days "
        f"{RUN_DAYS[0]}-{RUN_DAYS[-1]}; a finished cycle makes later days a no-op. "
        f"Logs: {working_directory / 'data' / 'runs' / 'monthly' / 'logs'}"
    )
    return f"""<?xml version="1.0" encoding="UTF-16"?>
<Task version="1.2" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
  <RegistrationInfo>
    <Description>{escape(description)}</Description>
    <URI>\\{escape(TASK_NAME)}</URI>
  </RegistrationInfo>
  <Triggers>
    <CalendarTrigger>
      <StartBoundary>{start.strftime('%Y-%m-%d')}T{RUN_TIME}</StartBoundary>
      <Enabled>true</Enabled>
      <ScheduleByMonth>
        <DaysOfMonth>{days}</DaysOfMonth>
        <Months>{months}</Months>
      </ScheduleByMonth>
    </CalendarTrigger>
  </Triggers>
  <Principals>
    <Principal id="Author">
      <UserId>{escape(user_id)}</UserId>
      <LogonType>InteractiveToken</LogonType>
      <RunLevel>LeastPrivilege</RunLevel>
    </Principal>
  </Principals>
  <Settings>
    <MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>
    <DisallowStartIfOnBatteries>true</DisallowStartIfOnBatteries>
    <StopIfGoingOnBatteries>true</StopIfGoingOnBatteries>
    <AllowHardTerminate>true</AllowHardTerminate>
    <StartWhenAvailable>true</StartWhenAvailable>
    <RunOnlyIfNetworkAvailable>true</RunOnlyIfNetworkAvailable>
    <IdleSettings>
      <StopOnIdleEnd>false</StopOnIdleEnd>
      <RestartOnIdle>false</RestartOnIdle>
    </IdleSettings>
    <AllowStartOnDemand>true</AllowStartOnDemand>
    <Enabled>true</Enabled>
    <Hidden>false</Hidden>
    <RunOnlyIfIdle>false</RunOnlyIfIdle>
    <WakeToRun>true</WakeToRun>
    <ExecutionTimeLimit>{TIME_LIMIT}</ExecutionTimeLimit>
    <Priority>7</Priority>
  </Settings>
  <Actions Context="Author">
    <Exec>
      <Command>{escape(str(command))}</Command>
      <WorkingDirectory>{escape(str(working_directory))}</WorkingDirectory>
    </Exec>
  </Actions>
</Task>
"""


def current_user() -> str:
    domain = os.environ.get("USERDOMAIN", "")
    user = os.environ.get("USERNAME", "")
    if not user:
        raise SystemExit("Could not determine the current Windows user.")
    return f"{domain}\\{user}" if domain else user


def task_exists() -> bool:
    result = subprocess.run(
        ["schtasks", "/Query", "/TN", TASK_NAME],
        capture_output=True, text=True,
    )
    return result.returncode == 0


def register(start: datetime | None = None, replace: bool = False) -> None:
    if not WRAPPER.is_file():
        raise SystemExit(f"Wrapper not found: {WRAPPER}")
    if task_exists() and not replace:
        raise SystemExit(
            f"A task named {TASK_NAME!r} already exists; pass --replace to overwrite it."
        )
    xml = build_task_xml(
        current_user(), WRAPPER, REPOSITORY, start or first_start(datetime.now()),
    )
    descriptor, name = tempfile.mkstemp(suffix=".xml")
    os.close(descriptor)
    try:
        # schtasks reads task XML reliably only as UTF-16 with a byte-order mark.
        Path(name).write_text(xml, encoding="utf-16")
        command = ["schtasks", "/Create", "/TN", TASK_NAME, "/XML", name]
        if replace:
            command.append("/F")
        result = subprocess.run(command, capture_output=True, text=True)
    finally:
        Path(name).unlink(missing_ok=True)
    if result.returncode != 0:
        raise SystemExit(f"schtasks failed: {(result.stderr or result.stdout).strip()}")
    print((result.stdout or "").strip())


def unregister() -> None:
    if not task_exists():
        print(f"No task named {TASK_NAME!r} is registered.")
        return
    result = subprocess.run(
        ["schtasks", "/Delete", "/TN", TASK_NAME, "/F"], capture_output=True, text=True,
    )
    if result.returncode != 0:
        raise SystemExit(f"schtasks failed: {(result.stderr or result.stdout).strip()}")
    print((result.stdout or "").strip())


def main(arguments: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("action", choices=("show", "register", "unregister"))
    parser.add_argument("--start", help="first date the schedule may fire, YYYY-MM-DD")
    parser.add_argument("--replace", action="store_true", help="overwrite an existing task")
    args = parser.parse_args(arguments)
    start = datetime.strptime(args.start, "%Y-%m-%d") if args.start else None
    if args.action == "show":
        print(build_task_xml(current_user(), WRAPPER, REPOSITORY, start or first_start(datetime.now())))
    elif args.action == "register":
        register(start, replace=args.replace)
    else:
        unregister()


if __name__ == "__main__":
    main()
