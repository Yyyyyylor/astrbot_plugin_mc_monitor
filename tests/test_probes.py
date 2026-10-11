import asyncio
import json
from contextlib import asynccontextmanager

import aiohttp
import pytest
from aiohttp import web

from mc_monitor.probes import DockerProbe, Rcon, java_status, parse_rcon, read_varint, varint
from tools.docker_readonly_bridge import create_app, resource_metrics


@asynccontextmanager
async def tcp_server(handler):
    server = await asyncio.start_server(handler, "127.0.0.1", 0)
    try:
        yield server.sockets[0].getsockname()[1]
    finally:
        server.close()
        await server.wait_closed()


@asynccontextmanager
async def http_server(app):
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    try:
        yield "http://127.0.0.1:" + str(site._server.sockets[0].getsockname()[1])
    finally:
        await runner.cleanup()


async def test_java_status_real_protocol_and_disconnect_recovery():
    fail = True

    async def handler(reader, writer):
        try:
            if fail:
                return
            await reader.readexactly(await read_varint(reader))
            assert await reader.readexactly(2) == b"\x01\x00"
            body = json.dumps({"players": {"online": 2}}).encode()
            packet = b"\0" + varint(len(body)) + body
            writer.write(varint(len(packet)) + packet)
            await writer.drain()
        finally:
            writer.close()
            await writer.wait_closed()

    async with tcp_server(handler) as port:
        cfg = {"host": "127.0.0.1", "port": port}
        with pytest.raises((OSError, asyncio.IncompleteReadError)):
            await java_status(cfg, 1)
        fail = False
        assert await java_status(cfg, 1) == {"responsive": True, "online": 2}


async def test_rcon_auth_fragmentation_marker_and_readonly(monkeypatch):
    monkeypatch.setenv("TEST_RCON_PASSWORD", "not-a-real-secret")
    commands = []

    async def handler(reader, writer):
        try:
            ident, typ, password = await Rcon.receive(reader)
            assert (typ, password) == (3, "not-a-real-secret")
            await Rcon.send(writer, ident, 2, "")
            while True:
                ident, typ, command = await Rcon.receive(reader)
                if not command:
                    await Rcon.send(writer, ident, 0, "")
                    continue
                commands.append(command)
                answer = {
                    "list": "There are 2 of a max of 20 players online: Alice, Bob",
                    "tps": "TPS from last 1m, 5m, 15m: *19.9, 20.0, 20.0",
                    "mspt": (
                        "Server tick times (avg/min/max) from last 5s, 10s, 1m:\n"
                        "12.3/1.0/40.0, 13.0/2.0/40.0"
                    ),
                }[command]
                midpoint = len(answer) // 2
                await Rcon.send(writer, ident, 0, answer[:midpoint])
                await Rcon.send(writer, ident, 0, answer[midpoint:])
        except asyncio.IncompleteReadError:
            pass
        finally:
            writer.close()
            await writer.wait_closed()

    async with tcp_server(handler) as port:
        cfg = {
            "host": "127.0.0.1",
            "port": port,
            "secret_env": "TEST_RCON_PASSWORD",
            "performance_source": "paper",
        }
        result = await Rcon().query(cfg, 2)
    assert commands == ["list", "tps", "mspt"]
    assert result == {"players": ["Alice", "Bob"], "online": 2, "tps": 19.9, "mspt": 12.3}


async def test_rcon_auth_failure(monkeypatch):
    monkeypatch.setenv("TEST_RCON_PASSWORD", "wrong")

    async def handler(reader, writer):
        await Rcon.receive(reader)
        await Rcon.send(writer, -1, 2, "")
        writer.close()
        await writer.wait_closed()

    async with tcp_server(handler) as port:
        with pytest.raises(PermissionError):
            await Rcon().query(
                {
                    "host": "127.0.0.1",
                    "port": port,
                    "secret_env": "TEST_RCON_PASSWORD",
                    "performance_source": "paper",
                },
                1,
            )


def test_unknown_rcon_metrics_are_unavailable():
    result = parse_rcon({"list": "unknown format", "tps": "Unknown command: tps", "mspt": "42"})
    assert all(x is None for x in result.values())


def test_cpu_without_previous_sample_is_unavailable():
    metrics = resource_metrics(
        {
            "cpu_stats": {
                "cpu_usage": {"total_usage": 200},
                "system_cpu_usage": 1000,
                "online_cpus": 4,
            }
        }
    )
    assert metrics["cpu"] is None
    assert metrics["memory"] is None


async def test_bridge_limits_container_methods_secrets_and_recovers(monkeypatch):
    requests = []
    broken = False

    async def daemon(request):
        requests.append((request.method, request.path))
        if broken:
            return web.Response(status=500, text="secret daemon error")
        if request.path.endswith("/json"):
            return web.json_response(
                {
                    "Id": "container-id",
                    "Config": {"Env": ["SECRET=private"]},
                    "State": {"Status": "running", "ExitCode": 0, "OOMKilled": False},
                    "RestartCount": 0,
                }
            )
        return web.json_response(
            {
                "memory_stats": {"usage": 1000, "limit": 2000, "stats": {"inactive_file": 100}},
                "cpu_stats": {
                    "cpu_usage": {"total_usage": 200},
                    "system_cpu_usage": 1000,
                    "online_cpus": 4,
                },
                "precpu_stats": {"cpu_usage": {"total_usage": 100}, "system_cpu_usage": 800},
            }
        )

    daemon_app = web.Application()
    daemon_app.router.add_get("/v1.41/containers/minecraft/{method}", daemon)
    token = "x" * 40
    monkeypatch.setenv("TEST_BRIDGE_TOKEN", token)
    async with http_server(daemon_app) as upstream:

        class LocalDaemonSession:
            def __init__(self):
                self.session = aiohttp.ClientSession()

            def get(self, url, **kwargs):
                return self.session.get(url.replace("http://docker", upstream), **kwargs)

            async def close(self):
                await self.session.close()

        bridge = create_app("minecraft", token, session_factory=LocalDaemonSession)
        async with http_server(bridge) as url:
            headers = {"Authorization": "Bearer " + token}
            async with aiohttp.ClientSession() as client:
                for path, method, status in [
                    ("/snapshot/minecraft", "GET", 401),
                    ("/snapshot/astrbot", "GET", 403),
                    ("/snapshot/minecraft", "POST", 405),
                ]:
                    async with client.request(
                        method, url + path, headers={} if status == 401 else headers
                    ) as response:
                        assert response.status == status
            probe = DockerProbe()
            cfg = {"container": "minecraft", "bridge_url": url, "secret_env": "TEST_BRIDGE_TOKEN"}
            try:
                result = await probe.snapshot(cfg, 2)
                assert "SECRET" not in json.dumps(result)
                assert result["metrics"]["cpu"] == 200
                assert result["metrics"]["memory"] == 45
                broken = True
                with pytest.raises(OSError):
                    await probe.snapshot(cfg, 2)
                broken = False
                assert (await probe.snapshot(cfg, 2))["state"]["status"] == "running"
            finally:
                await probe.close()
    assert all(method == "GET" for method, _ in requests)
    assert all("/containers/minecraft/" in path for _, path in requests)
