"""Exercise real AstrBot APIs in a NEW isolated directory, with fake transports.

Run with an AstrBot 4.29.1 Python environment. No QQ, LLM or MC connection is made.
"""

import argparse
import asyncio
import importlib
import json
import os
import shutil
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock


def prepare_plugin(work):
    name = "astrbot_plugin_mc_monitor"
    project = Path(__file__).resolve().parents[1]
    installed = work / "data" / "plugins" / name
    installed.mkdir(parents=True)
    (work / "data" / "config").mkdir(exist_ok=True)
    for filename in ("main.py", "metadata.yaml", "_conf_schema.json", "requirements.txt"):
        shutil.copy2(project / filename, installed / filename)
    shutil.copytree(
        project / "mc_monitor",
        installed / "mc_monitor",
        ignore=shutil.ignore_patterns("__pycache__"),
    )
    return json.loads((installed / "_conf_schema.json").read_text("utf-8"))


async def check(source, work, schema):
    # Set paths before any AstrBot import: core import initializes local storage.
    os.environ["ASTRBOT_ROOT"] = str(work)
    os.environ["ASTRBOT_RELOAD"] = "0"
    sys.path[:0] = [str(work), str(source)]
    from astrbot.api import AstrBotConfig
    from astrbot.api.event import MessageChain
    from astrbot.api.star import Context
    from astrbot.core import astrbot_config, db_helper, sp
    from astrbot.core.agent.message import Message
    from astrbot.core.config.default import VERSION
    from astrbot.core.conversation_mgr import ConversationManager
    from astrbot.core.persona_mgr import PersonaManager
    from astrbot.core.provider.entities import LLMResponse
    from astrbot.core.provider.provider import Provider
    from astrbot.core.star.star_manager import PluginManager

    assert VERSION == "4.29.1", VERSION
    name = "astrbot_plugin_mc_monitor"
    plugin_config = AstrBotConfig(
        str(work / "data" / "config" / f"{name}_config.json"), schema=schema
    )
    plugin_config["logs"]["enabled"] = False
    plugin_config.save_config()
    target = "qq-compat:FriendMessage:10001"
    acm = SimpleNamespace(default_conf=astrbot_config, get_conf=lambda umo: astrbot_config)
    await db_helper.initialize()
    for ident, prompt in (("conversation-persona", "对话人格"), ("rule-persona", "会话规则人格")):
        await db_helper.insert_persona(persona_id=ident, system_prompt=prompt)
    personas = PersonaManager(db_helper, acm)
    await personas.initialize()
    conversations = ConversationManager(db_helper)
    history = [
        {"role": "user", "content": [{"type": "text", "text": "你好"}]},
        {
            "role": "assistant",
            "content": [
                {"type": "think", "think": "不应引用"},
                {"type": "text", "text": "模拟回答"},
            ],
        },
    ]
    cid = await conversations.new_conversation(
        target, content=history, persona_id="conversation-persona"
    )
    await sp.session_put(target, "session_service_config", {"persona_id": "rule-persona"})
    provider = Mock(spec=Provider)
    provider.meta.return_value = SimpleNamespace(id="compat-provider")
    provider.text_chat = AsyncMock()
    providers = SimpleNamespace(
        get_provider_by_id=AsyncMock(return_value=provider),
        get_using_provider_async=AsyncMock(return_value=provider),
    )
    platform = SimpleNamespace(
        meta=lambda: SimpleNamespace(id="qq-compat", name="aiocqhttp"),
        send_by_session=AsyncMock(),
    )
    message_history = SimpleNamespace(insert_message_chain=AsyncMock())
    context = Context(
        event_queue=asyncio.Queue(),
        config=astrbot_config,
        db=db_helper,
        provider_manager=providers,
        platform_manager=SimpleNamespace(platform_insts=[platform]),
        conversation_manager=conversations,
        message_history_manager=message_history,
        persona_manager=personas,
        astrbot_config_mgr=acm,
        knowledge_base_manager=None,
        cron_manager=None,
    )
    manager = PluginManager(context, astrbot_config)
    try:
        ok, error = await manager.load(specified_dir_name=name)
        assert ok, error
        metadata = context.get_registered_star(name)
        plugin = metadata.star_cls
        assert len(metadata.star_handler_full_names) == 5
        assert len(plugin.tasks) == 4 and all(not task.done() for task in plugin.tasks)
        assert plugin.cfg.data == dict(plugin_config)
        package = metadata.module_path.rsplit(".", 1)[0] + ".mc_monitor."
        notifier = importlib.import_module(package + "notifier")
        models = importlib.import_module(package + "models")
        settings = importlib.import_module(package + "config").Settings
        composer = notifier.Composer(context, lambda *args, **kwargs: None)
        provider.text_chat.return_value = LLMResponse(
            role="assistant", completion_text=json.dumps({"opening": notifier.OPENINGS[0]})
        )
        event = models.Event(
            "join", "2026-10-19T12:00:00+08:00", {"player": "RuntimeAlice", "online": 1}
        ).identify("compat-event")
        cfg = settings.parse({"llm": {"enabled": True, "context_messages": 6}})
        before = (await conversations.get_conversation(target, cid)).history
        text = await composer.compose(target, [event], cfg)
        assert text.startswith(notifier.OPENINGS[0]) and "RuntimeAlice" in text
        request = provider.text_chat.call_args.kwargs
        assert "会话规则人格" in request["system_prompt"]
        assert "RuntimeAlice" not in str(request)
        assert request["func_tool"] is None
        assert all(isinstance(message, Message) for message in request["contexts"])
        assert [m.content[0].text for m in request["contexts"]] == ["你好", "模拟回答"]
        cfg["llm"]["persona_id"] = "default"
        await composer.compose(target, [event], cfg)
        assert "会话规则人格" not in provider.text_chat.call_args.kwargs["system_prompt"]
        cfg["llm"]["persona_id"] = ""
        await sp.session_put(target, "session_service_config", {"persona_id": "[%None]"})
        assert await composer._persona(target, None, cfg) == ""
        provider.text_chat.side_effect = TimeoutError
        assert (await composer.compose(target, [event], cfg)).startswith("MC 监控通知")
        assert (await conversations.get_conversation(target, cid)).history == before
        assert await context.send_message(target, MessageChain().message(text)) is True
        message_history.insert_message_chain.assert_not_awaited()
        platform.send_by_session.reset_mock()
        plugin.config["enabled"] = True
        plugin.config["llm"]["enabled"] = False
        plugin.config["notifications"].update(recipients=[target], merge_seconds=0)
        plugin.config.save_config()
        assert plugin.reload_config()
        await plugin.store.ingest({}, [event], [target], plugin.cfg)
        for _ in range(100):
            if await plugin.store.pending_count() == 0:
                break
            await asyncio.sleep(0.05)
        assert await plugin.store.pending_count() == 0
        platform.send_by_session.assert_awaited_once()
        old_tasks = list(plugin.tasks)
        ok, error = await manager.reload(name)
        assert ok, error
        assert all(task.done() for task in old_tasks)
        plugin = context.get_registered_star(name).star_cls
        assert len(context.get_registered_star(name).star_handler_full_names) == 5
        await plugin.store.ingest({}, [event], [target], plugin.cfg)
        assert await plugin.store.pending_count() == 0
        tasks = list(plugin.tasks)
        await manager.turn_off_plugin(name)
        assert not plugin.tasks and all(task.done() for task in tasks)
        return {
            "astrbot": VERSION,
            "plugin": metadata.version,
            "checks": [
                "official_loader_and_schema",
                "five_commands_and_four_tasks",
                "session_persona_override",
                "explicit_default_and_no_persona",
                "segmented_text_context",
                "official_llm_and_template_fallback",
                "unchanged_conversation_history",
                "official_private_send",
                "background_queue_delivery",
                "reload_dedup_and_task_cancellation",
            ],
            "transports": "in_memory_only",
        }
    finally:
        metadata = context.get_registered_star(name)
        if metadata and metadata.activated:
            await manager.turn_off_plugin(name)
        await sp.close()
        await db_helper.engine.dispose()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--astrbot-source", type=Path, required=True)
    parser.add_argument(
        "--work-dir", type=Path, required=True, help="New directory, must not exist"
    )
    args = parser.parse_args()
    source, work = args.astrbot_source.resolve(), args.work_dir.resolve()
    if not (source / "astrbot" / "core" / "star" / "context.py").is_file():
        parser.error("--astrbot-source must contain the official AstrBot source tree")
    work.mkdir(parents=True, exist_ok=False)
    schema = prepare_plugin(work)
    print(json.dumps(asyncio.run(check(source, work, schema)), ensure_ascii=True))


if __name__ == "__main__":
    main()
