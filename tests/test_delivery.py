import asyncio
import json
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock

from mc_monitor.models import Event
from mc_monitor.notifier import OPENINGS, Composer, Notifier
from mc_monitor.storage import Store

from .conftest import TARGET, no_log


def event(ident="one", kind="join"):
    return Event(kind, "2026-10-11T10:00:00+08:00", {"player": "Alice", "online": 3}).identify(
        ident
    )


async def test_atomic_ingest_dedup_and_restart(store, cfg):
    await store.ingest({"cursor": 123}, [event()], [TARGET], cfg)
    await store.ingest({"cursor": 123}, [event()], [TARGET], cfg)
    restarted = Store(store.path)
    assert await restarted.pending_count() == 1
    assert await restarted.get("cursor") == 123


async def test_send_failure_false_and_recovery(store, cfg):
    await store.ingest({}, [event()], [TARGET], cfg)
    send = AsyncMock(side_effect=[OSError("private secret"), False, True])
    compose = AsyncMock(return_value="message")
    notifier = Notifier(store, lambda: cfg, lambda c: [TARGET], compose, send, no_log)
    now = time.time() + 1
    await notifier.step(now)
    assert await store.pending_count() == 1
    await notifier.step(now + 6)
    assert await store.pending_count() == 1
    notifier.store = Store(store.path)
    await notifier.step(now + 20)
    assert await store.pending_count() == 0
    assert send.await_count == 3


async def test_multi_recipient_no_repeat_for_successful_target(store, cfg):
    second = "qq-main:FriendMessage:10002"
    cfg["notifications"]["recipients"].append(second)
    await store.ingest({}, [event()], [TARGET, second], cfg)
    calls = []

    async def send(target, text):
        calls.append(target)
        return target == TARGET or calls.count(second) > 1

    notifier = Notifier(
        store,
        lambda: cfg,
        lambda c: c["notifications"]["recipients"],
        AsyncMock(return_value="message"),
        send,
        no_log,
    )
    now = time.time() + 1
    await notifier.step(now)
    await notifier.step(now + 10)
    assert calls.count(TARGET) == 1
    assert calls.count(second) == 2


async def test_normal_merge_and_high_priority(store, cfg):
    cfg["notifications"]["merge_seconds"] = 20
    await store.ingest({}, [event("a"), event("b"), event("urgent", "crash")], [TARGET], cfg)
    send = AsyncMock(return_value=True)
    compose = AsyncMock(side_effect=lambda target, events, c: ",".join(e.kind for e in events))
    notifier = Notifier(store, lambda: cfg, lambda c: [TARGET], compose, send, no_log)
    now = time.time() + 1
    await notifier.step(now)
    assert send.call_args.args[1] == "crash"
    await notifier.step(now + 21)
    assert send.call_args.args[1] == "join,join"


async def test_cooldown_defers_and_keeps_events(store, cfg):
    cfg["events"]["join"]["cooldown"] = 30
    send = AsyncMock(return_value=True)
    notifier = Notifier(
        store, lambda: cfg, lambda c: [TARGET], AsyncMock(return_value="ok"), send, no_log
    )
    now = time.time() + 1
    await store.ingest({}, [event("a")], [TARGET], cfg)
    await notifier.step(now)
    await store.ingest({}, [event("b"), event("c")], [TARGET], cfg)
    await notifier.step(now + 2)
    assert await store.pending_count() == 2
    assert send.await_count == 1
    await notifier.step(now + 31)
    assert await store.pending_count() == 0
    assert send.await_count == 2


async def test_quiet_time_high_bypass(store, cfg):
    cfg["notifications"].update(quiet_enabled=True, quiet_start="00:00", quiet_end="00:00")
    await store.ingest({}, [event("a"), event("b", "crash")], [TARGET], cfg)
    send = AsyncMock(return_value=True)
    notifier = Notifier(
        store, lambda: cfg, lambda c: [TARGET], AsyncMock(return_value="ok"), send, no_log
    )
    await notifier.step(time.time() + 1)
    assert await store.pending_count() == 1
    assert send.await_count == 1


def fake_context(response):
    context = SimpleNamespace(
        get_current_chat_provider_id=AsyncMock(return_value="provider"),
        conversation_manager=SimpleNamespace(get_curr_conversation_id=AsyncMock(return_value=None)),
        persona_manager=SimpleNamespace(
            get_default_persona_v3=AsyncMock(return_value={"prompt": "角色人格"})
        ),
        llm_generate=AsyncMock(return_value=SimpleNamespace(completion_text=response)),
    )
    return context


async def test_llm_persona_facts_separate_and_never_written(cfg):
    cfg["llm"]["enabled"] = True
    context = fake_context(json.dumps({"opening": OPENINGS[0]}, ensure_ascii=False))
    result = await Composer(context, no_log).compose(TARGET, [event()], cfg)
    assert result.startswith(OPENINGS[0])
    assert "Alice" in result and "在线人数：3" in result
    kwargs = context.llm_generate.call_args.kwargs
    assert "角色人格" in kwargs["system_prompt"]
    assert "Alice" not in str(kwargs)
    assert kwargs["tools"] is None
    assert kwargs["contexts"] == []


async def test_llm_hallucination_and_timeout_fallback(cfg):
    cfg["llm"].update(enabled=True, timeout=0.1)
    context = fake_context('{"opening":"Alice 在线人数 100"}')
    result = await Composer(context, no_log).compose(TARGET, [event()], cfg)
    assert result.startswith("MC 监控通知") and "100" not in result

    async def slow(**kwargs):
        await asyncio.sleep(1)

    context.llm_generate = slow
    result = await Composer(context, no_log).compose(TARGET, [event()], cfg)
    assert result.startswith("MC 监控通知")


async def test_high_lane_not_blocked_by_normal_llm(store, cfg):
    blocked = asyncio.Event()
    entered = asyncio.Event()

    async def compose(target, events, settings):
        if events[0].kind == "join":
            entered.set()
            await blocked.wait()
        return events[0].kind

    await store.ingest({}, [event("normal"), event("urgent", "crash")], [TARGET], cfg)
    send = AsyncMock(return_value=True)
    notifier = Notifier(store, lambda: cfg, lambda c: [TARGET], compose, send, no_log)
    normal = asyncio.create_task(notifier.step(level="normal"))
    try:
        await asyncio.wait_for(entered.wait(), 1)
        await asyncio.wait_for(notifier.step(level="high"), 1)
        assert send.call_args.args[1] == "crash"
        assert not normal.done()
    finally:
        blocked.set()
        await normal
