"""Linux crash records: parsing the kernel's lines, apport's log and .crash
files, systemd-coredump entries and core file names, and one query over
faked sources. No journal is read."""

from __future__ import annotations

import os
import sys
import time

import pytest

from desktoppilot.core import winevents
from desktoppilot.linux import events

pytestmark = pytest.mark.skipif(not sys.platform.startswith("linux"), reason="Linux crash sources")

NOW = time.time()


def entry(message: str, at: float = NOW) -> dict:
    return {"MESSAGE": message, "__REALTIME_TIMESTAMP": str(int(at * 1e6))}


SEGV = (
    "python3[154849]: segfault at 0 ip 000079faae1aaa1c sp 00007ffd0bf9fe88 error 4 in "
    "libc.so.6[1aaa1c,79faae028000+198000] likely on CPU 3 (core 1, socket 0)"
)
TRAP = "traps: mousepad[77] trap invalid opcode ip:5555 sp:7ffc error:0 in mousepad[5555+2000]"
GPF = "traps: a-very-long-nam[88] general protection fault ip:7f sp:7ffd error:0 in libx.so[7f+1]"
OOM = "Out of memory: Killed process 4242 (code) total-vm:1234kB, anon-rss:99kB"

APPORT_LOG = """\
INFO: apport (pid 10) 2026-10-18 14:49:34,851: called for global pid 154786, signal 11, core limit 0, dump mode 1
ERROR: apport (pid 10) 2026-10-18 14:49:35,852: gdbus call error: Error: noise
INFO: apport (pid 10) 2026-10-18 14:49:35,854: executable: /home/me/segv-probe (command line "./segv-probe")
ERROR: apport (pid 10) 2026-10-18 14:49:35,854: executable does not belong to a package, ignoring
INFO: apport (pid 11) 2026-10-18 14:49:35,959: called for global pid 154824, signal 6, core limit 0, dump mode 1
INFO: apport (pid 11) 2026-10-18 14:49:36,961: executable: /usr/bin/mousepad (command line "mousepad")
INFO: apport (pid 11) 2026-10-18 14:49:38,778: wrote report /var/crash/_usr_bin_mousepad.1000.crash
"""

CRASH_FILE = """\
ProblemType: Crash
Architecture: amd64
Date: Sun Oct 18 14:49:37 2026
Dependencies:
 libc6 2.43
ExecutablePath: /usr/bin/mousepad
Package: mousepad 0.6.5-1
ProcStatus:
 Name:\tmousepad
 Pid:\t154824
Signal: 6
SignalName: SIGABRT
CoreDump: base64
 H4sICAAAAAAC/0NvcmVEdW1wAA==
"""


# ----------------------------------------------------------------- kernel


def test_a_segfault_line_is_a_crash_with_its_module():
    rec = events.parse_kernel(entry(SEGV))
    assert (rec["kind"], rec["app"], rec["pid"]) == ("crash", "python3", 154849)
    assert (rec["module"], rec["exception_code"], rec["source"]) == (
        "libc.so.6",
        "SIGSEGV",
        "kernel",
    )
    assert rec["time"] and rec["message"] == SEGV


@pytest.mark.parametrize(
    ("line", "app", "code"),
    [(TRAP, "mousepad", "SIGILL"), (GPF, "a-very-long-nam", "SIGSEGV")],
)
def test_a_trap_is_a_crash_with_the_signal_it_delivers(line, app, code):
    rec = events.parse_kernel(entry(line))
    assert (rec["kind"], rec["app"], rec["exception_code"]) == ("crash", app, code)


def test_an_oom_kill_is_its_own_kind():
    rec = events.parse_kernel(entry(OOM))
    assert (rec["kind"], rec["app"], rec["pid"], rec["exception_code"]) == (
        "oom",
        "code",
        4242,
        "SIGKILL",
    )


def test_other_kernel_lines_are_not_records():
    assert events.parse_kernel(entry("[UFW BLOCK] IN=eth0 OUT= SRC=fe80::1")) is None
    assert events.parse_kernel({"MESSAGE": [1, 2]}) is None


def test_the_kernels_cut_name_matches_the_full_image():
    rec = events.parse_kernel(entry(GPF))
    assert events.matches(rec, "a-very-long-name-indeed")
    assert not events.matches(rec, "a-very-long-nam-other"[:14])
    assert events.matches(rec, None)


# ----------------------------------------------------------------- apport


def test_apport_log_names_every_signal_with_its_executable():
    found = events.parse_apport_log(APPORT_LOG)
    assert [(r["app"], r["pid"], r["exception_code"]) for r in found] == [
        ("segv-probe", 154786, "SIGSEGV"),
        ("mousepad", 154824, "SIGABRT"),
    ]
    assert found[0]["path"] == "/home/me/segv-probe"
    assert "ignoring" in found[0]["message"] and "noise" not in found[0]["message"]
    assert "wrote report" in found[1]["message"]


def test_apport_log_lines_before_since_are_left_out():
    cutoff = time.mktime(time.strptime("2026-10-18 14:49:35", "%Y-%m-%d %H:%M:%S")) + 0.9
    assert [r["pid"] for r in events.parse_apport_log(APPORT_LOG, cutoff)] == [154824]


def test_a_crash_file_is_a_report_with_its_pid_and_signal(tmp_path):
    path = tmp_path / "_usr_bin_mousepad.1000.crash"
    fields = events.parse_crash_file(CRASH_FILE)
    assert "CoreDump" not in fields and fields["ProcStatus"].endswith("Pid:\t154824".strip())
    rec = events.report_record(path, fields, modified=1.0)
    assert (rec["kind"], rec["app"], rec["pid"], rec["exception_code"]) == (
        "report",
        "mousepad",
        154824,
        "SIGABRT",
    )
    assert rec["path"] == str(path) and rec["time"].startswith("2026-10-18T14:49:37")


def test_a_crash_file_of_another_problem_type_is_not_a_report(tmp_path):
    fields = events.parse_crash_file(CRASH_FILE.replace("Crash", "Package", 1))
    assert events.report_record(tmp_path / "x.crash", fields, modified=1.0) is None


# ---------------------------------------------------------- coredump, dumps


def test_a_systemd_coredump_entry_is_a_crash_and_its_stored_core_a_dump(tmp_path):
    core = tmp_path / "core.mousepad.1000.0123456789abcdef0123456789abcdef.77.1792306285000000.zst"
    core.write_bytes(b"core")
    found = events.parse_coredump(
        entry("Process 77 (mousepad) of user 1000 dumped core.")
        | {
            "COREDUMP_PID": "77",
            "COREDUMP_EXE": "/usr/bin/mousepad",
            "COREDUMP_COMM": "mousepad",
            "COREDUMP_SIGNAL": "11",
            "COREDUMP_FILENAME": str(core),
        }
    )
    assert [(r["kind"], r["app"], r["pid"]) for r in found] == [
        ("crash", "mousepad", 77),
        ("dump", "mousepad", 77),
    ]
    assert found[0]["exception_code"] == "SIGSEGV" and found[1]["path"] == str(core)


def test_an_apport_core_file_names_the_executable_and_the_pid(tmp_path, monkeypatch):
    monkeypatch.setattr(events, "APPORT_DUMPS", tmp_path)
    core = tmp_path / "core._usr_bin_mousepad.1000.0123456789abcdef0123456789abcdef.77.1792306285"
    core.write_bytes(b"core")
    rec = events.dump_record(core)
    assert (rec["kind"], rec["app"], rec["pid"], rec["source"]) == (
        "dump",
        "mousepad",
        77,
        "apport",
    )


def test_one_crash_per_pid_keeps_the_kernels_module_and_apports_executable():
    kernel = events.parse_kernel(entry(SEGV))
    apport = events.parse_apport_log(APPORT_LOG.replace("154786", "154849"))[0]
    merged = events.merge_crashes([apport, kernel])
    crashes = [r for r in merged if r["pid"] == 154849]
    assert len(crashes) == 1
    assert (crashes[0]["source"], crashes[0]["module"], crashes[0]["app"]) == (
        "kernel",
        "libc.so.6",
        "segv-probe",
    )


# ------------------------------------------------------------------ query


@pytest.fixture
def sources(tmp_path, monkeypatch):
    """Every source in tmp_path; the journal answers from `journal`."""
    state = {"kernel": [entry(SEGV), entry(OOM), entry(TRAP, at=NOW - 3600)], "coredump": []}

    def journal(*args, since=None):
        rows = state["kernel"] if args == ("-k",) else state["coredump"]
        return [r for r in rows if since is None or int(r["__REALTIME_TIMESTAMP"]) / 1e6 >= since]

    crash_dir = tmp_path / "crash"
    crash_dir.mkdir()
    (crash_dir / "_usr_bin_mousepad.1000.crash").write_text(CRASH_FILE)
    monkeypatch.setattr(events, "journal", journal)
    monkeypatch.setattr(events, "CRASH_DIR", crash_dir)
    monkeypatch.setattr(events, "APPORT_LOGS", (tmp_path / "apport.log",))
    monkeypatch.setattr(events, "APPORT_DUMPS", tmp_path / "apport-cores")
    monkeypatch.setattr(events, "SYSTEMD_DUMPS", tmp_path / "systemd-cores")
    return state | {"crash_dir": crash_dir}


def test_the_query_keeps_the_images_records_newest_last(sources):
    result = winevents.query("python3", since="30m")
    assert [r["kind"] for r in result["records"]] == ["crash"]
    assert result["unavailable"] == {} and result["since"] == "30m"
    assert "comm" not in result["records"][0]

    every = winevents.query(None, since="2h")
    assert {r["kind"] for r in every["records"]} == {"crash", "oom", "report"}
    assert every["count"] == 4


def test_the_query_takes_kinds_and_refuses_unknown_ones(sources):
    assert [r["kind"] for r in winevents.query(None, since="2h", kinds=["oom"])["records"]] == [
        "oom"
    ]
    with pytest.raises(Exception, match="unknown kind"):
        winevents.query(None, kinds=["hang"])


def test_a_mark_scopes_to_what_came_after(sources):
    mark = winevents.Mark.now(None)
    assert mark.record_id is None and mark.reports == ["_usr_bin_mousepad.1000.crash"]
    assert winevents.query(None, mark=mark)["count"] == 0

    later = time.time() + 5
    sources["kernel"].append(entry(OOM, at=later))
    report = sources["crash_dir"] / "_usr_bin_mousepad.1000.crash"
    os.utime(report, (later, later))  # apport rewrote the same file name
    assert {r["kind"] for r in winevents.query(None, mark=mark)["records"]} == {"oom", "report"}


def test_a_refused_journal_is_unavailable_not_an_error(sources, monkeypatch):
    def refused(*args, since=None):
        raise OSError("No journal files were opened due to insufficient permissions.")

    monkeypatch.setattr(events, "journal", refused)
    result = winevents.query(None, since="2h")
    assert set(result["unavailable"]) == {"journal:kernel", "journal:systemd-coredump"}
    assert [r["kind"] for r in result["records"]] == ["report"]


def test_fatal_is_crash_and_oom():
    assert winevents.KINDS == ("crash", "oom", "report", "dump")
    records = [events.parse_kernel(entry(SEGV)), events.parse_kernel(entry(OOM))]
    records.append(winevents.record("report", app="python3"))
    assert [r["kind"] for r in winevents.fatal(records)] == ["crash", "oom"]


# ------------------------------------------------------------------ dumps


@pytest.mark.parametrize(
    ("pattern", "limit", "handler", "configured", "folder"),
    [
        ("|/usr/share/apport/apport -p%p -s%s -- %E", 0, "apport", False, events.APPORT_DUMPS),
        ("|/usr/share/apport/apport -p%p -s%s -- %E", -1, "apport", True, events.APPORT_DUMPS),
        (
            "|/usr/lib/systemd/systemd-coredump %P %u",
            0,
            "systemd-coredump",
            True,
            events.SYSTEMD_DUMPS,
        ),
        ("core", -1, "file", True, None),
        ("/var/cores/core.%e.%p", 0, "file", False, "/var/cores"),
        ("|/usr/bin/other %p", -1, "pipe", False, None),
    ],
)
def test_who_keeps_a_core_comes_from_the_core_pattern(pattern, limit, handler, configured, folder):
    config = events.dump_config(pattern, limit)
    assert (config["handler"], config["configured"]) == (handler, configured)
    assert config["folder"] == (str(folder) if folder else None)
    assert config["core_pattern"] == pattern and config["scope"] == "global"


def test_local_dumps_lists_the_images_cores(sources, tmp_path, monkeypatch):
    cores = tmp_path / "apport-cores"
    cores.mkdir()
    for name in ("mousepad", "gedit"):
        (cores / f"core._usr_bin_{name}.1000.{'0' * 32}.5.1792306285").write_bytes(b"c")
    pattern = tmp_path / "core_pattern"
    pattern.write_text("|/usr/share/apport/apport -p%p -- %E\n")
    monkeypatch.setattr(events, "CORE_PATTERN", pattern)
    monkeypatch.setattr(events, "_apport_active", lambda: True)

    listing = winevents.local_dumps("mousepad")

    assert [d["app"] for d in listing["dumps"]] == ["mousepad"] and listing["count"] == 1
    assert listing["handler"] == "apport" and listing["apport_active"] is True
    for key in ("image", "configured", "scope", "folder", "unavailable", "core_pattern"):
        assert key in listing
