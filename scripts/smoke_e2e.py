#!/usr/bin/env python3
"""End-to-end stdio smoke test for node-red-homeassistant-mcp.

Requires a running Node-RED instance (e.g. `docker compose up -d nodered`).
Set NR_URL to point at it (default: http://localhost:1880).

Usage:
    NR_URL=http://localhost:1880 .venv/bin/python scripts/smoke_e2e.py

Exit code 0 = all assertions passed.
"""

import asyncio
import json
import os
import sys
import uuid
from pathlib import Path

import httpx
from mcp import ClientSession
from mcp.client.stdio import stdio_client, StdioServerParameters

NR_URL = os.environ.get("NR_URL", "http://localhost:1880")
BACKUP_DIR = os.environ.get("BACKUP_DIR", "./backups")

EXPECTED_TOOLS = {
    "nr_get_flow_summary",
    "nr_get_flow",
    "nr_search_nodes",
    "nr_get_function_code",
    "nr_get_node_config",
    "nr_get_flow_context",
    "nr_get_global_context",
    "nr_capture_debug",
    "nr_get_runtime_events",
    "nr_safe_deploy",
    "nr_safe_deploy_many",
    "nr_add_wire",
    "nr_create_nodes",
    "nr_delete_nodes",
    "nr_backup_flow",
    "nr_list_backups",
    "nr_restore_backup",
    "nr_get_installed_modules",
    "nr_install_module",
    "nr_inject",
    "ha_list_ha_nodes",
    "ha_get_server_config",
    "ha_search_entities",
    "ha_get_state",
}


def _parse(result) -> dict:
    """Extract and parse JSON from a tool CallToolResult."""
    content = result.content
    if content and hasattr(content[0], "text"):
        return json.loads(content[0].text)
    raise ValueError(f"Unexpected result structure: {result}")


async def seed_flow_if_empty(nr_url: str) -> tuple[str, str]:
    """If Node-RED has no tabs, POST a tab + inject node via Admin API.

    Returns (tab_id, inject_node_id).
    """
    async with httpx.AsyncClient(timeout=10.0) as http:
        r = await http.get(f"{nr_url}/flows", headers={"Node-RED-API-Version": "v2"})
        r.raise_for_status()
        data = r.json()
        existing_flows = data.get("flows", [])
        rev = data.get("rev", "")
        tabs = [f for f in existing_flows if f.get("type") == "tab"]

        if tabs:
            tab = tabs[0]
            tab_id = tab["id"]
            nodes_in_tab = [f for f in existing_flows if f.get("z") == tab_id and f.get("type") == "inject"]
            if nodes_in_tab:
                print(f"  [seed] Existing tab '{tab.get('label', tab_id)}' with inject node {nodes_in_tab[0]['id']} found.")
                return tab_id, nodes_in_tab[0]["id"]

        # Need to seed
        print("  [seed] No suitable flow found; creating tab + inject node via Admin API.")
        tab_id = str(uuid.uuid4())[:8]
        inject_id = str(uuid.uuid4())[:8]
        new_flows = existing_flows + [
            {"id": tab_id, "type": "tab", "label": "smoke-test"},
            {"id": inject_id, "type": "inject", "z": tab_id, "name": "smoke-inject",
             "repeat": "", "crontab": "", "once": False, "topic": "", "payload": "",
             "payloadType": "date", "wires": [[]]},
        ]
        post_r = await http.post(
            f"{nr_url}/flows",
            json={"rev": rev, "flows": new_flows},
            headers={
                "Node-RED-API-Version": "v2",
                "Node-RED-Deployment-Type": "full",
                "Content-Type": "application/json",
            },
        )
        post_r.raise_for_status()
        print(f"  [seed] Created tab {tab_id}, inject node {inject_id}.")
        return tab_id, inject_id


async def run_smoke():
    print(f"\n=== smoke_e2e.py — NR_URL={NR_URL} ===\n")

    # Seed a flow if Node-RED is empty
    print("Step 0: Seed flow if needed")
    tab_id, inject_id = await seed_flow_if_empty(NR_URL)

    server_params = StdioServerParameters(
        command=sys.executable,
        args=["-m", "nrha_mcp.server"],
        env={
            **os.environ,
            "NR_URL": NR_URL,
            "DEPLOY_MODE": "propose",
            "BACKUP_DIR": BACKUP_DIR,
        },
    )

    async with stdio_client(server_params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()

            # Step 1: List tools — assert all expected present
            print("\nStep 1: list_tools()")
            tools_result = await session.list_tools()
            tool_names = {t.name for t in tools_result.tools}
            missing = EXPECTED_TOOLS - tool_names
            assert not missing, f"Missing tools: {missing}"
            print(f"  OK: {len(tool_names)} tools registered, all {len(EXPECTED_TOOLS)} expected present.")

            # Step 2: nr_get_flow_summary
            print("\nStep 2: nr_get_flow_summary()")
            summary_result = _parse(await session.call_tool("nr_get_flow_summary", {}))
            assert "total_tabs" in summary_result, f"Unexpected response: {summary_result}"
            print(f"  OK: total_tabs={summary_result['total_tabs']}, total_nodes={summary_result['total_nodes']}")

            # Step 3: nr_safe_deploy in propose mode (confirm=false)
            print(f"\nStep 3: nr_safe_deploy(node_id={inject_id}, fields={{name:'renamed'}}, confirm=false)")
            propose_result = _parse(await session.call_tool("nr_safe_deploy", {
                "node_id": inject_id,
                "fields": {"name": "renamed"},
                "confirm": False,
            }))
            assert propose_result.get("deployed") is False, f"Expected deployed:false, got: {propose_result}"
            assert "preview" in propose_result, f"Missing preview in: {propose_result}"
            assert inject_id in propose_result["preview"].get("changed", {}), (
                f"Expected {inject_id} in changed, got preview: {propose_result['preview']}"
            )
            print(f"  OK: deployed=false, preview shows changed node {inject_id}")
            print(f"       preview.changed keys: {list(propose_result['preview']['changed'].keys())}")

            # Step 4: nr_safe_deploy with confirm=true
            print(f"\nStep 4: nr_safe_deploy(node_id={inject_id}, fields={{name:'renamed'}}, confirm=true)")
            confirm_result = _parse(await session.call_tool("nr_safe_deploy", {
                "node_id": inject_id,
                "fields": {"name": "renamed"},
                "confirm": True,
            }))
            assert confirm_result.get("deployed") is True, f"Expected deployed:true, got: {confirm_result}"
            backup_path = confirm_result.get("backup", "")
            assert backup_path, f"No backup path in result: {confirm_result}"
            assert Path(backup_path).exists(), f"Backup file not on disk: {backup_path}"
            print(f"  OK: deployed=true")
            print(f"       backup: {backup_path}")
            print(f"       rev: {confirm_result.get('rev', '?')}")

    print("\n=== ALL ASSERTIONS PASSED ===\n")


if __name__ == "__main__":
    asyncio.run(run_smoke())
