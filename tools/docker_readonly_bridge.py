#!/usr/bin/env python3
"""Host-side fixed-container Docker GET facade. Never put the socket in AstrBot."""

from __future__ import annotations

import argparse
import asyncio
import hmac
import json
import os
import re
from pathlib import Path

import aiohttp
from aiohttp import web


def resource_metrics(stats):
    # Standalone bridge is deployable without copying the whole AstrBot plugin.
    memory = stats.get("memory_stats", {})
    usage, limit = memory.get("usage"), memory.get("limit")
    details = memory.get("stats", {})
    inactive = details.get("inactive_file", details.get("total_inactive_file", 0))
    used = max(0, usage - inactive) if isinstance(usage, (int, float)) else None
    cpu, prev = stats.get("cpu_stats", {}), stats.get("precpu_stats", {})
    delta = cpu.get("cpu_usage", {}).get("total_usage", 0) - prev.get("cpu_usage", {}).get(
        "total_usage", 0
    )
    system = cpu.get("system_cpu_usage", 0) - prev.get("system_cpu_usage", 0)
    cores = cpu.get("online_cpus") or len(cpu.get("cpu_usage", {}).get("percpu_usage", []))
    valid_cpu = all(
        key in values
        for values, key in (
            (cpu, "system_cpu_usage"),
            (prev, "system_cpu_usage"),
            (cpu.get("cpu_usage", {}), "total_usage"),
            (prev.get("cpu_usage", {}), "total_usage"),
        )
    )
    return {
        "cpu": delta / system * cores * 100
        if valid_cpu and system > 0 and delta >= 0 and cores
        else None,
        "memory": used / limit * 100 if used is not None and limit else None,
        "memory_bytes": used,
        "memory_limit_bytes": limit or None,
    }


def create_app(container, token, socket="/var/run/docker.sock", session_factory=None):
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}", container):
        raise ValueError("invalid_container_name")
    if len(token) < 32:
        raise ValueError("token_must_have_at_least_32_characters")
    app = web.Application(client_max_size=1024)
    session_key = web.AppKey("docker_session", aiohttp.ClientSession)

    async def lifecycle(app):
        session = (
            session_factory()
            if session_factory
            else aiohttp.ClientSession(
                connector=aiohttp.UnixConnector(path=socket),
                timeout=aiohttp.ClientTimeout(total=4),
                trust_env=False,
            )
        )
        app[session_key] = session
        yield
        await session.close()

    app.cleanup_ctx.append(lifecycle)

    async def get_docker(path):
        async with app[session_key].get(
            "http://docker/v1.41" + path, allow_redirects=False
        ) as response:
            if response.status == 404:
                return None
            if response.status != 200:
                raise OSError("docker_get_failed")
            body = bytearray()
            async for chunk in response.content.iter_chunked(65536):
                body.extend(chunk)
                if len(body) > 2097152:
                    raise ValueError("docker_reply_too_large")
            return json.loads(body)

    async def snapshot(request):
        supplied = request.headers.get("Authorization", "")
        if not hmac.compare_digest(supplied.encode(), ("Bearer " + token).encode()):
            raise web.HTTPUnauthorized(text="unauthorized")
        if request.match_info["container"] != container or request.query_string:
            raise web.HTTPForbidden(text="forbidden")
        try:
            inspect = await get_docker("/containers/" + container + "/json")
            if inspect is None:
                return web.json_response(
                    {"container": container, "state": {"status": "missing"}, "metrics": {}}
                )
            raw = inspect.get("State", {})
            state = {
                "id": inspect.get("Id"),
                "status": raw.get("Status"),
                "started_at": raw.get("StartedAt"),
                "finished_at": raw.get("FinishedAt"),
                "exit_code": raw.get("ExitCode"),
                "oom_killed": raw.get("OOMKilled"),
                "restart_count": inspect.get("RestartCount"),
            }
            metrics = {}
            if state["status"] == "running":
                stats = await get_docker("/containers/" + container + "/stats?stream=false")
                if stats:
                    metrics = resource_metrics(stats)
            return web.json_response({"container": container, "state": state, "metrics": metrics})
        except (OSError, ValueError, aiohttp.ClientError, asyncio.TimeoutError):
            # Do not expose daemon errors, host paths, env, labels or passwords.
            return web.json_response({"error": "docker_unavailable"}, status=503)

    app.router.add_get("/snapshot/{container}", snapshot, allow_head=False)
    return app


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--container", required=True)
    parser.add_argument("--listen", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--socket", default="/var/run/docker.sock")
    parser.add_argument("--token-file", type=Path, required=True)
    args = parser.parse_args()
    if os.name != "nt" and args.token_file.stat().st_mode & 0o077:
        parser.error("token file must not be readable by group/others (chmod 600)")
    token = args.token_file.read_text("utf-8").strip()
    # No access logs: their request paths and credentials are unnecessary for monitoring.
    web.run_app(
        create_app(args.container, token, args.socket),
        host=args.listen,
        port=args.port,
        access_log=None,
        print=None,
    )


if __name__ == "__main__":
    main()
