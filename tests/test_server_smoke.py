# tests/test_server_smoke.py
import nrha_mcp.server as srv


def test_tools_registered():
    # FastMCP 1.27.1 stores registered tools in _tool_manager._tools (dict keyed by name).
    names = set(srv.mcp._tool_manager._tools.keys())
    expected = {
        "nr_get_flow_summary", "nr_get_flow", "nr_export_flow", "nr_get_subflow", "nr_search_nodes", "nr_get_function_code",
        "nr_get_node_config", "nr_get_flow_context", "nr_get_global_context",
        "nr_capture_debug", "nr_get_runtime_events", "nr_validate_flow", "nr_safe_deploy", "nr_safe_deploy_many", "nr_add_wire",
        "nr_create_nodes", "nr_delete_nodes", "nr_get_installed_modules", "nr_install_module",
        "nr_inject", "nr_diagnostics", "ha_list_ha_nodes", "ha_get_server_config",
        "nr_backup_flow", "nr_list_backups", "nr_restore_backup", "nr_diff",
        "ha_search_entities", "ha_get_state", "ha_get_services", "ha_get_entity_history", "ha_get_entity_statistics",
        "ha_check_nodered_integration", "ha_call_service", "ha_ws_command",
        "nr_instantiate_subflow", "nr_add_subflow_port", "nr_remove_subflow_port", "nr_delete_subflow",
        "nr_create_subflow",
        "esphome_list_devices", "esphome_get_config", "esphome_update_config", "esphome_validate",
        "esphome_logs", "esphome_compile", "esphome_install", "esphome_job_status",
        "esphome_cancel_job", "esphome_list_backups", "esphome_restore_backup",
    }
    assert expected.issubset(names)
    assert len(names) == 51
