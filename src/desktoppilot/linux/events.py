"""What Linux recorded about the app: the Linux side of `core/winevents.py`.

Kinds, and where each comes from:

- `crash`: the process died of a signal. The kernel's fault lines in the
  journal (`segfault at`, `traps: ... trap ...`, general protection fault),
  apport's log (`/var/log/apport.log`: every signal apport is called for,
  SIGABRT too, packaged or not), and systemd-coredump's journal entries. One
  record per pid: the sources are merged.
- `oom`: the kernel's OOM killer (`Out of memory: Killed process`).
- `report`: an apport `.crash` file in /var/crash (ProblemType: Crash).
  Apport writes one only for an executable of an installed package: a crash
  of a binary you built leaves a `crash`, never a `report`.
- `dump`: a core file, where apport (`/var/lib/apport/coredump`, only when the
  process's core limit is not 0) or systemd-coredump stores it.

Every record has `winevents.record`'s shape and is keyed to the image name
(`Profile.image_name`, the executable's file name), so `pilot APP events`
and the pytest crash check read the same JSON on both platforms. The kernel
names a process by its first 15 characters, and by the name it was started
as (a symlink's), so a kernel-only record matches on those. A source that is
missing or refused goes into `unavailable` and never fails a call.

Parsing is pure and separate from the I/O, so the unit tests need no journal.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import signal
import subprocess
import time
from datetime import datetime, timedelta
from pathlib import Path

KINDS = ("crash", "oom", "report", "dump")
FATAL_KINDS = ("crash", "oom")
COMM_CHARS = 15
SCAN_LIMIT = 5000
MESSAGE_LIMIT = 2000
CRASH_DIR = Path("/var/crash")
APPORT_LOGS = (Path("/var/log/apport.log"), Path("/var/log/apport.log.1"))
APPORT_DUMPS = Path("/var/lib/apport/coredump")
SYSTEMD_DUMPS = Path("/var/lib/systemd/coredump")
CORE_PATTERN = Path("/proc/sys/kernel/core_pattern")
COREDUMP_MESSAGE_ID = "fc2e22bc6ee647b6b90729ab34a250b1"
# A .crash file's header ends where its base64 blobs begin.
CRASH_HEAD_BYTES = 256 * 1024

_SEGV_RE = re.compile(
    r"^(?P<comm>.+?)\[(?P<pid>\d+)\]: segfault at \S+(?:.*? in (?P<module>[^\[\s]+)\[)?"
)
_TRAP_RE = re.compile(
    r"^traps: (?P<comm>.+?)\[(?P<pid>\d+)\] (?P<what>trap [a-z0-9 ]+?|general protection fault)"
    r"(?: ip[: ]| ip$|$)(?:.*? in (?P<module>[^\[\s]+)\[)?"
)
_OOM_RE = re.compile(r"Out of memory: Killed process (?P<pid>\d+) \((?P<comm>[^)]+)\)")
_APPORT_RE = re.compile(
    r"^\w+: apport \(pid (?P<apport>\d+)\) (?P<time>[\d-]+ [\d:]+),\d+: (?P<text>.*)$"
)
_APPORT_CALLED_RE = re.compile(r"called for (?:global )?pid (?P<pid>\d+), signal (?P<sig>\d+)")
_APPORT_EXE_RE = re.compile(r"^executable: (?P<exe>\S.*?)(?: \(command line .*\))?$")
# What apport did with the crash; its other lines are noise.
_APPORT_OUTCOMES = ("ignoring", ".crash")
_CORE_RE = re.compile(
    r"^core\.(?P<name>.+)\.(?P<uid>\d+)\.(?P<boot>[0-9a-f]{32})\.(?P<pid>\d+)\.(?P<time>\d+)"
)
# The signal each kernel trap delivers.
_TRAP_SIGNALS = {
    "trap invalid opcode": "SIGILL",
    "trap int3": "SIGTRAP",
    "trap divide error": "SIGFPE",
    "general protection fault": "SIGSEGV",
}


def _record(kind: str, **fields) -> dict:
    from ..core import winevents  # winevents imports this module

    return winevents.record(kind, **fields)


def _iso(epoch: float | None) -> str | None:
    from ..core import winevents

    if epoch is None:
        return None
    return winevents.local_iso(datetime.fromtimestamp(epoch).astimezone())


def _signal_name(number: int | str | None) -> str | None:
    try:
        return signal.Signals(int(number)).name
    except (TypeError, ValueError):
        return None


def matches(record: dict, image: str | None) -> bool:
    """The record is the image's: by its name, or by the kernel's cut one."""
    if image is None:
        return True
    names = {record["app"], record.get("comm")} - {None}
    return image in names or any(
        len(name) == COMM_CHARS and image.startswith(name) for name in names
    )


# ----------------------------------------------------------------- parsing


def parse_kernel(entry: dict) -> dict | None:
    """One kernel journal entry as a `crash` or `oom` record, or None."""
    text = entry.get("MESSAGE")
    if not isinstance(text, str):
        return None
    when = _iso(_realtime(entry))
    if match := _SEGV_RE.match(text):
        code = "SIGSEGV"
    elif match := _TRAP_RE.match(text):
        code = _TRAP_SIGNALS.get(match["what"])
    elif match := _OOM_RE.search(text):
        return _record(
            "oom",
            time=when,
            source="kernel",
            provider="kernel",
            app=match["comm"],
            comm=match["comm"],
            pid=int(match["pid"]),
            exception_code="SIGKILL",
            message=text[:MESSAGE_LIMIT],
        )
    else:
        return None
    return _record(
        "crash",
        time=when,
        source="kernel",
        provider="kernel",
        app=match["comm"],
        comm=match["comm"],
        pid=int(match["pid"]),
        module=match["module"],
        exception_code=code,
        message=text[:MESSAGE_LIMIT],
    )


def parse_coredump(entry: dict) -> list[dict]:
    """One systemd-coredump journal entry: a `crash`, and a `dump` when the
    core file is still stored."""
    exe = entry.get("COREDUMP_EXE")
    pid = _number(entry.get("COREDUMP_PID"))
    signame = entry.get("COREDUMP_SIGNAL_NAME") or _signal_name(entry.get("COREDUMP_SIGNAL"))
    app = Path(exe).name if exe else entry.get("COREDUMP_COMM")
    when = _iso(_realtime(entry))
    found = [
        _record(
            "crash",
            time=when,
            source="systemd-coredump",
            provider="systemd-coredump",
            app=app,
            comm=entry.get("COREDUMP_COMM"),
            pid=pid,
            exception_code=signame,
            message=str(entry.get("MESSAGE", ""))[:MESSAGE_LIMIT] or None,
            path=exe,
        )
    ]
    stored = entry.get("COREDUMP_FILENAME")
    if stored and Path(stored).exists():
        found.append(
            _record(
                "dump",
                time=when,
                source="systemd-coredump",
                app=app,
                pid=pid,
                message=f"{Path(stored).stat().st_size} bytes",
                path=stored,
            )
        )
    return found


def parse_apport_log(text: str, since: float | None = None) -> list[dict]:
    """apport.log as `crash` records: each apport run logs the pid and the
    signal, then the executable. Local times, to the second."""
    runs: dict[str, dict] = {}
    for line in text.splitlines():
        match = _APPORT_RE.match(line)
        if not match:
            continue
        try:
            when = time.mktime(time.strptime(match["time"], "%Y-%m-%d %H:%M:%S"))
        except ValueError:
            continue
        if since is not None and when < since - 1:
            continue
        run = match["apport"]
        if called := _APPORT_CALLED_RE.search(match["text"]):
            runs[run] = {
                "time": when,
                "pid": int(called["pid"]),
                "code": _signal_name(called["sig"]),
                "exe": None,
                "message": match["text"],
            }
        elif run in runs and (exe := _APPORT_EXE_RE.match(match["text"])):
            runs[run]["exe"] = exe["exe"]
        elif run in runs and match["text"].endswith(_APPORT_OUTCOMES):
            runs[run]["message"] += "; " + match["text"]
    return [
        _record(
            "crash",
            time=_iso(run["time"]),
            source="apport",
            provider="apport",
            app=Path(run["exe"]).name if run["exe"] else None,
            pid=run["pid"],
            exception_code=run["code"],
            message=run["message"][:MESSAGE_LIMIT],
            path=run["exe"],
        )
        for run in runs.values()
    ]


def parse_crash_file(text: str) -> dict[str, str]:
    """A .crash file's `Key: value` header; an indented line continues the
    key before it (ProcStatus holds the pid)."""
    fields: dict[str, str] = {}
    key = None
    for line in text.splitlines():
        if line.startswith((" ", "\t")) and key:
            fields[key] += "\n" + line.strip()
            continue
        name, sep, value = line.partition(":")
        if not sep or " " in name:
            key = None
            continue
        if value.strip() == "base64":
            break
        key = name
        fields[key] = value.strip()
    return fields


def report_record(path: Path, fields: dict[str, str], modified: float) -> dict | None:
    """A `report` for a crash, None for apport's other problem types."""
    if fields.get("ProblemType") != "Crash":
        return None
    try:
        when = time.mktime(time.strptime(fields["Date"], "%a %b %d %H:%M:%S %Y"))
    except (KeyError, ValueError):
        when = modified
    pid = re.search(r"^Pid:\s*(\d+)", fields.get("ProcStatus", ""), re.MULTILINE)
    exe = fields.get("ExecutablePath")
    signame = fields.get("SignalName") or _signal_name(fields.get("Signal"))
    return _record(
        "report",
        time=_iso(when),
        source="apport",
        provider="apport",
        app=Path(exe).name if exe else None,
        pid=int(pid[1]) if pid else None,
        exception_code=signame,
        message=" - ".join(v for v in (fields.get("Package"), signame) if v) or None,
        path=str(path),
    )


def dump_record(path: Path) -> dict:
    """A core file: apport's `core.<exe path, / as _>.<uid>.<boot>.<pid>.<time>`
    or systemd's `core.<comm>.<uid>.<boot>.<pid>.<usec>[.zst]`."""
    stat = path.stat()
    match = _CORE_RE.match(path.name)
    name = match["name"].rsplit("_", 1)[-1] if match and path.parent == APPORT_DUMPS else None
    return _record(
        "dump",
        time=_iso(stat.st_mtime),
        source="apport" if path.parent == APPORT_DUMPS else "systemd-coredump",
        app=name or (match["name"] if match else None),
        pid=int(match["pid"]) if match else None,
        message=f"{stat.st_size} bytes",
        path=str(path),
    )


def merge_crashes(records: list[dict]) -> list[dict]:
    """One `crash` per pid: the kernel's line (it names the module), with
    the executable of apport's or systemd-coredump's record for the pid."""
    by_pid: dict[int, dict] = {}
    rest = []
    rank = {"kernel": 0, "systemd-coredump": 1, "apport": 2}
    for rec in sorted(records, key=lambda r: rank.get(r["source"], 3)):
        if rec["kind"] != "crash" or rec["pid"] is None:
            rest.append(rec)
        elif rec["pid"] not in by_pid:
            by_pid[rec["pid"]] = dict(rec)
        else:
            kept = by_pid[rec["pid"]]
            if rec["path"]:
                kept["app"], kept["path"] = rec["app"], kept["path"] or rec["path"]
            kept["exception_code"] = kept["exception_code"] or rec["exception_code"]
    return rest + list(by_pid.values())


def _realtime(entry: dict) -> float | None:
    value = _number(entry.get("__REALTIME_TIMESTAMP"))
    return value / 1e6 if value is not None else None


def _number(value) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


# --------------------------------------------------------------------- I/O


def journal(*args: str, since: float | None = None) -> list[dict]:
    """journalctl entries as dicts, oldest first. Raises when journalctl is
    missing or fails; the caller turns that into `unavailable`."""
    tool = shutil.which("journalctl")
    if tool is None:
        raise OSError("journalctl not found")
    argv = [tool, "-o", "json", "--no-pager", "-q", "-n", str(SCAN_LIMIT), *args]
    if since is not None:
        argv.append(f"--since=@{int(since)}")
    done = subprocess.run(argv, capture_output=True, text=True, check=False)
    if done.returncode != 0:
        raise OSError(done.stderr.strip() or f"journalctl exited {done.returncode}")
    entries = []
    for line in done.stdout.splitlines():
        try:
            entry = json.loads(line)
        except ValueError:
            continue
        stamp = _realtime(entry)
        if since is None or stamp is None or stamp >= since:
            entries.append(entry)
    return entries


def _apport_log(since: float | None, unavailable: dict) -> list[dict]:
    found: list[dict] = []
    for path in APPORT_LOGS:
        try:
            found += parse_apport_log(path.read_text(errors="replace"), since)
        except FileNotFoundError:
            continue
        except OSError as exc:
            unavailable["apport:" + str(path)] = exc.strerror or str(exc)
    return found


def _crash_files(unavailable: dict) -> list[Path]:
    try:
        return sorted(CRASH_DIR.glob("*.crash"))
    except OSError as exc:
        unavailable["apport:" + str(CRASH_DIR)] = exc.strerror or str(exc)
        return []


def _reports(since: float | None, mark, unavailable: dict) -> list[dict]:
    seen = set(mark.reports) if mark else set()
    found = []
    for path in _crash_files(unavailable):
        try:
            modified = path.stat().st_mtime
            if since is not None and modified < since:
                continue
            if path.name in seen and mark is not None and modified <= mark.time:
                continue
            with open(path, "rb") as file:
                head = file.read(CRASH_HEAD_BYTES).decode("utf-8", errors="replace")
        except OSError as exc:
            unavailable["apport:" + str(path)] = exc.strerror or str(exc)
            continue
        rec = report_record(path, parse_crash_file(head), modified)
        if rec is not None:
            found.append(rec)
    return found


def _core_files() -> list[Path]:
    found: list[Path] = []
    for folder in (APPORT_DUMPS, SYSTEMD_DUMPS):
        try:
            found += [p for p in folder.iterdir() if p.name.startswith("core.")]
        except OSError:
            continue
    return found


def _dumps(since: float | None, mark) -> list[dict]:
    seen = set(mark.dumps) if mark else set()
    found = []
    for path in _core_files():
        try:
            if str(path) in seen or (since is not None and path.stat().st_mtime < since):
                continue
            found.append(dump_record(path))
        except OSError:
            continue
    return found


# ------------------------------------------------------------------- query


def query(image, since=None, after_record=None, kinds=None, limit=50, *, mark=None) -> dict:
    """As `winevents.query`: every source at once, newest last, scoped by
    `since` or by `mark` (a `winevents.Mark` from `mark_fields`; Linux marks
    by time, so `after_record` is unused). JSON: image, since, count,
    records, unavailable."""
    from ..core.errors import PilotError
    from ..core.util import parse_duration

    wanted = list(kinds) if kinds else list(KINDS)
    unknown = [k for k in wanted if k not in KINDS]
    if unknown:
        raise PilotError(f"unknown kind(s) {unknown}; kinds: {list(KINDS)}")
    window = parse_duration(since) if isinstance(since, str) else since
    cutoff = mark.time if mark is not None else _cutoff(window)
    unavailable: dict[str, str] = {}
    records: list[dict] = []

    if "crash" in wanted or "oom" in wanted:
        try:
            records += [r for e in journal("-k", since=cutoff) if (r := parse_kernel(e))]
        except Exception as exc:  # noqa: BLE001 - a refused journal is `unavailable`
            unavailable["journal:kernel"] = str(exc)
    if "crash" in wanted or "dump" in wanted:
        try:
            entries = journal(f"MESSAGE_ID={COREDUMP_MESSAGE_ID}", since=cutoff)
            records += [r for e in entries for r in parse_coredump(e)]
        except Exception as exc:  # noqa: BLE001
            unavailable["journal:systemd-coredump"] = str(exc)
    if "crash" in wanted:
        records += _apport_log(cutoff, unavailable)
    if "report" in wanted:
        records += _reports(cutoff, mark, unavailable)
    if "dump" in wanted:
        records += _dumps(cutoff, mark)

    records = [r for r in merge_crashes(records) if r["kind"] in wanted and matches(r, image)]
    for rec in records:
        rec.pop("comm", None)
    records.sort(key=lambda r: r["time"] or "")
    records = records[-limit:] if limit > 0 else records
    return {
        "image": image,
        "since": since if isinstance(since, str) else (str(window) if window else None),
        "count": len(records),
        "records": records,
        "unavailable": unavailable,
    }


def _cutoff(window: timedelta | None) -> float | None:
    return time.time() - window.total_seconds() if window is not None else None


def mark_fields(image: str | None) -> dict:
    """`winevents.Mark` fields for now: record_id None (the journal is scoped
    by time), reports (the .crash file names), dumps (core file paths), time.
    Cheap and never raising: the pytest plugin takes one per test."""
    return {
        "record_id": None,
        "reports": [p.name for p in _crash_files({})],
        "dumps": sorted(str(p) for p in _core_files()),
        "time": time.time(),
    }


# ------------------------------------------------------------------- dumps


def dump_config(pattern: str, core_limit: int) -> dict:
    """Who takes a core from the kernel's core_pattern, and whether a core
    file is kept: apport and a plain file pattern keep one only when the
    process's core limit is not 0 (`ulimit -c`); systemd-coredump always."""
    if "systemd-coredump" in pattern:
        handler, folder, keeps = "systemd-coredump", str(SYSTEMD_DUMPS), True
    elif "apport" in pattern:
        handler, folder, keeps = "apport", str(APPORT_DUMPS), core_limit != 0
    elif pattern.startswith("|"):
        handler, folder, keeps = "pipe", None, False
    else:
        folder = os.path.dirname(pattern) or None  # None: the process's cwd
        handler, keeps = "file", core_limit != 0
    return {
        "configured": keeps,
        "scope": "global",
        "folder": folder,
        "core_pattern": pattern,
        "handler": handler,
    }


def _apport_active() -> bool | None:
    tool = shutil.which("systemctl")
    if tool is None:
        return None
    done = subprocess.run(
        [tool, "is-active", "apport"], capture_output=True, text=True, check=False
    )
    return done.stdout.strip() == "active"


def local_dumps(image: str | None) -> dict:
    """As `winevents.local_dumps`, from the kernel's core_pattern. JSON:
    image, configured, scope, folder, count, dumps, unavailable,
    core_pattern, handler, apport_active."""
    import resource

    unavailable: dict[str, str] = {}
    try:
        pattern = CORE_PATTERN.read_text().strip()
    except OSError as exc:
        pattern = ""
        unavailable["core_pattern"] = exc.strerror or str(exc)
    config = dump_config(pattern, resource.getrlimit(resource.RLIMIT_CORE)[0])
    records = [r for r in _dumps(None, None) if matches(r, image)]
    records.sort(key=lambda r: r["time"] or "")
    return (
        {"image": image}
        | config
        | {
            "count": len(records),
            "dumps": records,
            "unavailable": unavailable,
            "apport_active": _apport_active(),
        }
    )
