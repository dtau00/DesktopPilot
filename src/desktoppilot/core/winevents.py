r"""What Windows itself recorded about the app: crashes, hangs, .NET exceptions.
On Linux: crashes, OOM kills, apport reports and core dumps (see below).

When the app dies or stops answering, the core only sees the result - the
process is gone, or UIA calls time out. Windows records the why in places no
UI call reaches:

- the Application event log: `Application Error` 1000 (crash), `Application
  Hang` 1002 (hang), `.NET Runtime` 1026 (unhandled .NET exception) and
  `Windows Error Reporting` 1001 (the WER report that follows either);
- WER report folders, `ReportArchive` / `ReportQueue` under %ProgramData% and
  %LOCALAPPDATA%\Microsoft\Windows\WER, each with a UTF-16 `Report.wer`;
- LocalDumps, when the machine has it configured.

Every record, whatever its source, has one shape (see `record`), keyed to the
process image name (`Profile.image_name`), so `WINWORD.EXE` finds Word's
crashes even when the tool never started that Word.

Parsing (`parse_event_xml`, `parse_report_wer`) is pure and separate from the
I/O, so the unit tests need no real log. A source that is missing or refused
goes into `unavailable` and never fails a call: evidence is best effort.

On Linux, `query`, `Mark.now` and `local_dumps` answer from
`desktoppilot.linux.events` (the journal, apport, systemd-coredump) in the
same shapes, and KINDS and FATAL_KINDS are its kinds.
"""

from __future__ import annotations

import json
import os
import re
import time
import xml.etree.ElementTree as ET
from collections.abc import Iterable
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path

from ..linux import events as linux_events
from . import paths
from .errors import PilotError
from .native import WINDOWS
from .util import parse_duration

if WINDOWS:
    import winreg

    import win32evtlog

LOG = "Application"
# kind -> (provider, event id). The providers are fixed by Windows.
EVENT_KINDS: dict[str, tuple[str, int]] = {
    "crash": ("Application Error", 1000),
    "hang": ("Application Hang", 1002),
    "dotnet": (".NET Runtime", 1026),
    "wer": ("Windows Error Reporting", 1001),
}
FILE_KINDS = ("report", "dump")
KINDS = (*EVENT_KINDS, *FILE_KINDS)
# The kinds that mean the app itself died or stopped answering. A WER 1001 is
# also written for non-fatal reports (StoreAgentInstallFailure, Office's
# module-version mismatch), so it is evidence, never a verdict. That mismatch
# also writes a `crash` with exception code 0, which `fatal` leaves out.
FATAL_KINDS = ("crash", "hang", "dotnet")
if not WINDOWS:
    # The kinds of linux/events.py: crash, oom, report, dump.
    KINDS = linux_events.KINDS
    FATAL_KINDS = linux_events.FATAL_KINDS
MESSAGE_LIMIT = 2000
# How many events one query renders before it stops looking. The filter on
# the image runs after rendering, so an --any-app query over days is bounded.
SCAN_LIMIT = 2000
DEFAULT_SINCE = "30m"
REPORT_PREFIXES = ("AppCrash_", "AppHang_")
# WER cuts the image to this many characters in a report folder's name.
REPORT_IMAGE_CHARS = 16
LOCAL_DUMPS_KEY = r"SOFTWARE\Microsoft\Windows\Windows Error Reporting\LocalDumps"
MARK_DIR = paths.STATE_DIR / "core"

_NS = {"e": "http://schemas.microsoft.com/win/2004/08/events/event"}
_SYSTEM_TIME_RE = re.compile(r"^(?P<base>[\d-]+T[\d:]+)(?:\.(?P<frac>\d+))?Z?$")
_DOTNET_APP_RE = re.compile(r"^Application:\s*(?P<app>\S.*?)\s*$", re.MULTILINE)
_DOTNET_EXC_RE = re.compile(r"^Exception Info:\s*(?P<exc>[\w.`+]+)", re.MULTILINE)
_DUMP_PID_RE = re.compile(r"\.(?P<pid>\d+)\.dmp$", re.IGNORECASE)
_FILETIME_EPOCH = datetime(1601, 1, 1, tzinfo=UTC)


# ------------------------------------------------------------------ shapes


def record(kind: str, **fields) -> dict:
    """One record, every key present, so a reader never has to ask which
    source a record came from before reading it."""
    base = {
        "kind": kind,
        "time": None,
        "source": None,
        "provider": None,
        "event_id": None,
        "record_id": None,
        "app": None,
        "pid": None,
        "module": None,
        "exception_code": None,
        "message": None,
        "path": None,
    }
    return base | fields


def local_iso(moment: datetime) -> str:
    """Local wall time to the millisecond, like the app's own logs, so a
    record lines up with them by eye."""
    return moment.astimezone().replace(tzinfo=None).isoformat(timespec="milliseconds")


def parse_system_time(text: str) -> datetime | None:
    """The event's `SystemTime`: UTC with 7 fractional digits, which
    datetime.fromisoformat does not take everywhere."""
    match = _SYSTEM_TIME_RE.match((text or "").strip())
    if not match:
        return None
    frac = (match["frac"] or "0")[:6].ljust(6, "0")
    return datetime.fromisoformat(f"{match['base']}.{frac}").replace(tzinfo=UTC)


def filetime(value: str | int) -> datetime | None:
    """A FILETIME (100 ns ticks since 1601, UTC), as Report.wer's EventTime."""
    try:
        return _FILETIME_EPOCH + timedelta(microseconds=int(value) // 10)
    except (TypeError, ValueError, OverflowError):
        return None


def _int(value: str | None, *, base: int = 10) -> int | None:
    if not value:
        return None
    text = value.strip()
    try:
        if text.lower().startswith("0x"):
            return int(text, 16)
        return int(text, base)
    except ValueError:
        return None


def _code(value: str | None) -> str | None:
    """'c0000005' and '0xC0000005' both become '0xc0000005'."""
    if not value:
        return None
    text = value.strip().lower()
    return text if text.startswith("0x") else "0x" + text


# ----------------------------------------------------------------- parsing


def parse_event_xml(xml: str, message: str | None = None) -> dict | None:
    """One rendered event as a record, or None when it is not one of ours.

    `message` is EvtFormatMessage's text. Without it (the publisher metadata
    could not be opened) the message is the joined EventData, not an error.
    Event data comes both named (Windows 10+ writes Data Name='AppName') and
    positional (Application Hang still writes bare Data), so each field is
    read by name first, by position second.
    """
    try:
        root = ET.fromstring(xml)
    except ET.ParseError:
        return None
    system = root.find("e:System", _NS)
    if system is None:
        return None
    provider_el = system.find("e:Provider", _NS)
    provider = provider_el.get("Name") if provider_el is not None else None
    event_id = _int(system.findtext("e:EventID", default="", namespaces=_NS))
    kind = next((k for k, (p, i) in EVENT_KINDS.items() if (p, i) == (provider, event_id)), None)
    if kind is None:
        return None

    time_el = system.find("e:TimeCreated", _NS)
    created = parse_system_time(time_el.get("SystemTime", "")) if time_el is not None else None
    data_els = root.findall("e:EventData/e:Data", _NS)
    values = [(el.text or "") for el in data_els]
    named = {el.get("Name"): (el.text or "") for el in data_els if el.get("Name")}

    def get(name: str, index: int | None = None) -> str | None:
        if name in named:
            return named[name] or None
        if index is not None and not named and index < len(values):
            return values[index] or None
        return None

    fields: dict = {
        "time": local_iso(created) if created else None,
        "source": "eventlog",
        "provider": provider,
        "event_id": event_id,
        "record_id": _int(system.findtext("e:EventRecordID", default="", namespaces=_NS)),
    }
    if kind == "crash":
        fields |= {
            "app": get("AppName", 0),
            "module": get("ModuleName", 3),
            "exception_code": _code(get("ExceptionCode", 6)),
            "pid": _int(get("ProcessId", 8)),
        }
    elif kind == "hang":
        # The hang pid is hex without a 0x ('3b1c').
        fields |= {"app": get("AppName", 0), "pid": _int(get("ProcessID", 2), base=16)}
    elif kind == "dotnet":
        text = "\n".join(values)
        app = _DOTNET_APP_RE.search(text)
        exc = _DOTNET_EXC_RE.search(text)
        # No numeric code here: the exception's type is what identifies it.
        fields |= {
            "app": app["app"] if app else None,
            "exception_code": exc["exc"] if exc else None,
        }
    elif kind == "wer":
        event_name = (get("EventName") or "").upper()
        fields |= {"app": get("P1"), "path": _strip_long_prefix(get("StorePath"))}
        if event_name == "APPCRASH":
            fields |= {"module": get("P4"), "exception_code": _code(get("P7"))}
        elif event_name.startswith("BEX"):
            fields |= {"module": get("P4"), "exception_code": _code(get("P8"))}

    body = (message or "\n".join(v for v in values if v)).replace("\r\n", "\n")
    fields["message"] = body[:MESSAGE_LIMIT] if body else None
    return record(kind, **fields)


def _strip_long_prefix(path: str | None) -> str | None:
    if not path:
        return None
    return path[4:] if path.startswith("\\\\?\\") else path


def parse_report_wer(text: str) -> dict[str, str]:
    """Report.wer's key=value lines. `Sig[n].Name`/`Sig[n].Value` pairs are
    also folded into their names ('Exception Code' -> 'c0000005') because
    the index of a field differs between event types."""
    fields: dict[str, str] = {}
    for line in text.splitlines():
        key, sep, value = line.partition("=")
        if sep:
            fields[key.strip().lstrip("\ufeff")] = value.strip()
    for key in [k for k in fields if k.startswith("Sig[") and k.endswith("].Name")]:
        value_key = key[: -len("Name")] + "Value"
        if value_key in fields:
            fields.setdefault("sig:" + fields[key], fields[value_key])
    return fields


def report_record(folder: Path, fields: dict[str, str], *, modified: float | None = None) -> dict:
    """A WER report folder, with its parsed Report.wer (may be empty)."""
    when = filetime(fields["EventTime"]) if fields.get("EventTime") else None
    if when is None and modified is not None:
        when = datetime.fromtimestamp(modified, tz=UTC)
    summary = " - ".join(v for v in (fields.get("EventType"), fields.get("FriendlyEventName")) if v)
    return record(
        "report",
        time=local_iso(when) if when else None,
        source="wer",
        provider="Windows Error Reporting",
        app=_report_image(folder, fields),
        module=fields.get("sig:Fault Module Name"),
        exception_code=_code(fields.get("sig:Exception Code")),
        message=summary or None,
        path=str(folder),
    )


def _report_image(folder: Path, fields: dict[str, str]) -> str | None:
    """The image name, the key every other record uses. `AppName` is the
    version resource's product name ('Microsoft Word' for WINWORD.EXE), so it
    comes last, after the exe's path and its original file name."""
    if fields.get("AppPath"):
        return Path(fields["AppPath"]).name
    return (
        fields.get("OriginalFilename")
        or fields.get("AppName")
        or fields.get("NsAppName")
        or _image_of_folder(folder.name)
    )


def read_report_wer(path: Path) -> str:
    """Report.wer is UTF-16 LE with a BOM; tolerate one written as UTF-8."""
    raw = path.read_bytes()
    if raw[:2] in (b"\xff\xfe", b"\xfe\xff"):
        return raw.decode("utf-16", errors="replace")
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        return raw.decode("utf-16-le", errors="replace")


def _image_of_folder(name: str) -> str | None:
    for prefix in REPORT_PREFIXES:
        if name.startswith(prefix):
            return name[len(prefix) :].split("_", 1)[0] or None
    return None


def report_folder_matches(name: str, image: str | None) -> bool:
    """`AppCrash_<image, cut to 16 chars>_<hash>...`. None matches any app's
    crash or hang report, and never the NonCritical noise."""
    lowered = name.casefold()
    for prefix in REPORT_PREFIXES:
        if not lowered.startswith(prefix.casefold()):
            continue
        if image is None:
            return True
        return lowered.startswith((prefix + image[:REPORT_IMAGE_CHARS] + "_").casefold())
    return False


def _same_app(app: str | None, image: str | None) -> bool:
    return image is None or (app or "").casefold() == image.casefold()


# -------------------------------------------------------------- event log


def event_xpath(
    kinds: Iterable[str], *, since: timedelta | None = None, after_record: int | None = None
) -> str:
    """One XPath for every kind asked for. The image filter cannot go here: a
    .NET event carries it inside a free-text message."""
    clauses = [
        f"(Provider[@Name='{EVENT_KINDS[k][0]}'] and EventID={EVENT_KINDS[k][1]})"
        for k in kinds
        if k in EVENT_KINDS
    ]
    condition = "(" + " or ".join(clauses) + ")"
    if since is not None:
        condition += (
            f" and TimeCreated[timediff(@SystemTime) <= {int(since.total_seconds() * 1000)}]"
        )
    if after_record is not None:
        condition += f" and EventRecordID > {int(after_record)}"
    return f"*[System[{condition}]]"


def newest_record_id() -> int | None:
    """The Application log's newest record id: one query, one render. None
    for an empty log."""
    handle = win32evtlog.EvtQuery(LOG, win32evtlog.EvtQueryReverseDirection, "*")
    events = win32evtlog.EvtNext(handle, 1)
    if not events:
        return None
    xml = win32evtlog.EvtRender(events[0], win32evtlog.EvtRenderEventXml)
    match = re.search(r"<EventRecordID>(\d+)</EventRecordID>", xml)
    return int(match.group(1)) if match else None


def event_records(
    image: str | None,
    *,
    kinds: Iterable[str],
    since: timedelta | None = None,
    after_record: int | None = None,
    limit: int = 50,
) -> list[dict]:
    """Matching events, newest last. Raises on a refused or missing log; the
    caller turns that into `unavailable`."""
    kinds = [k for k in kinds if k in EVENT_KINDS]
    if not kinds:
        return []
    query = event_xpath(kinds, since=since, after_record=after_record)
    handle = win32evtlog.EvtQuery(LOG, win32evtlog.EvtQueryReverseDirection, query)
    found: list[dict] = []
    publishers: dict[str, object] = {}
    scanned = 0
    while (limit <= 0 or len(found) < limit) and scanned < SCAN_LIMIT:
        batch = win32evtlog.EvtNext(handle, 64)
        if not batch:
            break
        for event in batch:
            scanned += 1
            xml = win32evtlog.EvtRender(event, win32evtlog.EvtRenderEventXml)
            parsed = parse_event_xml(xml)
            if parsed is None or not _same_app(parsed["app"], image):
                continue
            message = _format_message(event, parsed["provider"], publishers)
            found.append(parse_event_xml(xml, message) if message else parsed)
            if 0 < limit <= len(found):
                break
    found.reverse()
    return found


def _format_message(event, provider: str, cache: dict) -> str | None:
    """The event's text as Event Viewer shows it, or None when the publisher's
    metadata cannot be opened (an uninstalled runtime, a remote log)."""
    if provider in cache and cache[provider] is None:
        return None
    try:
        if provider not in cache:
            cache[provider] = win32evtlog.EvtOpenPublisherMetadata(provider)
        return win32evtlog.EvtFormatMessage(
            cache[provider], event, win32evtlog.EvtFormatMessageEvent
        )
    except Exception:  # noqa: BLE001 - a missing message falls back to the raw data
        cache[provider] = None
        return None


# ------------------------------------------------------------ WER folders


def wer_dirs() -> list[Path]:
    """Where WER keeps report folders. DESKTOPPILOT_WER_DIRS (os.pathsep list)
    replaces them for a test run."""
    override = os.environ.get("DESKTOPPILOT_WER_DIRS")
    if override:
        return [Path(p) for p in override.split(os.pathsep) if p]
    roots = [
        Path(os.environ.get("PROGRAMDATA", r"C:\ProgramData")) / "Microsoft" / "Windows" / "WER",
        Path(os.environ.get("LOCALAPPDATA", Path.home())) / "Microsoft" / "Windows" / "WER",
    ]
    return [root / sub for root in roots for sub in ("ReportArchive", "ReportQueue")]


def report_folders(image: str | None, unavailable: dict[str, str] | None = None) -> list[Path]:
    """Crash and hang report folders for the image. A folder moves from
    ReportQueue to ReportArchive under the same name once it is sent."""
    found: list[Path] = []
    for directory in wer_dirs():
        try:
            children = list(os.scandir(directory))
        except FileNotFoundError:
            if unavailable is not None:
                unavailable["wer:" + str(directory)] = "not found"
            continue
        except OSError as exc:
            if unavailable is not None:
                unavailable["wer:" + str(directory)] = exc.strerror or str(exc)
            continue
        found += [
            Path(c.path)
            for c in children
            if report_folder_matches(c.name, image) and c.is_dir(follow_symlinks=False)
        ]
    return found


def wer_reports(
    image: str | None,
    *,
    since: timedelta | None = None,
    exclude: Iterable[str] = (),
    unavailable: dict[str, str] | None = None,
) -> list[dict]:
    """Report records, oldest first. `exclude` holds folder names a mark
    already saw. The cheap filters (name, mtime) run before any Report.wer is
    read, so a machine with hundreds of old reports costs a directory listing."""
    skip = set(exclude)
    cutoff = time.time() - since.total_seconds() if since is not None else None
    records: list[dict] = []
    seen: set[str] = set()
    for folder in report_folders(image, unavailable):
        if folder.name in skip or folder.name in seen:
            continue
        try:
            modified = folder.stat().st_mtime
        except OSError:
            continue
        if cutoff is not None and modified < cutoff:
            continue
        seen.add(folder.name)
        try:
            fields = parse_report_wer(read_report_wer(folder / "Report.wer"))
        except OSError:
            fields = {}
        rec = report_record(folder, fields, modified=modified)
        if _same_app(rec["app"], image) or (image and not fields):
            records.append(rec)
    return sorted(records, key=lambda r: r["time"] or "")


# ---------------------------------------------------------------- dumps


def dump_config(image: str | None) -> dict:
    """Whether LocalDumps is configured for the image, and where it writes.
    Reads HKLM only - the tool never turns LocalDumps on, which is a
    machine-wide, elevated change."""
    default = Path(os.environ.get("LOCALAPPDATA", Path.home())) / "CrashDumps"
    config = {"configured": False, "scope": None, "folder": str(default)}
    try:
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, LOCAL_DUMPS_KEY) as key:
            config |= {"configured": True, "scope": "global"}
            folder = _reg_value(key, "DumpFolder")
            if folder:
                config["folder"] = folder
            if image:
                try:
                    with winreg.OpenKey(key, image) as app_key:
                        config["scope"] = "app"
                        folder = _reg_value(app_key, "DumpFolder")
                        if folder:
                            config["folder"] = folder
                except FileNotFoundError:
                    pass
    except FileNotFoundError:
        pass
    return config


def _reg_value(key, name: str) -> str | None:
    try:
        value, _ = winreg.QueryValueEx(key, name)
    except FileNotFoundError:
        return None
    return os.path.expandvars(str(value)) if value else None


def dump_files(folder: Path, image: str | None) -> list[Path]:
    """`<image>.<pid>.dmp`, oldest first; every dump for no image."""
    pattern = f"{image}.*.dmp" if image else "*.dmp"
    return sorted(folder.glob(pattern), key=lambda p: p.stat().st_mtime)


def dump_record(path: Path) -> dict:
    stat = path.stat()
    match = _DUMP_PID_RE.search(path.name)
    return record(
        "dump",
        time=local_iso(datetime.fromtimestamp(stat.st_mtime, tz=UTC)),
        source="localdumps",
        app=path.name[: match.start()] if match else None,
        pid=int(match["pid"]) if match else None,
        message=f"{stat.st_size} bytes",
        path=str(path),
    )


def local_dumps(image: str | None) -> dict:
    """The LocalDumps listing for the image. `configured: false` is news, not
    an error: without it Windows writes no full dump, only the WER report.
    JSON: image, configured, scope, folder, count, dumps, unavailable."""
    if not WINDOWS:
        return linux_events.local_dumps(image)
    config = dump_config(image)
    unavailable: dict[str, str] = {}
    records = _dumps(image, config, unavailable=unavailable)
    return (
        {"image": image}
        | config
        | {
            "count": len(records),
            "dumps": records,
            "unavailable": unavailable,
        }
    )


def _dumps(
    image: str | None,
    config: dict,
    *,
    since: timedelta | None = None,
    exclude: Iterable[str] = (),
    unavailable: dict[str, str] | None = None,
) -> list[dict]:
    folder = Path(config["folder"])
    if not folder.is_dir():
        if unavailable is not None and config["configured"]:
            unavailable["dump:" + str(folder)] = "not found"
        return []
    skip = set(exclude)
    cutoff = time.time() - since.total_seconds() if since is not None else None
    records = []
    try:
        for path in dump_files(folder, image):
            if str(path) in skip:
                continue
            if cutoff is not None and path.stat().st_mtime < cutoff:
                continue
            records.append(dump_record(path))
    except OSError as exc:
        if unavailable is not None:
            unavailable["dump:" + str(folder)] = exc.strerror or str(exc)
    return records


# ------------------------------------------------------------------- mark


@dataclass
class Mark:
    """Where every source stood at a point in time, so a caller can ask for
    'only what Windows recorded since'. The event log by record id (ids only
    grow), WER folders and dumps by "not in the mark's listing"."""

    image: str | None
    record_id: int | None
    reports: list[str] = field(default_factory=list)
    dumps: list[str] = field(default_factory=list)
    time: float = 0.0

    @staticmethod
    def now(image: str | None) -> Mark:
        """Cheap by design - one query for the newest record id and a few
        directory listings - because the pytest plugin takes one per test.
        A source that cannot be read marks as empty rather than failing."""
        if not WINDOWS:
            return Mark(image=image, **linux_events.mark_fields(image))
        try:
            record_id = newest_record_id()
        except Exception:  # noqa: BLE001 - an unreadable log marks as "no id"; time is the fallback
            record_id = None
        try:
            dumps = [str(p) for p in dump_files(Path(dump_config(image)["folder"]), None)]
        except OSError:
            dumps = []
        return Mark(
            image=image,
            record_id=record_id,
            # Every app's names: a mark taken for one image still serves --any-app.
            reports=sorted({p.name for p in report_folders(None)}),
            dumps=dumps,
            time=time.time(),
        )

    def to_json(self) -> dict:
        return asdict(self)

    @staticmethod
    def from_json(data: dict) -> Mark:
        return Mark(
            image=data.get("image"),
            record_id=data.get("record_id"),
            reports=list(data.get("reports", [])),
            dumps=list(data.get("dumps", [])),
            time=float(data.get("time", 0.0)),
        )

    def save(self, path: Path) -> Path:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(self.to_json(), indent=2), encoding="utf-8")
        return path

    @staticmethod
    def load(path: Path) -> Mark:
        if not path.exists():
            raise PilotError(f"no events mark at {path}; run 'events mark' first")
        return Mark.from_json(json.loads(path.read_text(encoding="utf-8")))


def mark_file(app: str) -> Path:
    return MARK_DIR / f"{app}-events-mark.json"


# ------------------------------------------------------------------ query


def query(
    image: str | None,
    since: str | timedelta | None = None,
    after_record: int | None = None,
    kinds: Iterable[str] | None = None,
    limit: int = 50,
    *,
    mark: Mark | None = None,
) -> dict:
    """Every source at once, newest last.

    `image` None means any app. `mark` scopes to what came after it and
    overrides `after_record`. Never raises for a source: what could not be
    read is in `unavailable`. JSON: image, since, count, records, unavailable.
    """
    if not WINDOWS:
        return linux_events.query(image, since, after_record, kinds, limit, mark=mark)
    wanted = list(kinds) if kinds else list(KINDS)
    unknown = [k for k in wanted if k not in KINDS]
    if unknown:
        raise PilotError(f"unknown kind(s) {unknown}; kinds: {list(KINDS)}")
    window = parse_duration(since) if isinstance(since, str) else since
    unavailable: dict[str, str] = {}
    records: list[dict] = []

    event_since = window
    if mark is not None:
        after_record = mark.record_id
        if after_record is None:
            # The log was unreadable at mark time: fall back to the clock.
            event_since = timedelta(seconds=max(time.time() - mark.time, 0.0))
    if any(k in EVENT_KINDS for k in wanted):
        try:
            if mark is not None and after_record is not None:
                newest = newest_record_id()
                if newest is not None and newest < after_record:
                    after_record = 0  # the log was cleared since the mark
            records += event_records(
                image, kinds=wanted, since=event_since, after_record=after_record, limit=limit
            )
        except Exception as exc:  # noqa: BLE001 - a refused log is `unavailable`, not a failure
            unavailable["eventlog"] = str(exc)
    if "report" in wanted:
        records += wer_reports(
            image,
            since=window,
            exclude=mark.reports if mark else (),
            unavailable=unavailable,
        )
    if "dump" in wanted:
        try:
            records += _dumps(
                image,
                dump_config(image),
                since=window,
                exclude=mark.dumps if mark else (),
                unavailable=unavailable,
            )
        except Exception as exc:  # noqa: BLE001 - registry trouble is `unavailable`
            unavailable["dump"] = str(exc)

    records.sort(key=lambda r: r["time"] or "")
    records = records[-limit:] if limit > 0 else records
    return {
        "image": image,
        "since": since if isinstance(since, str) else (str(window) if window else None),
        "count": len(records),
        "records": records,
        "unavailable": unavailable,
    }


def fatal(records: Iterable[dict], image: str | None = None) -> list[dict]:
    """The records that mean the app died or stopped answering."""
    return [
        r
        for r in records
        if r["kind"] in FATAL_KINDS and _same_app(r["app"], image) and not _code_zero(r)
    ]


def _code_zero(r: dict) -> bool:
    """A crash with exception code 0 is no crash: Office writes one (module
    'unknown', then a WER OFFICE_MODULE_VERSION_MISMATCH) for its
    module-version-mismatch report, and the process lives on."""
    return r["kind"] == "crash" and _int(r.get("exception_code")) == 0
