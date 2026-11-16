"""Has an app's MCP server changed since its coverage was tested?

    python scripts/mcp_check.py fingerprint --server NAME [--config FILE]
    python scripts/mcp_check.py fingerprint --tools-json FILE
    python scripts/mcp_check.py check [DOC ...] [--tools-json APP=FILE ...]

A fingerprint is a hash of the server's tool names and parameters (names,
types, required), not its descriptions: a reworded description does not
change what the server can do, a new tool or parameter does. `check` reads
the `- fingerprint:` line of each `docs/mcp/<app>.md` and compares. A server
in a Claude Code config (`source: config`) is asked itself, over stdio or
HTTP; a claude.ai connector or a built-in (`source: connector`, `builtin`)
holds its auth inside Claude, so an agent dumps its tool list to a file and
passes it with `--tools-json`.

Standard library only, and no desktoppilot import: it runs on any VM,
including a scheduled job on Linux. It never calls a model. Prints JSON;
a failure prints {"error": KIND, "message": ...} and exits 1, and `check`
exits 1 when a server changed.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import queue
import re
import shutil
import subprocess
import sys
import threading
import urllib.request
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
DOCS = REPO / "docs" / "mcp"
PROTOCOL = "2025-06-18"
TIMEOUT = 30.0


class CheckError(Exception):
    def __init__(self, kind: str, message: str) -> None:
        super().__init__(message)
        self.kind = kind


# ------------------------------------------------------------- fingerprint


def _schema(tool: dict) -> dict:
    """MCP says inputSchema; an agent's dump may say input_schema or parameters."""
    for key in ("inputSchema", "input_schema", "parameters"):
        if isinstance(tool.get(key), dict):
            return tool[key]
    return {}


def _type(prop: dict) -> str:
    kind = prop.get("type")
    if isinstance(kind, list):
        return "|".join(sorted(str(k) for k in kind))
    if kind:
        return str(kind)
    for key in ("anyOf", "oneOf"):
        if isinstance(prop.get(key), list):
            return "|".join(sorted(_type(p) for p in prop[key] if isinstance(p, dict)))
    return "any"


def shape(tools: list[dict]) -> list[dict]:
    """What a fingerprint covers, sorted so that order never matters."""
    out = []
    for tool in tools:
        schema = _schema(tool)
        props = schema.get("properties") or {}
        out.append(
            {
                "name": tool["name"],
                "params": {k: _type(v) for k, v in sorted(props.items()) if isinstance(v, dict)},
                "required": sorted(schema.get("required") or []),
            }
        )
    return sorted(out, key=lambda t: t["name"])


def fingerprint(tools: list[dict]) -> str:
    canon = json.dumps(shape(tools), sort_keys=True, separators=(",", ":"))
    return "sha256:" + hashlib.sha256(canon.encode("utf-8")).hexdigest()[:16]


def load_dump(path: Path) -> list[dict]:
    """A tools/list result ({"tools": [...]}) or a bare list of tools."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise CheckError("BadDump", f"{path}: {exc}") from exc
    tools = data.get("tools") if isinstance(data, dict) else data
    if not isinstance(tools, list) or not all(isinstance(t, dict) and "name" in t for t in tools):
        raise CheckError("BadDump", f"{path}: expected a list of tools, each with a name")
    return tools


# ------------------------------------------------------------ server config


def _config_files(explicit: Path | None) -> list[Path]:
    if explicit:
        return [explicit]
    return [Path.cwd() / ".mcp.json", Path.home() / ".claude.json"]


def find_server(name: str, explicit: Path | None = None) -> dict:
    """The server's entry from .mcp.json or ~/.claude.json: user scope first,
    then any project scope, the way `claude mcp list` merges them."""
    for path in _config_files(explicit):
        if not path.exists():
            continue
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except ValueError as exc:
            raise CheckError("BadConfig", f"{path}: {exc}") from exc
        scopes = [data.get("mcpServers") or {}]
        scopes += [p.get("mcpServers") or {} for p in (data.get("projects") or {}).values()]
        for scope in scopes:
            if name in scope:
                return scope[name]
    raise CheckError(
        "NoServer", f"no MCP server {name!r} in {[str(p) for p in _config_files(explicit)]}"
    )


def _expand(value: str) -> str:
    """Claude Code configs write ${VAR} and ${VAR:-default}."""

    def sub(match: re.Match) -> str:
        var, _, default = match.group(1).partition(":-")
        return os.environ.get(var, default)

    return re.sub(r"\$\{([^}]+)\}", sub, value)


# ---------------------------------------------------------------- clients


def _init_params() -> dict:
    return {
        "protocolVersion": PROTOCOL,
        "capabilities": {},
        "clientInfo": {"name": "desktoppilot-mcp-check", "version": "1"},
    }


class Stdio:
    """Newline-delimited JSON-RPC over the server's stdin and stdout."""

    def __init__(self, entry: dict) -> None:
        command = shutil.which(_expand(entry["command"])) or _expand(entry["command"])
        env = os.environ | {k: _expand(str(v)) for k, v in (entry.get("env") or {}).items()}
        self.proc = subprocess.Popen(
            [command, *(_expand(str(a)) for a in entry.get("args") or [])],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            env=env,
            text=True,
            encoding="utf-8",
        )
        self.lines: queue.Queue[str | None] = queue.Queue()
        threading.Thread(target=self._read, daemon=True).start()

    def _read(self) -> None:
        for line in self.proc.stdout:
            self.lines.put(line)
        self.lines.put(None)

    def send(self, message: dict) -> None:
        self.proc.stdin.write(json.dumps(message) + "\n")
        self.proc.stdin.flush()

    def request(self, rid: int, method: str, params: dict) -> dict:
        self.send({"jsonrpc": "2.0", "id": rid, "method": method, "params": params})
        while True:
            try:
                line = self.lines.get(timeout=TIMEOUT)
            except queue.Empty as exc:
                raise CheckError("Timeout", f"no answer to {method} in {TIMEOUT:.0f}s") from exc
            if line is None:
                raise CheckError("ServerExited", f"server exited before answering {method}")
            try:
                message = json.loads(line)
            except ValueError:
                continue  # a server that logs to stdout
            if message.get("id") == rid:
                return message

    def notify(self, method: str) -> None:
        self.send({"jsonrpc": "2.0", "method": method})

    def close(self) -> None:
        self.proc.kill()


class Http:
    """Streamable HTTP: one POST per message, the answer as JSON or as SSE."""

    def __init__(self, entry: dict) -> None:
        self.url = _expand(entry["url"])
        self.headers = {k: _expand(str(v)) for k, v in (entry.get("headers") or {}).items()}
        self.session: str | None = None

    def _post(self, message: dict) -> tuple[str, str]:
        headers = self.headers | {
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
            "MCP-Protocol-Version": PROTOCOL,
        }
        if self.session:
            headers["Mcp-Session-Id"] = self.session
        req = urllib.request.Request(self.url, json.dumps(message).encode(), headers, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
                self.session = resp.headers.get("Mcp-Session-Id") or self.session
                return resp.headers.get("Content-Type", ""), resp.read().decode("utf-8")
        except OSError as exc:
            raise CheckError("Unreachable", f"{self.url}: {exc}") from exc

    def request(self, rid: int, method: str, params: dict) -> dict:
        kind, body = self._post({"jsonrpc": "2.0", "id": rid, "method": method, "params": params})
        if "text/event-stream" not in kind:
            return json.loads(body)
        for line in body.splitlines():
            if line.startswith("data:"):
                message = json.loads(line[5:])
                if message.get("id") == rid:
                    return message
        raise CheckError("NoAnswer", f"the event stream held no answer to {method}")

    def notify(self, method: str) -> None:
        self._post({"jsonrpc": "2.0", "method": method})

    def close(self) -> None:
        pass


def list_tools(entry: dict) -> list[dict]:
    kind = entry.get("type") or ("stdio" if "command" in entry else "http")
    if kind == "stdio":
        client = Stdio(entry)
    elif kind == "http":
        client = Http(entry)
    else:
        raise CheckError("Unsupported", f"transport {kind!r}: dump the tools and pass --tools-json")
    try:
        answer = client.request(1, "initialize", _init_params())
        if "error" in answer:
            raise CheckError("InitFailed", json.dumps(answer["error"]))
        client.notify("notifications/initialized")
        tools: list[dict] = []
        cursor = None
        rid = 2
        while True:
            answer = client.request(rid, "tools/list", {"cursor": cursor} if cursor else {})
            if "error" in answer:
                raise CheckError("ListFailed", json.dumps(answer["error"]))
            tools += answer["result"].get("tools", [])
            cursor = answer["result"].get("nextCursor")
            if not cursor:
                return tools
            rid += 1
    finally:
        client.close()


# ------------------------------------------------------------ coverage docs


def read_doc(path: Path) -> dict:
    """The `- key: value` lines of a coverage doc's header list."""
    fields: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        match = re.match(r"^- ([a-z ]+): (.*)$", line)
        if match:
            fields.setdefault(match.group(1), match.group(2).strip().strip("`"))
        elif line.startswith("|"):
            break  # the table: the header is done
    return fields


def check_doc(path: Path, dumps: dict[str, Path], config: Path | None) -> dict:
    app = path.stem
    fields = read_doc(path)
    out = {"app": app, "doc": str(path), "server": fields.get("server")}
    want = fields.get("fingerprint", "")
    try:
        if app in dumps:
            tools = load_dump(dumps[app])
        elif fields.get("source") == "config":
            tools = list_tools(find_server(fields["server"], config))
        else:
            return out | {"status": "needs_dump", "source": fields.get("source")}
        got = fingerprint(tools)
    except CheckError as exc:
        return out | {"status": "error", "error": exc.kind, "message": str(exc)}
    if not want.startswith("sha256:"):
        return out | {"status": "untested", "fingerprint": got, "tools": len(tools)}
    status = "same" if got == want else "changed"
    return out | {"status": status, "fingerprint": got, "was": want, "tools": len(tools)}


# --------------------------------------------------------------------- CLI


def _parse_dumps(values: list[str]) -> dict[str, Path]:
    dumps = {}
    for value in values:
        app, sep, file = value.partition("=")
        if not sep:
            raise CheckError("BadArgument", f"--tools-json takes APP=FILE, got {value!r}")
        dumps[app] = Path(file)
    return dumps


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)
    fp = sub.add_parser("fingerprint", help="print a server's fingerprint and tool names")
    source = fp.add_mutually_exclusive_group(required=True)
    source.add_argument("--server", help="a server name in .mcp.json or ~/.claude.json")
    source.add_argument("--tools-json", type=Path, help="a tools/list dump")
    fp.add_argument("--config", type=Path, help="read the server from this file instead")
    ck = sub.add_parser("check", help="compare each coverage doc with its server")
    ck.add_argument("docs", nargs="*", type=Path, help="default: docs/mcp/*.md")
    ck.add_argument("--tools-json", action="append", default=[], metavar="APP=FILE")
    ck.add_argument("--config", type=Path)
    args = parser.parse_args(argv)

    try:
        if args.command == "fingerprint":
            if args.tools_json:
                tools = load_dump(args.tools_json)
            else:
                tools = list_tools(find_server(args.server, args.config))
            print(
                json.dumps(
                    {
                        "server": args.server or str(args.tools_json),
                        "fingerprint": fingerprint(tools),
                        "tools": len(tools),
                        "names": [t["name"] for t in shape(tools)],
                    },
                    indent=2,
                )
            )
            return 0
        dumps = _parse_dumps(args.tools_json)
        docs = args.docs or sorted(p for p in DOCS.glob("*.md") if p.name != "README.md")
        results = [check_doc(p, dumps, args.config) for p in docs]
        print(json.dumps({"results": results}, indent=2))
        return 1 if any(r["status"] in ("changed", "error") for r in results) else 0
    except CheckError as exc:
        print(json.dumps({"error": exc.kind, "message": str(exc)}))
        return 1


if __name__ == "__main__":
    sys.exit(main())
