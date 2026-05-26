"""Runtime configuration via environment variables."""

from typing import Literal

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

    # Transport
    mcp_transport: Literal["stdio", "http"] = "stdio"
    mcp_http_host: str = "0.0.0.0"
    mcp_http_port: int = 8080
