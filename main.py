"""AstrBot entrypoint. All MC/network work happens in cancellable background tasks."""

from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, MessageChain, filter
from astrbot.api.star import Context, Star
from astrbot.core.utils.astrbot_path import get_astrbot_data_path

from .mc_monitor.config import Settings, valid_umo
from .mc_monitor.engine import Engine
from .mc_monitor.models import Event, canonical, utc_now
from .mc_monitor.notifier import Composer, Notifier
from .mc_monitor.storage import Store


class MinecraftMonitor(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config
        self.cfg = None
        self.config_error = None
        self.tasks = []
        self.engine = None
        self.bound = []
        self.file_stamp = None
        self.last_injected = None
        self.config_changed = asyncio.Event()
        self.panel_test_seen = False
        self.action_lock = asyncio.Lock()
        self.store = Store(
            Path(get_astrbot_data_path())
            / "plugin_data"
            / "astrbot_plugin_mc_monitor"
            / "state.sqlite3"
        )

    @staticmethod
    def log(code, **fields):
        # Exclude exception messages, URLs, raw logs, recipients and configuration.
        logger.info(canonical({"plugin": "mc_monitor", "code": code, **fields}))

    def targets(self, cfg):
        bound = self.bound if cfg["notifications"]["use_bound_recipients"] else []
        return list(dict.fromkeys(cfg["notifications"]["recipients"] + bound))

    async def initialize(self):
        await self.store.initialize()
        self.bound = [x for x in await self.store.get("bound_recipients", []) if valid_umo(x)]
        self.panel_test_seen = await self.store.get("panel_test_seen", False)
        self.reload_config()
        if self.cfg is None:
            self.cfg = Settings.parse({"enabled": False})
        self.engine = Engine(
            self.store, lambda: self.cfg, self.targets, self.log, self.config_changed
        )
        composer = Composer(self.context, self.log)

        async def send(target, text):
            return await self.context.send_message(target, MessageChain().message(text))

        self.notifier = Notifier(
            self.store, lambda: self.cfg, self.targets, composer.compose, send, self.log
        )
        self.tasks = [
            asyncio.create_task(self.engine.run(), name="mc-monitor-poll"),
            asyncio.create_task(self.notifier.run("high"), name="mc-monitor-urgent"),
            asyncio.create_task(self.notifier.run("normal"), name="mc-monitor-normal"),
            asyncio.create_task(self.watch_config(), name="mc-monitor-config"),
        ]
        self.log("initialized", enabled=self.cfg["enabled"])

    def reload_config(self, raw=None):
        try:
            candidate = Settings.parse(dict(self.config) if raw is None else raw)
        except (ValueError, TypeError, KeyError) as exc:
            field = str(exc) if isinstance(exc, ValueError) else "configuration"
            if not all(c.isalnum() or c in "_." for c in field):
                field = "configuration"
            if field != self.config_error:
                self.log("config_invalid", field=field)
            self.config_error = field
            return False
        changed = self.cfg is None or candidate.data != self.cfg.data
        self.cfg, self.config_error = candidate, None
        if changed:
            self.config_changed.set()
            self.log("config_applied", enabled=candidate["enabled"])
        return True

    async def watch_config(self):
        while True:
            try:
                injected = dict(self.config)
                if injected != self.last_injected:
                    self.reload_config(injected)
                    self.last_injected = json.loads(json.dumps(injected))
                path = getattr(self.config, "config_path", None)
                if path:
                    result = await asyncio.to_thread(
                        self.read_config_file, Path(path), self.file_stamp
                    )
                    if result:
                        self.file_stamp, raw = result
                        self.reload_config(raw)
                await self.apply_config_actions()
            except Exception as exc:
                self.log("config_read_failed", error=type(exc).__name__)
            await asyncio.sleep(2)

    async def apply_config_actions(self):
        async with self.action_lock:
            await self._apply_config_actions()

    async def _apply_config_actions(self):
        requested = self.cfg["notifications"]["send_test_once"]
        if requested == self.panel_test_seen:
            return
        targets = self.targets(self.cfg)
        if requested and self.cfg["enabled"] and targets and self.cfg.policy("test")["enabled"]:
            notification = Event("test", utc_now(), {"source": "configuration_panel"}).identify(
                "panel-test:" + str(time.time_ns())
            )
            await self.store.ingest({"panel_test_seen": True}, [notification], targets, self.cfg)
            self.log("panel_test_queued", count=len(targets))
        else:
            await self.store.put("panel_test_seen", requested)
            if requested:
                self.log("panel_test_skipped_check_enabled_recipients_and_test_policy")
        self.panel_test_seen = requested

    @staticmethod
    def read_config_file(path, prior_stamp):
        stat = path.stat()
        stamp = (stat.st_mtime_ns, stat.st_size, stat.st_ino)
        if stamp == prior_stamp:
            return None
        if stat.st_size > 1048576:
            raise ValueError("configuration_too_large")
        raw = json.loads(path.read_text("utf-8-sig"))
        if not isinstance(raw, dict):
            raise ValueError("configuration")
        return stamp, raw

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("mc绑定")
    async def bind(self, event: AstrMessageEvent):
        """管理员在目标 QQ 私聊中登记当前会话。"""
        target = event.unified_msg_origin
        if event.get_platform_name() != "aiocqhttp" or not valid_umo(target):
            yield event.plain_result("请在 OneBot QQ 私聊中使用此命令。")
            return
        if target not in self.bound:
            updated = self.bound + [target]
            await self.store.put("bound_recipients", updated)
            self.bound = updated
        yield event.plain_result(
            "已绑定此私聊。请在插件配置中启用监控，使用 /mc测试 检查主动消息链路。\n当前 UMO："
            + target
        )

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("mc解绑")
    async def unbind(self, event: AstrMessageEvent):
        """移除当前会话的命令绑定；配置面板接收者需在面板中移除。"""
        target = event.unified_msg_origin
        if target in self.bound:
            updated = [x for x in self.bound if x != target]
            await self.store.put("bound_recipients", updated)
            self.bound = updated
        yield event.plain_result(
            "已移除当前会话的命令绑定；若仍在配置面板接收者列表中，请同时移除。"
        )

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("mc测试")
    async def test_message(self, event: AstrMessageEvent):
        """向已登记的私聊接收者排队发送测试消息（可能调用当前 LLM）。"""
        targets = self.targets(self.cfg)
        if not self.cfg["enabled"] or not targets:
            yield event.plain_result("请先启用监控，并绑定至少一个 QQ 私聊接收者。")
            return
        if not self.cfg.policy("test")["enabled"]:
            yield event.plain_result("测试事件通知已关闭，请先在配置面板开启。")
            return
        notification = Event("test", utc_now(), {"source": "administrator_command"}).identify(
            "test:" + str(time.time_ns())
        )
        await self.store.ingest({}, [notification], targets, self.cfg)
        yield event.plain_result(
            f"已为 {len(targets)} 个接收者排队测试消息；发送结果请查看私聊及结构化日志。"
        )

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("mc重载")
    async def reload(self, event: AstrMessageEvent):
        """重新校验当前配置；面板保存和插件重载也会生效。"""
        path = getattr(self.config, "config_path", None)
        try:
            result = (
                await asyncio.to_thread(self.read_config_file, Path(path), None) if path else None
            )
            if result:
                self.file_stamp, raw = result
                ok = self.reload_config(raw)
            else:
                ok = self.reload_config()
        except (OSError, ValueError):
            self.config_error = "configuration"
            ok = False
        yield event.plain_result(
            "配置已校验并生效。"
            if ok
            else "配置无效，已保留上一份有效配置。字段：" + str(self.config_error)
        )

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("mc状态")
    async def status(self, event: AstrMessageEvent):
        """查看最近采样及数据来源；不额外请求 MC 或 LLM。"""
        metrics = await self.store.get("metrics", {})
        names = {
            "tps": "TPS（1分钟）",
            "mspt": "MSPT（5秒平均）",
            "cpu": "容器 CPU%（100%=1核）",
            "memory": "容器内存%",
            "memory_bytes": "容器内存字节",
            "memory_limit_bytes": "容器内存上限字节",
            "online": "在线人数",
            "players": "在线列表",
            "responsive": "Java 状态响应",
        }
        text = [
            "MC 监控：" + ("启用" if self.cfg["enabled"] else "关闭"),
            "采样时间：" + metrics.get("at", "尚未采样"),
        ]
        for key, label in names.items():
            value = metrics.get(key)
            text.append(
                label
                + "："
                + ("不可用" if value is None else json.dumps(value, ensure_ascii=False))
            )
        text.extend(
            [
                "数据来源：" + json.dumps(metrics.get("sources", {}), ensure_ascii=False),
                "待发记录：" + str(await self.store.pending_count()),
                "私聊接收者数量：" + str(len(self.targets(self.cfg))),
            ]
        )
        if self.config_error:
            text.append("无效配置字段：" + self.config_error)
        yield event.plain_result("\n".join(text))

    async def terminate(self):
        for task in self.tasks:
            task.cancel()
        if self.tasks:
            await asyncio.gather(*self.tasks, return_exceptions=True)
        self.tasks = []
        if self.engine:
            await self.engine.close()
        self.log("terminated")
