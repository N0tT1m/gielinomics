"""Atomic JSON writes, for the two stores that keep state in a file.

:mod:`reldo.accounts` and :mod:`reldo.progress` both persist a small dict, and
both deliberately read an unparseable file as an empty one -- losing the data
should degrade the bot rather than stop it booting. That posture is only safe
if an ordinary crash cannot *leave* the file unparseable, and ``write_text``
truncates before it writes: interrupt it and what is on disk is precisely the
half-written file both of them then shrug at, with one ``log.warning`` against
it. For accounts that reads as every user silently unlinked, and the next
symptom is generic advice several days later, which is a long way from the
cause.

Written here once rather than in both stores for the same reason
:func:`reldo.llm.client_for` exists: two copies of a durability rule is two
chances for them to drift, and the one that drifts fails silently.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any


def write_json(path: Path, data: Any, *, indent: int | None = None) -> None:
    """Serialise ``data`` to ``path`` so a reader sees all of it or none of it.

    Write beside the target and rename over it. ``os.replace`` is atomic on
    POSIX and on Windows alike, which matters here rather than academically --
    the bot runs on the Windows box.

    The temporary file goes in the target's own directory, not the system temp
    dir, because the rename is only atomic within one filesystem and those are
    routinely different ones. It is fsynced before the rename so the bytes are
    on the device rather than only in the page cache; at a few hundred KB and
    at most one write per question, that costs nothing worth measuring.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.tmp")
    try:
        with open(tmp, "w", encoding="utf-8") as handle:
            json.dump(data, handle, indent=indent)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
    except BaseException:
        # Leave the target alone and take the debris with us. Without this a
        # failed save leaves a .tmp beside a file that is still perfectly good,
        # which reads like the corruption that did not happen.
        tmp.unlink(missing_ok=True)
        raise
