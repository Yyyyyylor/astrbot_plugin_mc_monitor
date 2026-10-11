import asyncio
from unittest.mock import AsyncMock

import pytest

from mc_monitor.engine import Engine

from .conftest import TARGET, no_log
from .test_parser import line


async def test_pipeline_log_restart_no_duplicate_and_failed_transaction(store, cfg, tmp_path):
    path = tmp_path / "latest.log"
    path.write_text(
        line('Done (1.23s)! For help, type "help"') + "\n" + line("Alice joined the game") + "\n",
        "utf-8",
    )
    cfg["logs"].update(enabled=True, path=str(path), initial_position="beginning")
    engine = Engine(store, lambda: cfg, lambda c: [TARGET], no_log)
    await engine.log_step(cfg)
    assert await store.pending_count() == 2
    await Engine(store, lambda: cfg, lambda c: [TARGET], no_log).log_step(cfg)
    assert await store.pending_count() == 2
    # Queue exhaustion must rollback cursor and parser, then successfully resume.
    cfg["notifications"]["queue_limit"] = 2
    with path.open("a", encoding="utf-8") as file:
        file.write(line("Bob joined the game") + "\n")
    old = await store.get("cursor:" + str(path))
    with pytest.raises(RuntimeError, match="queue_full"):
        await engine.log_step(cfg)
    assert await store.get("cursor:" + str(path)) == old
    assert (await store.get("parser"))["players"] == ["Alice"]
    cfg["notifications"]["queue_limit"] = 10000
    await engine.log_step(cfg)
    assert (await store.get("parser"))["players"] == ["Alice", "Bob"]


async def test_threshold_edges_recovery_and_missing_metric(store, cfg):
    cfg["rcon"].update(enabled=True, performance_source="paper")
    cfg["alerts"].update(consecutive=2, recover_samples=2)
    engine = Engine(store, lambda: cfg, lambda c: [TARGET], no_log)
    low = {"tps": 10, "mspt": None, "players": None, "online": None}
    good = {**low, "tps": 20}
    engine.rcon.query = AsyncMock(
        side_effect=[low, low, low, {**low, "tps": None}, good, good, good]
    )
    for _ in range(7):
        await engine.probe_step(cfg)
    rows = await store.due(float("inf"))
    events = [store.decode(x) for x in rows]
    assert [e.kind for e in events].count("alert") == 1
    assert [e.kind for e in events].count("recovery") == 1
    assert (await store.get("metrics"))["cpu"] is None


async def test_collector_disconnect_episode_and_recovery(store, cfg):
    engine = Engine(store, lambda: cfg, lambda c: [TARGET], no_log)
    for good in [False, False, False, False, True, True]:
        await engine.source_health("docker", good, cfg)
    rows = await store.due(float("inf"))
    assert sorted(store.decode(x).kind for x in rows) == ["monitor_error", "recovery"]


async def test_container_changes_not_every_poll(store, cfg):
    cfg["docker"]["enabled"] = True
    engine = Engine(store, lambda: cfg, lambda c: [TARGET], no_log)
    running = {
        "container": "minecraft",
        "state": {
            "id": "a",
            "status": "running",
            "started_at": "2026-10-11T00:00:00Z",
            "restart_count": 0,
        },
        "metrics": {},
    }
    exited = {
        **running,
        "state": {**running["state"], "status": "exited", "exit_code": 137, "oom_killed": True},
    }
    restarted = {
        **running,
        "state": {**running["state"], "started_at": "2026-10-11T01:00:00Z", "restart_count": 1},
    }
    engine.docker.snapshot = AsyncMock(
        side_effect=[running, running, exited, exited, restarted, restarted]
    )
    for _ in range(6):
        await engine.probe_step(cfg)
    kinds = [store.decode(x).kind for x in await store.due(float("inf"))]
    assert kinds.count("container") == 2
    assert kinds.count("crash") == 1
    assert kinds.count("restart") == 1


async def test_monitor_loop_can_cancel_and_resources_close(store, cfg):
    cfg.data["enabled"] = False
    engine = Engine(store, lambda: cfg, lambda c: [], no_log)
    task = asyncio.create_task(engine.run())
    await asyncio.sleep(0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    await engine.close()


async def test_unresponsive_alarm_and_recovery(store, cfg, monkeypatch):
    cfg["status"]["enabled"] = True
    cfg["alerts"].update(consecutive=2, recover_samples=2)
    monkeypatch.setattr(
        "mc_monitor.engine.java_status",
        AsyncMock(
            side_effect=[
                OSError(),
                OSError(),
                OSError(),
                {"responsive": True, "online": 0},
                {"responsive": True, "online": 0},
            ]
        ),
    )
    engine = Engine(store, lambda: cfg, lambda c: [TARGET], no_log)
    for _ in range(5):
        await engine.probe_step(cfg)
    events = [store.decode(x) for x in await store.due(float("inf"))]
    assert [e.kind for e in events].count("alert") == 1
    assert [e.kind for e in events].count("recovery") == 1
    assert all(e.facts["metric"] == "responsive" for e in events)
