"""Configuration defaults come from the same schema AstrBot displays."""

from __future__ import annotations

import copy
import json
import math
import re
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from urllib.parse import urlparse
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

KINDS = (
    "join",
    "quit",
    "whitelist",
    "auth",
    "kick",
    "ban",
    "server_start",
    "server_stop",
    "crash",
    "restart",
    "container",
    "alert",
    "recovery",
    "monitor_error",
    "test",
)


@lru_cache(maxsize=1)
def config_schema():
    return json.loads((Path(__file__).parent.parent / "_conf_schema.json").read_text("utf-8"))


def defaults(schema=None):
    if schema is None:
        schema = config_schema()
    return {
        key: defaults(item["items"]) if item["type"] == "object" else copy.deepcopy(item["default"])
        for key, item in schema.items()
    }


def merge(base, incoming):
    for key, value in incoming.items():
        if key not in base:
            continue
        if isinstance(base[key], dict) and isinstance(value, dict):
            merge(base[key], value)
        else:
            base[key] = copy.deepcopy(value)
    return base


def valid_umo(value):
    return isinstance(value, str) and bool(re.fullmatch(r"[^:\s]+:FriendMessage:[^\s:]+", value))


@dataclass(repr=False)
class Settings:
    data: dict = field(repr=False)

    @classmethod
    def parse(cls, raw):
        if not isinstance(raw, dict):
            raise ValueError("configuration")
        data = merge(defaults(), raw)
        schema = config_schema()

        def check(items, values, prefix=""):
            for key, spec in items.items():
                name, value = prefix + key, values[key]
                typ = spec["type"]
                if typ == "object":
                    if not isinstance(value, dict):
                        raise ValueError(name)
                    check(spec["items"], value, name + ".")
                    continue
                expected = {
                    "bool": bool,
                    "int": int,
                    "float": (int, float),
                    "string": str,
                    "text": str,
                    "list": list,
                }[typ]
                if not isinstance(value, expected) or (
                    typ in ("int", "float") and isinstance(value, bool)
                ):
                    raise ValueError(name)
                if typ in ("int", "float") and (
                    not math.isfinite(value)
                    or value < spec.get("minimum", -math.inf)
                    or value > spec.get("maximum", math.inf)
                ):
                    raise ValueError(name)
                if "options" in spec and value not in spec["options"]:
                    raise ValueError(name)

        check(schema, data)
        try:
            ZoneInfo(data["timezone"])
        except (ZoneInfoNotFoundError, ValueError) as exc:
            raise ValueError("timezone") from exc
        if any(not valid_umo(x) for x in data["notifications"]["recipients"]):
            raise ValueError("notifications.recipients")
        for key in ("quiet_start", "quiet_end"):
            if not re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d", data["notifications"][key]):
                raise ValueError("notifications." + key)
        docker = data["docker"]
        if docker["enabled"]:
            url = urlparse(docker["bridge_url"])
            if (
                url.scheme not in ("http", "https")
                or not url.hostname
                or url.username
                or url.password
                or url.query
                or url.fragment
                or url.path not in ("", "/")
            ):
                raise ValueError("docker.bridge_url")
            if not docker["container"]:
                raise ValueError("docker.container")
        if data["logs"]["enabled"] and not data["logs"]["path"]:
            raise ValueError("logs.path")
        if data["logs"]["max_line_bytes"] >= data["logs"]["max_bytes"]:
            raise ValueError("logs.max_line_bytes")
        if data["notifications"]["retry_max"] < data["notifications"]["retry_base"]:
            raise ValueError("notifications.retry_max")
        if data["rcon"]["enabled"] and not data["rcon"]["host"]:
            raise ValueError("rcon.host")
        if data["status"]["enabled"] and not data["status"]["host"]:
            raise ValueError("status.host")
        for group in ("rcon", "docker"):
            if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", data[group]["secret_env"]):
                raise ValueError(group + ".secret_env")
        return cls(data)

    def __getitem__(self, key):
        return self.data[key]

    def policy(self, kind):
        return self.data["events"][kind]
