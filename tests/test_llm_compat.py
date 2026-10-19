import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from mc_monitor.notifier import Composer, recent_text

from .conftest import TARGET, no_log


@pytest.mark.parametrize("selected", [{"prompt": "会话规则指定的人格"}, None])
async def test_host_resolver_has_priority_over_conversation_persona(cfg, selected):
    manager = SimpleNamespace(
        resolve_selected_persona=AsyncMock(return_value=("forced", selected, "forced", False))
    )
    context = SimpleNamespace(persona_manager=manager)
    conversation = SimpleNamespace(persona_id="[%None]")
    prompt = await Composer(context, no_log)._persona(TARGET, conversation, cfg)
    assert prompt == (selected["prompt"] if selected else "")
    manager.resolve_selected_persona.assert_awaited_once_with(
        umo=TARGET, conversation_persona_id="[%None]", platform_name="aiocqhttp"
    )


@pytest.mark.parametrize("persona_id", ["default", "[%None]"])
async def test_explicit_persona_overrides_host_session_rule(cfg, persona_id):
    cfg["llm"]["persona_id"] = persona_id
    manager = SimpleNamespace(
        resolve_selected_persona=AsyncMock(),
        get_persona_v3_by_id=Mock(return_value={"prompt": "内置默认人格"}),
    )
    context = SimpleNamespace(persona_manager=manager)
    prompt = await Composer(context, no_log)._persona(TARGET, None, cfg)
    assert prompt == ("" if persona_id == "[%None]" else "内置默认人格")
    manager.resolve_selected_persona.assert_not_awaited()
    assert manager.get_persona_v3_by_id.call_count == (persona_id != "[%None]")


async def test_older_host_conversation_persona_api_remains_supported(cfg):
    manager = SimpleNamespace(
        get_persona=AsyncMock(return_value=SimpleNamespace(system_prompt="旧版人格"))
    )
    context = SimpleNamespace(persona_manager=manager)
    conversation = SimpleNamespace(persona_id="legacy")
    assert await Composer(context, no_log)._persona(TARGET, conversation, cfg) == "旧版人格"
    manager.get_persona.assert_awaited_once_with("legacy")


def test_segmented_and_legacy_history_include_text_only():
    history = [
        {"role": "system", "content": "不应引用"},
        {
            "role": "user",
            "content": [
                {"type": "text", "text": "你好"},
                {"type": "image_url", "image_url": {"url": "private-image"}},
                {"type": "text", "text": "呀"},
            ],
        },
        {"role": "assistant", "content": "工具调用", "tool_calls": [{"id": "tool"}]},
        {"role": "tool", "content": "工具结果"},
        {"role": "assistant", "content": "旧版回答"},
        None,
    ]
    assert recent_text(json.dumps(history), 6) == [("user", "你好呀"), ("assistant", "旧版回答")]
    assert recent_text(json.dumps(history), 0) == []
    assert recent_text(json.dumps([{"role": "user", "content": "x" * 1000}]), 1) == [
        ("user", "x" * 600)
    ]


@pytest.mark.parametrize("history", [None, "{", "{}", "null", "42"])
def test_invalid_optional_history_is_ignored(history):
    assert recent_text(history, 6) == []
