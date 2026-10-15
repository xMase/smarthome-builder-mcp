"""Runtime configuration via environment variables."""

from typing import Literal

from pydantic import SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # Node-RED Admin API
    nr_url: str = "http://localhost:1880"
    nr_token: str = ""
    nr_user: str = ""
    nr_pass: str = ""
    nr_auth: Literal["basic", "token"] = "basic"
    nr_verify_ssl: bool = True

    # Safety
    deploy_mode: Literal["propose", "auto"] = "propose"
    backup_dir: str = "./backups"

    # Home Assistant REST API (entity discovery for authoring)
    # HA base URL is :8123, distinct from Node-RED's :1880
    ha_url: str = ""
    ha_token: str = ""

    # Home Assistant service-call safety (ha_call_service)
    # csv of "domain.service" or "domain.*"
    ha_block_services: str = ""            # additive to the catastrophic floor
    ha_confirm_services: str | None = None  # override-with-default: None=default, ""=gate nothing

    # Home Assistant WebSocket command safety (ha_ws_command)
    ha_ws_block_commands: str = ""            # additive block-list of WS command types
    ha_ws_read_commands: str | None = None    # additive to the read allowlist (None=just defaults)

    # ESPHome Device Builder via HA ingress (esphome_* tools; needs HA_URL + HA_TOKEN)
    esphome_addon_slug: str = "5c53de3b_esphome"
    esphome_allow_secrets: bool = False   # unblocks secrets.yaml / secrets.yml

    # Transport
    mcp_transport: Literal["stdio", "http"] = "stdio"
    mcp_http_host: str = "0.0.0.0"
    mcp_http_port: int = 8080
    mcp_auth_token: SecretStr | None = None  # required in http mode; checked in server.main()

    @field_validator("mcp_auth_token", mode="before")
    @classmethod
    def _blank_token_is_none(cls, v):
        # Strip copy-paste whitespace; MCP_AUTH_TOKEN="" overrides a .env token and means "none".
        if isinstance(v, str):
            v = v.strip()
            return v or None
        return v
