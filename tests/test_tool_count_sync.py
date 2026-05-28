"""Guard: the registered tool set must stay in sync across the three places that
declare it. Adding/removing a tool in server.py without updating BOTH the
`expected` set + `len(names) == N` count in tests/test_server_smoke.py AND
EXPECTED_TOOLS in scripts/smoke_e2e.py silently breaks the smoke tests. This test
fails loudly and names the exact divergence instead.

Source of truth: the tools actually registered on the FastMCP server.
"""

import ast
import importlib.util
from pathlib import Path

import nrha_mcp.server as srv

ROOT = Path(__file__).resolve().parent.parent
SMOKE_E2E = ROOT / "scripts" / "smoke_e2e.py"
SERVER_SMOKE = ROOT / "tests" / "test_server_smoke.py"


def _registered_tool_names() -> set[str]:
    # FastMCP stores registered tools in _tool_manager._tools (keyed by name).
    return set(srv.mcp._tool_manager._tools.keys())


def _smoke_e2e_expected_tools() -> set[str]:
    spec = importlib.util.spec_from_file_location("_smoke_e2e_for_sync", SMOKE_E2E)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)  # defines EXPECTED_TOOLS; run_smoke() stays guarded
    return set(mod.EXPECTED_TOOLS)


def _server_smoke_expected_and_count() -> tuple[set[str], int]:
    """Statically extract the `expected = {...}` set literal and the integer N from
    `assert len(names) == N` in test_server_smoke.py (without re-running the test)."""
    tree = ast.parse(SERVER_SMOKE.read_text())
    expected: set[str] | None = None
    count: int | None = None
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Assign)
            and any(isinstance(t, ast.Name) and t.id == "expected" for t in node.targets)
            and isinstance(node.value, ast.Set)
        ):
            expected = {e.value for e in node.value.elts if isinstance(e, ast.Constant)}
        if (
            isinstance(node, ast.Compare)
            and isinstance(node.left, ast.Call)
            and isinstance(node.left.func, ast.Name)
            and node.left.func.id == "len"
            and len(node.comparators) == 1
            and isinstance(node.comparators[0], ast.Constant)
            and isinstance(node.comparators[0].value, int)
        ):
            count = node.comparators[0].value
    assert expected is not None, "Could not find `expected = {...}` in test_server_smoke.py"
    assert count is not None, "Could not find `assert len(names) == N` in test_server_smoke.py"
    return expected, count


def test_smoke_e2e_expected_tools_match_registered():
    registered = _registered_tool_names()
    smoke = _smoke_e2e_expected_tools()
    assert smoke == registered, (
        "scripts/smoke_e2e.py EXPECTED_TOOLS is out of sync with server.py.\n"
        f"  only in smoke_e2e: {sorted(smoke - registered)}\n"
        f"  missing from smoke_e2e: {sorted(registered - smoke)}"
    )


def test_server_smoke_expected_set_matches_registered():
    registered = _registered_tool_names()
    expected, _ = _server_smoke_expected_and_count()
    assert expected == registered, (
        "tests/test_server_smoke.py `expected` set is out of sync with server.py.\n"
        f"  only in test: {sorted(expected - registered)}\n"
        f"  missing from test: {sorted(registered - expected)}"
    )


def test_server_smoke_count_matches_registered():
    registered = _registered_tool_names()
    _, count = _server_smoke_expected_and_count()
    assert count == len(registered), (
        f"tests/test_server_smoke.py asserts len(names) == {count} but "
        f"{len(registered)} tools are registered in server.py."
    )
