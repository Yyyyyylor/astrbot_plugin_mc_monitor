from __future__ import annotations

import asyncio
import math
import time
from datetime import datetime

from .models import Event, canonical, utc_now
from .parser import Parser
from .probes import DockerProbe, Rcon, java_status
from .tailer import Tailer


class Engine:
    def __init__(self, store, settings, targets, log, wake_event=None):
        self.store, self.settings, self.targets, self.log = store, settings, targets, log
        self.docker = DockerProbe()
        self.rcon = Rcon()
        self.tailer = Tailer()
        self.last_prune = 0
        self.logged_errors = {}
        self.wake_event = wake_event or asyncio.Event()

    async def emit(self, changes, events, cfg):
        await self.store.ingest(changes, events, self.targets(cfg), cfg)

    async def log_step(self, cfg):
        if not cfg["logs"]["enabled"]:
            return
        key = "cursor:" + cfg["logs"]["path"]
        saved = await self.store.get(key)
        worker = asyncio.create_task(asyncio.to_thread(self.tailer.read, cfg["logs"], saved))
        try:
            batch = await asyncio.shield(worker)
        except asyncio.CancelledError:
            await worker
            raise
        state = await self.store.get("parser")
        if state and state.get("source_path") != cfg["logs"]["path"]:
            state = None
        parser = Parser(cfg["timezone"], state)
        parser.state["source_path"] = cfg["logs"]["path"]
        if any(
            x in batch.issues
            for x in ("rotation_source_missing_possible_gap", "log_truncated_or_rewritten")
        ):
            parser.state.update(complete=False, players=[])
        events = []
        for line, origin, mtime in batch.lines:
            events.extend(parser.parse(line, origin, mtime))
        for issue in batch.issues:
            events.append(
                Event("monitor_error", utc_now(), {"source": "logs", "reason": issue}).identify(
                    f"logissue:{key}:{canonical(batch.cursor)}:{issue}"
                )
            )
        await self.emit({key: batch.cursor, "parser": parser.state}, events, cfg)

    async def source_health(self, source, good, cfg):
        key = "health:" + source
        health = await self.store.get(key, {"failures": 0, "active": False, "epoch": 0})
        health["failures"] = 0 if good else min(health["failures"] + 1, 100000)
        events = []
        if not good and health["failures"] >= cfg["alerts"]["consecutive"] and not health["active"]:
            health.update(active=True, epoch=health["epoch"] + 1)
            events.append(
                Event(
                    "monitor_error",
                    utc_now(),
                    {"source": source, "reason": "collection_unavailable"},
                ).identify(f"health:{source}:{health['epoch']}:failed")
            )
        elif good and health["active"]:
            health.update(active=False)
            if cfg["notifications"]["recovery"]:
                events.append(
                    Event(
                        "recovery", utc_now(), {"source": source, "reason": "collection_resumed"}
                    ).identify(f"health:{source}:{health['epoch']}:recovered")
                )
        await self.emit({key: health}, events, cfg)

    async def probe_step(self, cfg):
        jobs = []
        sources = []
        if cfg["docker"]["enabled"]:
            jobs.append(self.docker.snapshot(cfg["docker"], cfg["io_timeout"]))
            sources.append("docker")
        if cfg["status"]["enabled"]:
            jobs.append(java_status(cfg["status"], cfg["io_timeout"]))
            sources.append("status")
        if cfg["rcon"]["enabled"]:
            jobs.append(self.rcon.query(cfg["rcon"], cfg["io_timeout"]))
            sources.append("rcon")
        results = await asyncio.gather(*jobs, return_exceptions=True)
        metrics = {
            "at": utc_now(),
            "tps": None,
            "mspt": None,
            "cpu": None,
            "memory": None,
            "memory_bytes": None,
            "memory_limit_bytes": None,
            "players": None,
            "online": None,
            "responsive": None,
            "container": None,
            "sources": {},
        }
        changes, events = {}, []
        for source, result in zip(sources, results, strict=True):
            good = not isinstance(result, BaseException)
            metrics["sources"][source] = "ok" if good else "unavailable"
            if source != "status":
                await self.source_health(source, good, cfg)
            if not good:
                if source == "status":
                    metrics["responsive"] = False
                continue
            if source == "docker":
                state = result.get("state", {})
                # Only accept fixed bridge fields; never preserve inspect env/mounts/raw errors.
                current = {
                    k: state.get(k)
                    for k in (
                        "id",
                        "status",
                        "started_at",
                        "finished_at",
                        "exit_code",
                        "oom_killed",
                        "restart_count",
                    )
                }
                metrics["container"] = current
                old = await self.store.get("docker_state:" + cfg["docker"]["container"])
                if old and current != old:
                    transition = old["status"] != current["status"]
                    restarted = (
                        bool(old["started_at"])
                        and current["status"] == "running"
                        and (
                            current["id"] != old["id"] or current["started_at"] != old["started_at"]
                        )
                    )
                    if transition:
                        events.append(
                            Event(
                                "container",
                                metrics["at"],
                                {
                                    "before": old["status"],
                                    "after": current["status"],
                                    "exit_code": current["exit_code"],
                                    "oom_killed": current["oom_killed"],
                                },
                            ).identify("docker:" + canonical(current))
                        )
                    if current["status"] in ("exited", "dead") and (
                        current["oom_killed"] or current["exit_code"] not in (None, 0)
                    ):
                        events.append(
                            Event(
                                "crash",
                                metrics["at"],
                                {
                                    "basis": "container_abnormal_exit",
                                    "exit_code": current["exit_code"],
                                    "oom_killed": current["oom_killed"],
                                },
                            ).identify("dockercrash:" + canonical(current))
                        )
                    if restarted:
                        parser = await self.store.get("parser", {})
                        stop = parser.get("last_stop_at")
                        try:
                            clean = bool(stop) and datetime.fromisoformat(
                                stop
                            ) >= datetime.fromisoformat(old["started_at"].replace("Z", "+00:00"))
                        except (TypeError, ValueError, AttributeError):
                            clean = False
                        if not clean:
                            events.append(
                                Event(
                                    "restart",
                                    metrics["at"],
                                    {
                                        "basis": "generation_changed_without_stop_log",
                                        "inferred": True,
                                        "restart_count": current["restart_count"],
                                    },
                                ).identify("dockerrestart:" + canonical(current))
                            )
                changes["docker_state:" + cfg["docker"]["container"]] = current
                for key in ("cpu", "memory", "memory_bytes", "memory_limit_bytes"):
                    value = result.get("metrics", {}).get(key)
                    if type(value) in (int, float) and math.isfinite(value) and value >= 0:
                        metrics[key] = value
            else:
                metrics.update(result)
        parser = await self.store.get("parser", {})
        container = metrics["container"]
        if container:
            invalid = container["status"] != "running"
            if not invalid and container.get("started_at"):
                try:
                    invalid = not parser.get("last_start_at") or datetime.fromisoformat(
                        parser["last_start_at"]
                    ) < datetime.fromisoformat(container["started_at"].replace("Z", "+00:00"))
                except (ValueError, TypeError):
                    invalid = True
            if invalid:
                parser.update(complete=False, players=[])
                changes["parser"] = parser
        if cfg["logs"]["enabled"]:
            log_health = await self.store.get("health:logs", {})
            if log_health.get("failures", 0):
                parser.update(complete=False)
                changes["parser"] = parser
        if metrics["players"] is not None:
            parser.update(players=metrics["players"], complete=True)
            changes["parser"] = parser
        elif cfg["logs"]["enabled"] and parser.get("complete"):
            # The list is known only since last log startup/RCON reconciliation; label the source.
            metrics["players"] = parser.get("players", [])
            if metrics["online"] is None:
                metrics["online"] = len(metrics["players"])
            metrics["sources"]["players"] = "log_observed"
        alert_state = await self.store.get("alerts", {})
        specs = {
            "tps": (cfg["alerts"]["low_tps"], True),
            "mspt": (cfg["alerts"]["high_mspt"], False),
            "cpu": (cfg["alerts"]["high_cpu"], False),
            "memory": (cfg["alerts"]["high_memory"], False),
        }
        if cfg["status"]["enabled"]:
            specs["responsive"] = (1, True)
        for metric, (threshold, low) in specs.items():
            value = metrics[metric]
            entry = alert_state.setdefault(
                metric, {"active": False, "bad": 0, "good": 0, "epoch": 0}
            )
            if not threshold:
                entry.update(active=False, bad=0, good=0)
                continue
            if value is None:
                entry.update(bad=0, good=0)
                continue
            bad = value < threshold if low else value > threshold
            margin = cfg["alerts"]["hysteresis_percent"] / 100
            recovered = (
                value >= min(20, threshold * (1 + margin))
                if low
                else value <= threshold * (1 - margin)
            )
            if metric == "responsive":
                bad, recovered = value is False, value is True
            entry["bad"] = min(entry["bad"] + 1, 100000) if bad else 0
            entry["good"] = min(entry["good"] + 1, 100000) if recovered else 0
            if not entry["active"] and entry["bad"] >= cfg["alerts"]["consecutive"]:
                entry.update(active=True, epoch=entry["epoch"] + 1, good=0)
                events.append(
                    Event(
                        "alert",
                        metrics["at"],
                        {"metric": metric, "value": value, "threshold": threshold},
                    ).identify(f"alert:{metric}:{entry['epoch']}")
                )
            elif entry["active"] and entry["good"] >= cfg["alerts"]["recover_samples"]:
                entry.update(active=False, bad=0)
                if cfg["notifications"]["recovery"]:
                    events.append(
                        Event(
                            "recovery",
                            metrics["at"],
                            {"metric": metric, "value": value, "threshold": threshold},
                        ).identify(f"recover:{metric}:{entry['epoch']}")
                    )
        changes.update(metrics=metrics, alerts=alert_state)
        await self.emit(changes, events, cfg)

    async def run(self):
        while True:
            self.wake_event.clear()
            cfg = self.settings()
            if cfg["enabled"]:
                try:
                    await self.log_step(cfg)
                    if cfg["logs"]["enabled"]:
                        await self.source_health("logs", True, cfg)
                except Exception as exc:
                    self.log_error("log_poll_failed", exc)
                    try:
                        await self.source_health("logs", False, cfg)
                    except Exception:
                        self.log("state_write_failed")
                try:
                    await self.probe_step(cfg)
                except Exception as exc:
                    self.log_error("probe_poll_failed", exc)
                if time.time() - self.last_prune > 3600:
                    try:
                        await self.store.prune(cfg["retention_days"])
                        self.last_prune = time.time()
                    except Exception as exc:
                        self.log("prune_failed", error=type(exc).__name__)
            try:
                await asyncio.wait_for(self.wake_event.wait(), cfg["poll_seconds"])
            except asyncio.TimeoutError:
                pass

    def log_error(self, code, exc):
        if time.monotonic() - self.logged_errors.get(code, -1000) >= 60:
            self.logged_errors[code] = time.monotonic()
            self.log(code, error=type(exc).__name__)

    async def close(self):
        await self.docker.close()
