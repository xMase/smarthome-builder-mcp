# tests/test_server_smoke.py
import nrha_mcp.server as srv


def test_tools_registered():
    # MCPServer (mcp 2.x) stores registered tools in _tool_manager._tools (dict keyed by name).
    names = set(srv.mcp._tool_manager._tools.keys())
    expected = {
        "nr_get_flow_summary", "nr_get_flow", "nr_export_flow", "nr_get_subflow", "nr_search_nodes", "nr_get_function_code",
        "nr_get_node_config", "nr_get_flow_context", "nr_get_global_context",
        "nr_capture_debug", "nr_get_runtime_events", "nr_validate_flow", "nr_safe_deploy", "nr_safe_deploy_many", "nr_add_wire",
        "nr_create_nodes", "nr_delete_nodes", "nr_get_installed_modules", "nr_install_module",
        "nr_inject", "nr_diagnostics", "nr_list_ha_nodes", "nr_get_ha_servers",
        "nr_backup_flow", "nr_list_backups", "nr_restore_backup", "nr_diff",
        "nr_instantiate_subflow", "nr_add_subflow_port", "nr_remove_subflow_port", "nr_delete_subflow",
        "nr_create_subflow",
        "esphome_logs", "esphome_list_backups", "esphome_restore_backup",
    }
    assert expected.issubset(names)
    assert len(names) == 35
