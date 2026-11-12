#!/usr/bin/env python3
"""AGSH MCP server — official Model Context Protocol entry (backlog #5).

Zero-dependency stdio MCP server (JSON-RPC 2.0) exposing ONE tool:

    agsh_execute(script: str, flags: list[str] = [], plan_first: bool = true)

plan_first=true (default) returns the dry-run plan with an "approval_required"
flag instead of executing, so the calling agent/host can gate dangerous verbs.
Agents should call once with plan_first=true, then again with false.

Run:  python3 mcp/agsh_mcp.py [--agsh /path/to/agsh.py] [--cwd WORKDIR]
Wire into any MCP client (Claude Desktop, Hermes, ...):
  { "command": "python3", "args": ["/path/to/AGSH-/mcp/agsh_mcp.py"], "env": {} }
"""
import json
import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
DEFAULT_AGSH = os.path.join(HERE, "..", "agsh.py")
DEFAULT_CWD = os.path.dirname(DEFAULT_AGSH)

PROTO_VERSION = "2024-11-05"

TOOL_DEF = {
    "name": "agsh_execute",
    "description": (
        "Execute an AGSH pipeline: a token-cheap pipe DSL for agent data tasks "
        "(fetch/post/read/write/filter/pick/map/get/let/take/checkpoint/undo/"
        "schema/lease and 20+ more). One call replaces many round-trips. "
        "Call with plan_first=true first to get the dry-run plan; if "
        "approval_required is true, get human approval, then re-call with "
        "plan_first=false and the needed --approve/--allow-* flags."
    ),
    "inputSchema": {
        "type": "object",
        "properties": {
            "script": {"type": "string",
                       "description": "AGSH pipeline, one stage per line. "
                                      "Example: 'fetch https://api.example.com/users | filter active=true | pick name,email | write users.json'"},
            "flags": {"type": "array", "items": {"type": "string"},
                      "description": "Host-level flags, e.g. ['--approve', '--allow-run=sort', '--audit', '/tmp/agsh.jsonl']. Never self-approve dangerous verbs without a human."},
            "plan_first": {"type": "boolean", "default": True,
                           "description": "true = dry-run plan only (safe, default). false = actually execute."},
        },
        "required": ["script"],
    },
}


def run_agsh(script, flags, plan_first):
    args = [sys.executable, os.path.abspath(DEFAULT_AGSH)] + list(flags or [])
    if plan_first:
        args.append("--plan")
    p = subprocess.run(args, input=script, capture_output=True, text=True,
                       cwd=os.path.abspath(DEFAULT_CWD), timeout=120)
    try:
        return json.loads(p.stdout)
    except Exception:
        return {"internal": f"non-JSON output (rc={p.returncode})", "stdout": p.stdout[-2000:], "stderr": p.stderr[-1000:]}


def handle(req, state):
    m = req.get("method", "")
    if m == "initialize":
        return {"result": {"protocolVersion": PROTO_VERSION,
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "agsh", "version": "0.7"}}}
    if m == "tools/list":
        return {"result": {"tools": [TOOL_DEF]}}
    if m == "tools/call":
        a = (req.get("params") or {}).get("arguments") or {}
        try:
            out = run_agsh(a.get("script", ""), a.get("flags"), a.get("plan_first", True))
            needs = bool(out.get("needs_human")) or bool(out.get("approval_required"))
            return {"result": {"content": [{"type": "text", "text": json.dumps(out, ensure_ascii=False)}],
                    "isError": "error" in out, "_meta": {"approval_required": needs}}}
        except Exception as e:
            return {"result": {"content": [{"type": "text", "text": json.dumps({"internal": str(e)})}], "isError": True}}
    if m.startswith("notifications/"):
        return None
    return {"error": {"code": -32601, "message": f"method not found: {m}"}}


def main():
    agsh, cwd = DEFAULT_AGSH, DEFAULT_CWD
    argv = sys.argv[1:]
    if "--agsh" in argv:
        agsh = argv[argv.index("--agsh") + 1]
    if "--cwd" in argv:
        cwd = argv[argv.index("--cwd") + 1]
    state = {}
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            req = json.loads(line)
        except Exception:
            continue
        resp = handle(req, state)
        if resp is None:
            continue
        if "id" in req:
            resp = {"jsonrpc": "2.0", "id": req["id"], **resp}
        else:
            resp = {"jsonrpc": "2.0", "id": None, **resp}
        sys.stdout.write(json.dumps(resp, ensure_ascii=False) + "\n")
        sys.stdout.flush()


if __name__ == "__main__":
    main()
