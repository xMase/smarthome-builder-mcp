"""JSON-RPC transport to the Device Builder's own MCP endpoint (/api/mcp) through HA ingress."""
import asyncio
import copy
import itertools
import json

import httpx

from .client import NRAuthError, NRError, NRNotFoundError
from .esphome import (CONNECT_TIMEOUT, REPLY_TIMEOUT, _ERROR_CLASSES, ESPHomeError, YamlBackupStore,
                      _diff, _open_ingress, check_configuration)

_ids = itertools.count(1)


async def rpc(settings, method: str, params: dict | None = None, *, ha_connect=None,
              transport: httpx.AsyncBaseTransport | None = None) -> dict:
    """POST one JSON-RPC request; return its `result`. A fresh ingress session per call (no cache)."""
    try:
        ingress_url, session_id = await asyncio.wait_for(
            _open_ingress(settings, ha_connect), CONNECT_TIMEOUT)
    except asyncio.TimeoutError:
        raise ESPHomeError(f"timed out after {CONNECT_TIMEOUT}s opening the HA ingress session") from None
    url = f"{settings.ha_url.rstrip('/')}/{ingress_url.strip('/')}/api/mcp"
    body = {"jsonrpc": "2.0", "id": next(_ids), "method": method}
    if params is not None:
        body["params"] = params
    try:
        async with httpx.AsyncClient(timeout=REPLY_TIMEOUT, transport=transport,
                                       cookies={"ingress_session": session_id}) as http:
            resp = await http.post(url, json=body)
    except httpx.HTTPError as e:
        raise ESPHomeError(
            f"cannot reach the Device Builder MCP endpoint: {type(e).__name__}: {e}") from None
    if resp.status_code in (401, 403):
        raise NRAuthError(f"HA ingress refused the Device Builder MCP endpoint (HTTP {resp.status_code}); "
                          "the HA token must belong to an admin user")
    if resp.status_code == 404:
        raise ESPHomeError("ESPHome Device Builder >= 1.15 is required for esphome_* tools "
                           "(no /api/mcp endpoint)")
    try:
        resp.raise_for_status()
        reply = resp.json()
    except (httpx.HTTPError, ValueError) as e:
        raise ESPHomeError(f"bad reply from the Device Builder MCP endpoint: {type(e).__name__}: {e}") from None
    if "error" in reply:
        message = (reply["error"] or {}).get("message", "unknown error")
        if message.startswith("Unknown tool"):
            message += (": the ESPHome tool list changed (add-on updated?); "
                        "reconnect this MCP server (/mcp) or start a new session.")
        raise ESPHomeError(f"Device Builder MCP error: {message}")
    return reply.get("result") or {}


PREFIX = "esphome_"
NO_CONFIRM = frozenset({
    "list_devices", "get_config", "validate_config", "get_job", "cancel_job", "compile",
    "search_components", "get_component", "get_config_components", "search_boards",
    "list_secret_names", "list_automations", "get_available_automations", "get_automation_docs"})
DEPLOY_MODE_CONFIRM = frozenset({
    "update_config", "add_component", "create_device", "upsert_automation", "delete_automation"})

_CONFIRM_PROP = {"type": "boolean", "default": False,
                 "description": "Set true to run this write; without it the call only previews."}


def gate_class(tool: str) -> str:
    """`none` forwards as is; `deploy_mode` needs confirm in propose mode; anything else always does."""
    if tool in NO_CONFIRM:
        return "none"
    return "deploy_mode" if tool in DEPLOY_MODE_CONFIRM else "always"


async def list_tools(settings, *, rpc=rpc) -> list[dict]:
    """Upstream tools renamed `esphome_<n>`; gated ones get a `confirm` property (upstream dicts untouched)."""
    out = []
    for t in (await rpc(settings, "tools/list")).get("tools", []):
        cls = gate_class(t["name"])
        schema = copy.deepcopy(t.get("inputSchema") or {"type": "object"})
        desc = t.get("description", "")
        if cls != "none":
            schema.setdefault("properties", {})["confirm"] = dict(_CONFIRM_PROP)
            desc += ("\nWrites: needs confirm=true (per DEPLOY_MODE)." if cls == "deploy_mode"
                     else "\nNeeds confirm=true always, also with DEPLOY_MODE=auto.")
        out.append({"name": PREFIX + t["name"], "description": desc, "inputSchema": schema})
    return out


async def _forward(settings, rpc, tool: str, arguments: dict, *, raw: bool = False):
    """tools/call upstream; return the parsed text (JSON if it parses, else the raw string).

    `raw=True` keeps the text as is: file reads (YAML may be valid JSON flow style) must not be parsed."""
    res = await rpc(settings, "tools/call", {"name": tool, "arguments": arguments})
    text = "".join(c.get("text", "") for c in res.get("content", []) if c.get("type") == "text")
    if res.get("isError"):
        raise _ERROR_CLASSES.get(text.split(":", 1)[0].strip(), ESPHomeError)(text)
    if raw:
        return text
    try:
        return json.loads(text)
    except ValueError:
        return text


async def _preview(settings, rpc, tool: str, args: dict) -> dict:
    out = {"preview": True, "tool": tool, "arguments": args, "requires_confirm": True}
    if tool == "update_config":
        current = await _forward(settings, rpc, "get_config", {"configuration": args["configuration"]},
                                  raw=True)
        out = {"preview": True, "tool": tool, "requires_confirm": True,
               "diff": _diff(current, args["content"], args["configuration"])}
    elif tool == "install":
        rows = await _forward(settings, rpc, "list_devices", {})
        row = next((r for r in rows if r.get("configuration") == args["configuration"]), None)
        out["device"] = row
        # Live rows keep state "unknown" for an offline device and set offline_seconds.
        offline = row and (row.get("state") == "offline" or row.get("offline_seconds") is not None)
        if offline and args.get("port") in (None, "OTA"):
            out["warning"] = "The device is offline; an OTA install will fail until it is reachable."
    return out


async def call(settings, tool: str, arguments: dict, *, rpc=rpc,
               backups: YamlBackupStore | None = None) -> dict:
    """Forward an upstream tool call (`tool` without prefix) through the confirm/backup gate."""
    args = dict(arguments)
    confirm = args.pop("confirm", False) is True  # upstream schemas are closed: never forward it
    if "configuration" in args:
        check_configuration(args["configuration"], settings)
    cls = gate_class(tool)
    if cls == "none":
        return {"result": await _forward(settings, rpc, tool, args)}
    if (cls == "always" or settings.deploy_mode == "propose") and not confirm:
        return await _preview(settings, rpc, tool, args)

    backups = backups or YamlBackupStore(settings.backup_dir)
    out: dict = {}
    name = "secrets.yaml" if tool == "set_secret" else None if tool == "install" else args.get("configuration")
    before = None
    if name:
        try:  # internal read: bypasses the secrets block, content never returned
            before = await _forward(settings, rpc, "get_config", {"configuration": name}, raw=True)
        except NRNotFoundError:
            if tool != "set_secret":  # set_secret creates a missing secrets.yaml
                raise
        if before is not None:
            out["backup_id"] = backups.snapshot(name, before)
    out = {"result": await _forward(settings, rpc, tool, args), **out}
    if before is not None and tool != "set_secret":
        try:  # the write already happened: a failed re-read must not turn it into an error
            after = await _forward(settings, rpc, "get_config", {"configuration": name}, raw=True)
            out["diff"] = _diff(before, after, name)
        except NRError as e:
            out["warning"] = f"the write succeeded but the after-diff could not be read: {e}"
    return out
