"""scripts/mcp_check.py: what a fingerprint sees, and how check reads a doc."""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

_PATH = Path(__file__).resolve().parents[2] / "scripts" / "mcp_check.py"
_spec = importlib.util.spec_from_file_location("mcp_check", _PATH)
mcp_check = importlib.util.module_from_spec(_spec)
sys.modules["mcp_check"] = mcp_check
_spec.loader.exec_module(mcp_check)

TOOLS = [
    {
        "name": "open",
        "description": "Open a file",
        "inputSchema": {
            "type": "object",
            "properties": {"path": {"type": "string"}, "readonly": {"type": "boolean"}},
            "required": ["path"],
        },
    },
    {"name": "list", "inputSchema": {"type": "object", "properties": {}}},
]


def test_order_and_descriptions_do_not_change_the_fingerprint():
    reworded = [dict(TOOLS[1]), dict(TOOLS[0], description="Opens a document")]
    assert mcp_check.fingerprint(reworded) == mcp_check.fingerprint(TOOLS)


def test_a_new_parameter_changes_the_fingerprint():
    schema = json.loads(json.dumps(TOOLS[0]["inputSchema"]))
    schema["properties"]["encoding"] = {"type": "string"}
    changed = [dict(TOOLS[0], inputSchema=schema), TOOLS[1]]
    assert mcp_check.fingerprint(changed) != mcp_check.fingerprint(TOOLS)


def test_a_newly_required_parameter_changes_the_fingerprint():
    schema = json.loads(json.dumps(TOOLS[0]["inputSchema"]))
    schema["required"] = ["path", "readonly"]
    changed = [dict(TOOLS[0], inputSchema=schema), TOOLS[1]]
    assert mcp_check.fingerprint(changed) != mcp_check.fingerprint(TOOLS)


def test_an_agent_dump_with_input_schema_matches_tools_list():
    dump = [{"name": t["name"], "input_schema": t["inputSchema"]} for t in TOOLS]
    assert mcp_check.fingerprint(dump) == mcp_check.fingerprint(TOOLS)


def test_union_types_are_order_free():
    a = [
        {
            "name": "t",
            "inputSchema": {"properties": {"x": {"anyOf": [{"type": "string"}, {"type": "null"}]}}},
        }
    ]
    b = [{"name": "t", "inputSchema": {"properties": {"x": {"type": ["null", "string"]}}}}]
    assert mcp_check.shape(a) == mcp_check.shape(b)


def test_load_dump_takes_a_tools_list_result_or_a_list(tmp_path):
    wrapped = tmp_path / "wrapped.json"
    wrapped.write_text(json.dumps({"tools": TOOLS}), encoding="utf-8")
    bare = tmp_path / "bare.json"
    bare.write_text(json.dumps(TOOLS), encoding="utf-8")
    assert mcp_check.load_dump(wrapped) == mcp_check.load_dump(bare) == TOOLS


def test_load_dump_refuses_a_tool_without_a_name(tmp_path):
    bad = tmp_path / "bad.json"
    bad.write_text(json.dumps([{"inputSchema": {}}]), encoding="utf-8")
    with pytest.raises(mcp_check.CheckError):
        mcp_check.load_dump(bad)


def _doc(tmp_path, fingerprint="", source="connector"):
    doc = tmp_path / "word.md"
    doc.write_text(
        "# word\n\n"
        f"- server: Example\n- source: {source}\n- fingerprint: `{fingerprint}`\n"
        "- tested: 2026-10-17\n\n"
        "| task | path |\n| --- | --- |\n| - fingerprint: in a cell | x |\n",
        encoding="utf-8",
    )
    return doc


def test_read_doc_stops_at_the_table(tmp_path):
    fields = mcp_check.read_doc(_doc(tmp_path, "sha256:abc"))
    assert fields["server"] == "Example"
    assert fields["fingerprint"] == "sha256:abc"


def test_check_reports_same_changed_untested_and_needs_dump(tmp_path):
    dump = tmp_path / "dump.json"
    dump.write_text(json.dumps(TOOLS), encoding="utf-8")
    good = mcp_check.fingerprint(TOOLS)

    same = mcp_check.check_doc(_doc(tmp_path, good), {"word": dump}, None)
    assert same["status"] == "same"
    changed = mcp_check.check_doc(_doc(tmp_path, "sha256:0000"), {"word": dump}, None)
    assert changed["status"] == "changed"
    untested = mcp_check.check_doc(_doc(tmp_path, ""), {"word": dump}, None)
    assert untested["status"] == "untested"
    assert untested["fingerprint"] == good
    needs = mcp_check.check_doc(_doc(tmp_path, good), {}, None)
    assert needs["status"] == "needs_dump"


def test_find_server_reads_user_then_project_scope(tmp_path):
    config = tmp_path / "claude.json"
    config.write_text(
        json.dumps(
            {
                "mcpServers": {"a": {"command": "a"}},
                "projects": {"/x": {"mcpServers": {"b": {"url": "http://b"}}}},
            }
        ),
        encoding="utf-8",
    )
    assert mcp_check.find_server("a", config) == {"command": "a"}
    assert mcp_check.find_server("b", config) == {"url": "http://b"}
    with pytest.raises(mcp_check.CheckError):
        mcp_check.find_server("c", config)


def test_stdio_server_end_to_end(tmp_path):
    """A fake server in Python: initialize, then two pages of tools/list."""
    server = tmp_path / "server.py"
    server.write_text(
        "import json, sys\n"
        f"TOOLS = {TOOLS!r}\n"
        "for line in sys.stdin:\n"
        "    msg = json.loads(line)\n"
        "    if 'id' not in msg:\n"
        "        continue\n"
        "    print('log line on stdout', flush=True)\n"
        "    if msg['method'] == 'initialize':\n"
        "        result = {'protocolVersion': '2025-06-18', 'capabilities': {}}\n"
        "    elif not msg['params'].get('cursor'):\n"
        "        result = {'tools': TOOLS[:1], 'nextCursor': 'p2'}\n"
        "    else:\n"
        "        result = {'tools': TOOLS[1:]}\n"
        "    print(json.dumps({'jsonrpc': '2.0', 'id': msg['id'], 'result': result}), flush=True)\n",
        encoding="utf-8",
    )
    tools = mcp_check.list_tools({"command": sys.executable, "args": [str(server)]})
    assert mcp_check.fingerprint(tools) == mcp_check.fingerprint(TOOLS)
