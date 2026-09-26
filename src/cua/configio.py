"""Loading and saving YAML-backed models, and resolving secret references."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel

from .models import AppProfile, Overlay, Policy, TenantConfig


class SecretMissing(RuntimeError):
    pass


def repo_root() -> Path:
    env = os.environ.get("CUA_HOME")
    return Path(env) if env else Path(__file__).resolve().parents[2]


def config_dir() -> Path:
    return repo_root() / "config"


def load_yaml(path: Path) -> Any:
    with open(path, encoding="utf-8") as f:
        return yaml.safe_load(f)


def load_model[M: BaseModel](path: Path, model: type[M]) -> M:
    try:
        return model.model_validate(load_yaml(path))
    except Exception as e:  # add the file name to validation errors
        raise ValueError(f"{path}: {e}") from e


class _Dumper(yaml.SafeDumper):
    pass


def _str(dumper: yaml.SafeDumper, data: str) -> yaml.ScalarNode:
    style = "|" if "\n" in data else None
    return dumper.represent_scalar("tag:yaml.org,2002:str", data, style=style)


_Dumper.add_representer(str, _str)


def prune(data: Any) -> Any:
    """Drop empty containers and noisy defaults so reviewed YAML stays short."""
    if isinstance(data, dict):
        out = {}
        for k, v in data.items():
            v = prune(v)
            if v in ([], {}, None) or (k == "optional" and v is False):
                continue
            out[k] = v
        return out
    if isinstance(data, list):
        return [prune(x) for x in data]
    return data


def dump_yaml(data: Any) -> str:
    return yaml.dump(data, Dumper=_Dumper, sort_keys=False, allow_unicode=True, width=110)


def model_to_yaml(model: BaseModel) -> str:
    return dump_yaml(prune(model.model_dump(mode="json", exclude_none=True)))


def load_app_profile(product: str) -> AppProfile:
    return load_model(config_dir() / "apps" / f"{product}.yaml", AppProfile)


def load_tenant(tenant_id: str) -> TenantConfig:
    return load_model(config_dir() / "tenants" / f"{tenant_id}.yaml", TenantConfig)


def load_policy(policy_id: str) -> Policy:
    return load_model(config_dir() / "policy" / f"{policy_id}.yaml", Policy)


def load_overlays() -> list[Overlay]:
    folder = config_dir() / "overlays"
    return [load_model(p, Overlay) for p in sorted(folder.glob("*.yaml"))] if folder.exists() else []


def resolve_secret(ref: str) -> str:
    scheme, _, name = ref.partition(":")
    if scheme != "env":
        raise SecretMissing(f"unsupported secret scheme {scheme!r} (only env: in this prototype)")
    value = os.environ.get(name)
    if not value:
        raise SecretMissing(f"secret {name} is not set (see .env.example)")
    return value
