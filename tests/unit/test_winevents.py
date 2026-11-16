"""Windows event records: parsing fixed samples, the mark, unavailable
sources, and the pytest plugin failing a test the app crashed in. No real log
is needed and nothing crashes."""

from __future__ import annotations

import json
import os
import sys
import time
from datetime import timedelta
from pathlib import Path

import pytest
from typer.testing import CliRunner

from desktoppilot.core import pytest_plugin, winevents
from desktoppilot.core.errors import PilotError

from .compat import PLAIN_APP

NS = "http://schemas.microsoft.com/win/2004/08/events/event"


def event(provider: str, event_id: int, data: str, *, record_id: int = 100) -> str:
    return (
        f"<Event xmlns='{NS}'><System><Provider Name='{provider}'/>"
        f"<EventID Qualifiers='0'>{event_id}</EventID>"
        f"<TimeCreated SystemTime='2026-09-27T02:43:05.3221123Z'/>"
        f"<EventRecordID>{record_id}</EventRecordID><Channel>Application</Channel>"
        f"</System><EventData>{data}</EventData></Event>"
    )


CRASH = event(
    "Application Error",
    1000,
    "<Data Name='AppName'>WINWORD.EXE</Data><Data Name='AppVersion'>16.0.1</Data>"
    "<Data Name='AppTimeStamp'>6a96ded9</Data><Data Name='ModuleName'>mso40uiwin32client.dll</Data>"
    "<Data Name='ModuleVersion'>0.0.0.0</Data><Data Name='ModuleTimeStamp'>00000000</Data>"
    "<Data Name='ExceptionCode'>c0000005</Data><Data Name='FaultingOffset'>0</Data>"
    "<Data Name='ProcessId'>0x1894</Data>",
    record_id=48211,
)
HANG = event(
    "Application Hang",
    1002,
    "<Data>WINWORD.EXE</Data><Data>16.0.17328.20162</Data><Data>3b1c</Data>"
    "<Data>01da8b</Data><Data>4294967295</Data><Data>C:\\Office\\WINWORD.EXE</Data>"
    "<Data>2d5e</Data><Data></Data><Data></Data><Data>Unknown</Data>",
)
DOTNET = event(
    ".NET Runtime",
    1026,
    "<Data>Application: WINWORD.EXE\nFramework Version: v4.0.30319\n"
    "Description: The process was terminated due to an unhandled exception.\n"
    "Exception Info: System.NullReferenceException\n   at MyAddin.Pane.Load()\n</Data>",
)
WER = event(
    "Windows Error Reporting",
    1001,
    "<Data Name='Bucket'>1</Data><Data Name='EventName'>BEX64</Data>"
    "<Data Name='P1'>python.exe</Data><Data Name='P4'>StackHash_ac46</Data>"
    "<Data Name='P8'>c0000005</Data>"
    "<Data Name='StorePath'>\\\\?\\C:\\ProgramData\\WER\\ReportArchive\\AppCrash_python.exe_1</Data>",
)


# ----------------------------------------------------------------- parsing


def test_crash_event():
    r = winevents.parse_event_xml(CRASH)
    assert r["kind"] == "crash" and r["source"] == "eventlog"
    assert (r["app"], r["pid"], r["module"]) == ("WINWORD.EXE", 0x1894, "mso40uiwin32client.dll")
    assert r["exception_code"] == "0xc0000005"
    assert (r["provider"], r["event_id"], r["record_id"]) == ("Application Error", 1000, 48211)
    assert r["time"].startswith("2026-09-27T") and len(r["time"]) == 23


def test_crash_event_keeps_the_formatted_message_capped():
    r = winevents.parse_event_xml(CRASH, "x\r\n" * 5000)
    assert len(r["message"]) == winevents.MESSAGE_LIMIT and "\r" not in r["message"]


def test_hang_event_reads_positional_data_and_a_hex_pid():
    r = winevents.parse_event_xml(HANG)
    assert (r["kind"], r["app"], r["pid"]) == ("hang", "WINWORD.EXE", 0x3B1C)


def test_dotnet_event_takes_the_app_from_the_message():
    r = winevents.parse_event_xml(DOTNET)
    assert (r["kind"], r["app"]) == ("dotnet", "WINWORD.EXE")
    assert r["exception_code"] == "System.NullReferenceException"
    assert "MyAddin.Pane.Load" in r["message"]


def test_wer_event():
    r = winevents.parse_event_xml(WER)
    assert (r["kind"], r["app"], r["module"]) == ("wer", "python.exe", "StackHash_ac46")
    assert r["exception_code"] == "0xc0000005"
    assert r["path"] == "C:\\ProgramData\\WER\\ReportArchive\\AppCrash_python.exe_1"


def test_without_a_formatted_message_the_data_is_the_message():
    assert "WINWORD.EXE" in winevents.parse_event_xml(CRASH)["message"]


def test_other_events_and_garbage_are_not_records():
    assert winevents.parse_event_xml(event("Application Error", 1001, "")) is None
    assert winevents.parse_event_xml(event("MsiInstaller", 1000, "")) is None
    assert winevents.parse_event_xml("<not xml") is None


def test_xpath_names_every_kind_and_the_limits():
    xpath = winevents.event_xpath(["crash", "report"], since=timedelta(minutes=5), after_record=7)
    assert "Provider[@Name='Application Error'] and EventID=1000" in xpath
    assert "Hang" not in xpath and "report" not in xpath
    assert "timediff(@SystemTime) <= 300000" in xpath and "EventRecordID > 7" in xpath


REPORT_WER = (
    "Version=1\nEventType=BEX64\nEventTime=134349505853616430\n"
    "Sig[0].Name=Application Name\nSig[0].Value=python.exe\n"
    "Sig[3].Name=Fault Module Name\nSig[3].Value=StackHash_ac46\n"
    "Sig[7].Name=Exception Code\nSig[7].Value=c0000005\n"
    "FriendlyEventName=Stopped working\nAppName=python.exe\n"
)


def write_report(parent, name: str, text: str = REPORT_WER):
    folder = parent / name
    folder.mkdir(parents=True)
    (folder / "Report.wer").write_bytes(text.encode("utf-16"))
    return folder


def test_report_wer_is_utf16_key_value(tmp_path):
    folder = write_report(tmp_path, "AppCrash_python.exe_abc_1")
    fields = winevents.parse_report_wer(winevents.read_report_wer(folder / "Report.wer"))
    assert fields["AppName"] == "python.exe"
    assert fields["sig:Exception Code"] == "c0000005"
    r = winevents.report_record(folder, fields)
    assert (r["kind"], r["app"], r["module"]) == ("report", "python.exe", "StackHash_ac46")
    assert r["exception_code"] == "0xc0000005" and r["path"] == str(folder)
    assert r["message"] == "BEX64 - Stopped working"
    assert r["time"].startswith("2026-09-")


WORD_REPORT_WER = (
    "Version=1\nEventType=BEX64\nEventTime=134349505853616430\n"
    "Sig[0].Name=Application Name\nSig[0].Value=WINWORD.EXE\n"
    "Sig[7].Name=Exception Code\nSig[7].Value=c0000409\n"
    "AppName=Microsoft Word\n"
    "AppPath=C:\\Program Files\\Microsoft Office\\root\\Office16\\WINWORD.EXE\n"
    "OriginalFilename=WinWord.exe\n"
)


@pytest.mark.windows("the Windows event log, WER reports and LocalDumps")
def test_a_report_is_keyed_to_the_image_not_the_product_name(wer):
    write_report(wer, "AppCrash_WINWORD.EXE_abc_1", WORD_REPORT_WER)
    [r] = winevents.wer_reports("WINWORD.EXE")
    assert r["app"] == "WINWORD.EXE" and r["exception_code"] == "0xc0000409"
    assert winevents.query("WINWORD.EXE", kinds=["report"])["count"] == 1


def test_a_report_without_app_path_falls_back_to_the_original_file_name(tmp_path):
    fields = {"AppName": "Microsoft Word", "OriginalFilename": "WinWord.exe"}
    r = winevents.report_record(tmp_path / "AppCrash_WINWORD.EXE_1", fields)
    assert r["app"] == "WinWord.exe"
    assert winevents.report_record(tmp_path / "AppCrash_WINWORD.EXE_1", {})["app"] == "WINWORD.EXE"


def test_report_folders_match_the_cut_image_name():
    image = "Contoso.Web.Hosting.exe"
    assert winevents.report_folder_matches("AppCrash_Contoso.Web.Host_322b_dc5d_f21a", image)
    assert winevents.report_folder_matches("apphang_contoso.web.host_1", image)
    assert not winevents.report_folder_matches("AppCrash_python.exe_1", image)
    assert not winevents.report_folder_matches("NonCritical_python.exe_1", None)
    assert winevents.report_folder_matches("AppCrash_python.exe_1", None)


# -------------------------------------------------- sources, mark, query


@pytest.fixture
def wer(tmp_path, monkeypatch):
    """A WER folder in tmp_path, no event log and no dumps."""
    archive = tmp_path / "ReportArchive"
    archive.mkdir()
    monkeypatch.setenv("DESKTOPPILOT_WER_DIRS", str(archive))
    monkeypatch.setattr(winevents, "newest_record_id", lambda: 10)
    monkeypatch.setattr(winevents, "event_records", lambda image, **k: [])
    monkeypatch.setattr(
        winevents,
        "dump_config",
        lambda image: {"configured": False, "scope": None, "folder": str(tmp_path / "dumps")},
    )
    return archive


def test_wer_reports_filter_on_the_image(wer):
    write_report(wer, "AppCrash_python.exe_abc_1")
    write_report(wer, "AppCrash_WINWORD.EXE_abc_1", REPORT_WER.replace("python", "WINWORD"))
    write_report(wer, "NonCritical_Update_1")
    assert [r["app"] for r in winevents.wer_reports("python.exe")] == ["python.exe"]
    assert len(winevents.wer_reports(None)) == 2


def test_wer_reports_skip_folders_older_than_since(wer):
    folder = write_report(wer, "AppCrash_python.exe_abc_1")
    old = time.time() - 3600
    os.utime(folder, (old, old))
    assert winevents.wer_reports("python.exe", since=timedelta(minutes=5)) == []


@pytest.mark.windows("the Windows event log, WER reports and LocalDumps")
def test_since_mark_returns_only_what_came_after(wer, monkeypatch):
    write_report(wer, "AppCrash_python.exe_abc_1")
    mark = winevents.Mark.now("python.exe")
    assert mark.record_id == 10 and mark.reports == ["AppCrash_python.exe_abc_1"]

    seen = {}

    def events(image, *, kinds, since, after_record, limit):
        seen["after"] = after_record
        return [winevents.parse_event_xml(CRASH.replace("WINWORD.EXE", "python.exe"))]

    monkeypatch.setattr(winevents, "event_records", events)
    monkeypatch.setattr(winevents, "newest_record_id", lambda: 12)
    write_report(wer, "AppCrash_python.exe_abc_2")

    result = winevents.query("python.exe", mark=mark)
    assert seen["after"] == 10
    assert sorted(r["kind"] for r in result["records"]) == ["crash", "report"]
    assert [r["path"] for r in result["records"] if r["kind"] == "report"] == [
        str(wer / "AppCrash_python.exe_abc_2")
    ]


@pytest.mark.windows("the Windows event log, WER reports and LocalDumps")
def test_a_mark_survives_a_cleared_log(wer, monkeypatch):
    seen = {}
    monkeypatch.setattr(winevents, "newest_record_id", lambda: 3)
    monkeypatch.setattr(winevents, "event_records", lambda image, **k: seen.update(k) or [])
    winevents.query("python.exe", mark=winevents.Mark("python.exe", record_id=10))
    assert seen["after_record"] == 0


@pytest.mark.windows("the Windows event log, WER reports and LocalDumps")
def test_a_mark_without_a_record_id_falls_back_to_its_time(wer, monkeypatch):
    seen = {}
    monkeypatch.setattr(winevents, "event_records", lambda image, **k: seen.update(k) or [])
    mark = winevents.Mark("python.exe", record_id=None, time=time.time() - 60)
    winevents.query("python.exe", mark=mark)
    assert seen["after_record"] is None
    assert timedelta(seconds=59) < seen["since"] < timedelta(seconds=70)


def test_mark_round_trips_and_a_missing_one_is_a_user_error(tmp_path):
    mark = winevents.Mark("python.exe", 5, ["AppCrash_x"], ["a.dmp"], 1.5)
    path = mark.save(tmp_path / "core" / "m.json")
    assert winevents.Mark.load(path) == mark
    with pytest.raises(PilotError, match="events mark"):
        winevents.Mark.load(tmp_path / "missing.json")


def test_mark_file_lives_in_the_core_state_dir():
    path = winevents.mark_file("notepad")
    assert path.name == "notepad-events-mark.json" and path.parent.name == "core"


@pytest.mark.windows("the Windows event log, WER reports and LocalDumps")
def test_unavailable_sources_never_fail_the_call(tmp_path, monkeypatch):
    monkeypatch.setenv("DESKTOPPILOT_WER_DIRS", str(tmp_path / "nowhere"))

    def refused(image, **k):
        raise OSError("access denied")

    monkeypatch.setattr(winevents, "event_records", refused)
    monkeypatch.setattr(
        winevents,
        "dump_config",
        lambda image: {"configured": True, "scope": "app", "folder": str(tmp_path / "nodumps")},
    )
    result = winevents.query("python.exe", since="5m")
    assert result["records"] == []
    assert result["unavailable"]["eventlog"] == "access denied"
    assert result["unavailable"]["wer:" + str(tmp_path / "nowhere")] == "not found"
    assert result["unavailable"]["dump:" + str(tmp_path / "nodumps")] == "not found"


@pytest.mark.windows("the Windows event log, WER reports and LocalDumps")
def test_unknown_kind_is_a_user_error():
    with pytest.raises(PilotError, match="unknown kind"):
        winevents.query("python.exe", kinds=["segfault"])


@pytest.mark.windows("the Windows event log, WER reports and LocalDumps")
def test_dumps_by_image_with_their_pid(tmp_path, monkeypatch):
    (tmp_path / "python.exe.4242.dmp").write_bytes(b"MDMP")
    (tmp_path / "WINWORD.EXE.1.dmp").write_bytes(b"MDMP")
    monkeypatch.setattr(
        winevents,
        "dump_config",
        lambda image: {"configured": True, "scope": "global", "folder": str(tmp_path)},
    )
    listing = winevents.local_dumps("python.exe")
    assert listing["configured"] and listing["count"] == 1
    assert (listing["dumps"][0]["pid"], listing["dumps"][0]["app"]) == (4242, "python.exe")


@pytest.mark.windows("the Windows kinds")
def test_fatal_is_crash_hang_dotnet_for_the_app():
    records = [winevents.parse_event_xml(x) for x in (CRASH, HANG, DOTNET, WER)]
    assert [r["kind"] for r in winevents.fatal(records, "winword.exe")] == [
        "crash",
        "hang",
        "dotnet",
    ]


def test_a_crash_with_code_zero_is_not_fatal():
    office = CRASH.replace("c0000005", "00000000").replace("mso40uiwin32client.dll", "unknown")
    records = [winevents.parse_event_xml(office), winevents.parse_event_xml(CRASH)]
    records.append(dict(records[0], exception_code="0x0"))
    assert [r["exception_code"] for r in winevents.fatal(records, "WINWORD.EXE")] == ["0xc0000005"]


# --------------------------------------------------------------------- CLI


@pytest.mark.windows("the Windows event log, WER reports and LocalDumps")
def test_the_null_app_lists_every_app(monkeypatch):
    from desktoppilot.cli import app

    seen = {}
    monkeypatch.setattr(
        winevents,
        "query",
        lambda image, **k: seen.update(image=image, **k) or {"records": []},
    )
    result = CliRunner().invoke(app, ["desktop", "events", "list", "--since", "5m"])
    assert result.exit_code == 0, result.output
    assert seen["image"] is None and seen["since"] == "5m"

    CliRunner().invoke(app, ["notepad", "events", "list"])
    assert seen["image"] == "notepad.exe" and seen["since"] == winevents.DEFAULT_SINCE


def test_since_and_since_mark_do_not_mix():
    from desktoppilot.cli import app

    result = CliRunner().invoke(app, [PLAIN_APP, "events", "list", "--since", "1m", "--since-mark"])
    assert result.exit_code == 1
    assert "mutually exclusive" in json.loads(result.output)["message"]


# ------------------------------------------------------- pytest plugin


INNER = """
import pytest

def test_body_passes():
    pass
"""

INNER_FAILS = """
def test_body_fails():
    assert False, "the body failed"
"""

# The image of the ad-hoc --app below: python.exe on Windows, python on Linux.
IMAGE = Path(sys.executable).name
FAKE_CRASH = winevents.parse_event_xml(CRASH.replace("WINWORD.EXE", IMAGE))


@pytest.fixture
def plugin(pytester, monkeypatch):
    """The plugin with its app sources faked: `found` is what each query
    returns, in order (the last one repeats)."""
    state = {"found": [[]], "queries": 0, "pids": [], "alive": True}

    def query(image, **k):
        state["queries"] += 1
        found = state["found"].pop(0) if len(state["found"]) > 1 else state["found"][0]
        return {"records": found, "unavailable": {}}

    monkeypatch.setattr(winevents, "query", query)
    monkeypatch.setattr(winevents.Mark, "now", staticmethod(lambda image: winevents.Mark(image, 1)))
    monkeypatch.setattr(pytest_plugin.apps, "pids", lambda profile: state["pids"])
    # Only the pids the app was seen with run; a crash record's pid is gone.
    monkeypatch.setattr(
        pytest_plugin, "_alive", lambda pid: state["alive"] and pid in state["pids"]
    )
    monkeypatch.setattr(pytest_plugin, "CRASH_RECORD_WAIT", 2.0)
    monkeypatch.setattr(pytest_plugin.dialogs, "close_all", lambda pids, **k: [])
    monkeypatch.setattr(
        pytest_plugin.artifacts,
        "capture",
        lambda name, ctx, directory: {"dir": "d", "saved": {}, "failed": {}},
    )

    # An executable path is an ad-hoc profile, image IMAGE.
    base = ("--app=" + sys.executable, "-p", "no:cacheprovider")

    def run(source: str = INNER, *args: str):
        pytester.makepyfile(source)
        reports = pytester.inline_run(*base, *args).getreports("pytest_runtest_logreport")
        return {r.when: r for r in reports}

    def output(source: str = INNER, *args: str):
        pytester.makepyfile(source)
        return pytester.runpytest(*base, *args)

    state["run"] = run
    state["output"] = output
    return state


def sections(report) -> list[str]:
    """Named "(teardown)" at teardown, so pytest -rA prints it for a passed test."""
    name = "desktoppilot windows events" + (" (teardown)" if report.when == "teardown" else "")
    return [text for n, text in report.sections if n == name]


def test_a_crash_fails_a_test_whose_body_passed(plugin):
    plugin["found"] = [[FAKE_CRASH]]
    reports = plugin["run"]()
    assert reports["call"].passed
    assert reports["teardown"].failed
    assert "crashed or hung" in str(reports["teardown"].longrepr)
    assert "0xc0000005" in str(reports["teardown"].longrepr)


def test_allow_crash_only_reports_it(plugin):
    plugin["found"] = [[FAKE_CRASH]]
    reports = plugin["run"](INNER, "--pilot-allow-crash")
    assert reports["teardown"].passed
    assert f"crash {IMAGE}" in sections(reports["teardown"])[0]


def test_allow_crash_prints_the_records_for_a_passed_test_under_ra(plugin):
    plugin["found"] = [[FAKE_CRASH]]
    result = plugin["output"](INNER, "--pilot-allow-crash", "-rA")
    result.assert_outcomes(passed=1)
    result.stdout.fnmatch_lines(["*desktoppilot windows events (teardown)*", f"*crash {IMAGE}*"])


def test_a_crash_record_of_a_still_running_process_is_not_fatal(plugin):
    plugin.update(pids=[FAKE_CRASH["pid"]], found=[[FAKE_CRASH]])
    reports = plugin["run"]()
    assert reports["teardown"].passed and not sections(reports["teardown"])


def test_no_record_passes_with_one_cheap_look(plugin):
    plugin["pids"] = [111]
    reports = plugin["run"]()
    assert reports["teardown"].passed and not sections(reports["teardown"])
    assert plugin["queries"] == 1


def test_a_gone_app_waits_for_the_late_record(plugin):
    plugin.update(pids=[111], alive=False, found=[[], [], [FAKE_CRASH]])
    reports = plugin["run"]()
    assert reports["teardown"].failed
    assert plugin["queries"] >= 3


def test_a_failed_body_is_not_failed_twice_but_shows_the_records(plugin):
    plugin["found"] = [[FAKE_CRASH]]
    reports = plugin["run"](INNER_FAILS)
    assert reports["call"].failed and "the body failed" in str(reports["call"].longrepr)
    assert sections(reports["call"])
    assert reports["teardown"].passed
    assert sections(reports["teardown"])


def test_a_broken_event_source_never_fails_the_test(plugin, monkeypatch):
    def broken(image, **k):
        raise RuntimeError("log gone")

    monkeypatch.setattr(winevents, "query", broken)
    reports = plugin["run"]()
    assert reports["call"].passed and reports["teardown"].passed


@pytest.mark.windows("the Windows event log, WER reports and LocalDumps")
def test_event_records_with_limit_zero_keeps_all(monkeypatch):
    batches = [[CRASH, CRASH, CRASH], []]
    monkeypatch.setattr(winevents.win32evtlog, "EvtQuery", lambda *a: "handle")
    monkeypatch.setattr(winevents.win32evtlog, "EvtNext", lambda handle, n: batches.pop(0))
    monkeypatch.setattr(winevents.win32evtlog, "EvtRender", lambda event, flags: event)
    monkeypatch.setattr(winevents, "_format_message", lambda event, provider, cache: None)

    found = winevents.event_records("WINWORD.EXE", kinds=["crash"], limit=0)

    assert len(found) == 3
