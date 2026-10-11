"""Lifecycle/handler tests use a small API stub, not a real AstrBot installation."""

import asyncio
import importlib.util
import json
import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from .conftest import TARGET


@pytest.fixture
def plugin_module(monkeypatch, tmp_path):
    class FakeFilter:
        PermissionType = SimpleNamespace(ADMIN="admin")

        @staticmethod
        def permission_type(value):
            def decorate(fn):
                fn.required_permission = value
                return fn

            return decorate

        @staticmethod
        def command(name):
            return lambda fn: fn

    class FakeStar:
        def __init__(self, context):
            self.context = context

    class FakeChain:
        def message(self, text):
            self.text = text
            return self

    modules = {
        "astrbot": {},
        "astrbot.api": {"AstrBotConfig": dict, "logger": SimpleNamespace(info=lambda text: None)},
        "astrbot.api.event": {
            "AstrMessageEvent": object,
            "MessageChain": FakeChain,
            "filter": FakeFilter,
        },
        "astrbot.api.star": {"Context": object, "Star": FakeStar},
        "astrbot.core": {},
        "astrbot.core.utils": {},
        "astrbot.core.utils.astrbot_path": {"get_astrbot_data_path": lambda: str(tmp_path)},
    }
    for name, attrs in modules.items():
        module = ModuleType(name)
        module.__dict__.update(attrs)
        monkeypatch.setitem(sys.modules, name, module)
    root = Path(__file__).resolve().parents[1]
    package = ModuleType("isolated_mc_plugin")
    package.__path__ = [str(root)]
    monkeypatch.setitem(sys.modules, package.__name__, package)
    spec = importlib.util.spec_from_file_location("isolated_mc_plugin.main", root / "main.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


async def collect(iterator):
    return [x async for x in iterator]


async def test_lifecycle_binding_test_message_and_shutdown(plugin_module):
    context = SimpleNamespace(send_message=AsyncMock(return_value=True))
    plugin = plugin_module.MinecraftMonitor(
        context,
        {
            "enabled": True,
            "logs": {"enabled": False},
            "llm": {"enabled": False},
            "notifications": {"merge_seconds": 0},
        },
    )
    await plugin.initialize()
    tasks = list(plugin.tasks)
    event = SimpleNamespace(
        unified_msg_origin=TARGET,
        get_platform_name=lambda: "aiocqhttp",
        plain_result=lambda text: text,
    )
    try:
        assert plugin.bind.required_permission == "admin"
        assert plugin.test_message.required_permission == "admin"
        await collect(plugin.bind(event))
        assert await plugin.store.get("bound_recipients") == [TARGET]
        await collect(plugin.test_message(event))
        await asyncio.sleep(1.1)
        assert context.send_message.await_count == 1
        target, chain = context.send_message.call_args.args
        assert target == TARGET and "通知链路测试" in chain.text
    finally:
        await plugin.terminate()
    assert all(task.done() for task in tasks)
    assert plugin.tasks == []


async def test_config_file_hot_reload_invalid_retains_last(plugin_module, tmp_path):
    class Config(dict):
        pass

    config = Config({"enabled": False, "logs": {"enabled": False}})
    path = tmp_path / "config.json"
    config.config_path = str(path)
    path.write_text(json.dumps(dict(config)), "utf-8")
    plugin = plugin_module.MinecraftMonitor(SimpleNamespace(), config)
    await plugin.initialize()
    try:
        assert plugin.cfg["enabled"] is False
        path.write_text(
            json.dumps({"enabled": True, "logs": {"enabled": False}, "poll_seconds": 3}), "utf-8"
        )
        await asyncio.sleep(2.1)
        assert plugin.cfg["poll_seconds"] == 3
        assert plugin.cfg["enabled"] is True
        assert plugin.reload_config({"poll_seconds": 0}) is False
        assert plugin.cfg["poll_seconds"] == 3
    finally:
        await plugin.terminate()


async def test_panel_test_action_persistent_edge_and_rearm(plugin_module):
    context = SimpleNamespace(send_message=AsyncMock(return_value=True))
    config = {
        "enabled": True,
        "logs": {"enabled": False},
        "llm": {"enabled": False},
        "notifications": {"recipients": [TARGET], "send_test_once": True, "merge_seconds": 0},
    }
    plugin = plugin_module.MinecraftMonitor(context, config)
    await plugin.initialize()
    try:
        for task in plugin.tasks:
            task.cancel()
        await asyncio.gather(*plugin.tasks, return_exceptions=True)
        await plugin.apply_config_actions()
        await plugin.apply_config_actions()
        assert await plugin.store.pending_count() == 1
        assert await plugin.store.get("panel_test_seen") is True
    finally:
        await plugin.terminate()
    restarted = plugin_module.MinecraftMonitor(context, config)
    await restarted.initialize()
    try:
        for task in restarted.tasks:
            task.cancel()
        await asyncio.gather(*restarted.tasks, return_exceptions=True)
        await restarted.apply_config_actions()
        assert await restarted.store.get("panel_test_seen") is True
        restarted.cfg["notifications"]["send_test_once"] = False
        await restarted.apply_config_actions()
        assert await restarted.store.get("panel_test_seen") is False
        restarted.cfg["notifications"]["send_test_once"] = True
        await restarted.apply_config_actions()
        assert await restarted.store.get("panel_test_seen") is True
    finally:
        await restarted.terminate()
