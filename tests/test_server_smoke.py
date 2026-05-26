# tests/test_server_smoke.py
import nrha_mcp.server as srv


def test_tools_registered():
    # FastMCP 1.27.1 stores registered tools in _tool_manager._tools (dict keyed by name).
    names = set(srv.mcp._tool_manager._tools.keys())
    expected = {
        "nr_get_flow_summary", "nr_get_flow", "nr_search_nodes", "nr_get_function_code",
        "nr_get_node_config", "nr_get_flow_context", "nr_get_global_context",
        "nr_get_debug_output", "nr_safe_deploy", "nr_safe_deploy_many", "nr_add_wire",
        "nr_create_nodes", "nr_delete_nodes", "nr_get_installed_modules", "nr_install_module",
        "nr_inject", "ha_list_ha_nodes", "ha_get_server_config",
        "nr_backup_flow", "nr_list_backups", "nr_restore_backup",
    }
    assert expected.issubset(names)
    assert len(names) == 21
