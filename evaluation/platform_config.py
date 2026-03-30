"""Central platform configuration loader.

All evaluation code imports from here instead of hardcoding values.
"""
import os
from pathlib import Path
from typing import Any

try:
    import tomllib
except ModuleNotFoundError:
    import tomli as tomllib

_config: dict[str, Any] | None = None
_CONFIG_PATH = Path(__file__).parent / "oas_platform.toml"


def load_config(path: Path | None = None) -> dict[str, Any]:
    global _config
    if _config is not None and path is None:
        return _config
    p = path or _CONFIG_PATH
    with open(p, "rb") as f:
        data = tomllib.load(f)
    if path is None:
        _config = data
    return data


def hostname() -> str:
    return load_config()["platform"]["hostname"]


def project_name() -> str:
    return load_config()["platform"]["project_name"]


def credentials() -> dict[str, str]:
    return load_config()["credentials"]


def service_url(service: str, host: str | None = None) -> str:
    cfg = load_config()
    h = host or cfg["platform"]["hostname"]
    port = cfg["services"].get(service)
    if port is None:
        raise KeyError(f"Unknown service: {service}")
    return f"http://{h}:{port}"


def admin_user() -> str:
    return load_config()["credentials"]["admin_user"]


def admin_pass() -> str:
    return load_config()["credentials"]["admin_pass"]


def gitlab_user() -> str:
    return load_config()["credentials"]["gitlab_user"]


def gitlab_pass() -> str:
    return load_config()["credentials"]["gitlab_pass"]


def encryption_key() -> str:
    return load_config()["encryption"]["key"]


def email_domain() -> str:
    return load_config()["platform"]["email_domain"]


def service_port(service: str) -> int:
    port = load_config()["services"].get(service)
    if port is None:
        raise KeyError(f"Unknown service: {service}")
    return port
