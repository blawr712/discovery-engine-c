"""Keep Windows from idle-sleeping while a long job runs.

A monthly cycle takes most of a day, and the first October run died partway
through without a traceback. ``SetThreadExecutionState`` is the per-process
request media players use: it changes no power settings, only asks that the
machine not idle into sleep while this thread runs, and Windows releases it
when the process exits even if the release below never runs. The display may
still turn off, and a sleep the user asks for still happens. Elsewhere this is
a no-op.
"""

from __future__ import annotations

import contextlib
import sys
from typing import Callable, Iterator

ES_CONTINUOUS = 0x80000000
ES_SYSTEM_REQUIRED = 0x00000001


@contextlib.contextmanager
def keep_awake(setter: Callable[[int], int] | None = None) -> Iterator[bool]:
    """Hold an idle-sleep exemption for the duration of the block.

    Yields whether the request was accepted.
    """
    if setter is None:
        if sys.platform != "win32":
            yield False
            return
        import ctypes

        setter = ctypes.windll.kernel32.SetThreadExecutionState
    try:
        accepted = bool(setter(ES_CONTINUOUS | ES_SYSTEM_REQUIRED))
    except Exception:  # noqa: BLE001 - staying awake is best effort
        accepted = False
    try:
        yield accepted
    finally:
        try:
            setter(ES_CONTINUOUS)
        except Exception:  # noqa: BLE001 - released at process exit regardless
            pass
