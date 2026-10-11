"""Bounded asynchronous read-only probes. No shell or Docker control API."""

from __future__ import annotations

import asyncio
import json
import os
import re
import struct
from urllib.parse import quote

import aiohttp


class DockerProbe:
    def __init__(self):
        self.session = None

    async def snapshot(self, cfg, timeout):
        token = os.environ.get(cfg["secret_env"], "")
        if not token:
            raise ValueError("bridge_secret_missing")
        if self.session is None:
            self.session = aiohttp.ClientSession(trust_env=False)
        url = cfg["bridge_url"].rstrip("/") + "/snapshot/" + quote(cfg["container"], safe="")
        async with self.session.get(
            url,
            headers={"Authorization": "Bearer " + token},
            timeout=aiohttp.ClientTimeout(total=timeout),
            allow_redirects=False,
        ) as response:
            if response.status != 200:
                raise OSError("bridge_unavailable")
            body = bytearray()
            async for chunk in response.content.iter_chunked(65536):
                body.extend(chunk)
                if len(body) > 262144:
                    raise ValueError("bridge_response_too_large")
            result = json.loads(body)
            if not isinstance(result, dict) or result.get("container") != cfg["container"]:
                raise ValueError("bridge_invalid_response")
            return result

    async def close(self):
        if self.session:
            await self.session.close()
            self.session = None


def varint(value):
    value &= 0xFFFFFFFF
    output = bytearray()
    while True:
        byte = value & 0x7F
        value >>= 7
        output.append(byte | (0x80 if value else 0))
        if not value:
            return bytes(output)


async def read_varint(reader):
    result = 0
    for i in range(5):
        byte = (await reader.readexactly(1))[0]
        result |= (byte & 0x7F) << (i * 7)
        if not byte & 0x80:
            return result
    raise ValueError("invalid_varint")


async def close_writer(writer):
    writer.close()
    try:
        await asyncio.wait_for(writer.wait_closed(), 1)
    except (TimeoutError, OSError):
        pass


async def java_status(cfg, timeout):
    async def request():
        reader, writer = await asyncio.open_connection(cfg["host"], cfg["port"])
        try:
            host = cfg["host"].encode("utf-8")
            if len(host) > 255:
                raise ValueError("invalid_host_length")
            hello = (
                b"\x00"
                + varint(-1)
                + varint(len(host))
                + host
                + struct.pack(">H", cfg["port"])
                + b"\x01"
            )
            writer.write(varint(len(hello)) + hello + b"\x01\x00")
            await writer.drain()
            size = await read_varint(reader)
            if size <= 0 or size > 262144:
                raise ValueError("status_packet_size")
            packet = await reader.readexactly(size)
            # Parse packet ID and JSON length with a bounded in-memory reader.
            data = asyncio.StreamReader()
            data.feed_data(packet)
            data.feed_eof()
            if await read_varint(data) != 0:
                raise ValueError("status_packet_id")
            length = await read_varint(data)
            if length > size:
                raise ValueError("status_json_length")
            result = json.loads((await data.readexactly(length)).decode("utf-8"))
            count = result.get("players", {}).get("online")
            return {
                "responsive": True,
                "online": count if type(count) is int and count >= 0 else None,
            }
        finally:
            await close_writer(writer)

    return await asyncio.wait_for(request(), timeout)


class Rcon:
    """Source RCON framing with a Minecraft-compatible empty command end marker."""

    ALLOWED = frozenset(("list", "tps", "mspt"))

    async def query(self, cfg, timeout):
        password = os.environ.get(cfg["secret_env"], "")
        if not password:
            raise ValueError("rcon_secret_missing")

        async def request():
            reader, writer = await asyncio.open_connection(cfg["host"], cfg["port"])
            try:
                await self.send(writer, 1, 3, password)
                for _ in range(2):
                    ident, typ, _ = await self.receive(reader)
                    if ident == -1:
                        raise PermissionError("rcon_auth_failed")
                    if ident == 1 and typ == 2:
                        break
                else:
                    raise ValueError("rcon_auth_protocol")
                commands = ["list"] + (
                    ["tps", "mspt"] if cfg["performance_source"] == "paper" else []
                )
                output = {}
                for i, command in enumerate(commands, 10):
                    if command not in self.ALLOWED:
                        raise ValueError("rcon_command_forbidden")
                    await self.send(writer, i, 2, command)
                    # Minecraft replies to an empty command; its ID ends the fragmented reply.
                    await self.send(writer, i + 100, 2, "")
                    parts = []
                    for _ in range(64):
                        ident, typ, text = await self.receive(reader)
                        if ident == i + 100:
                            break
                        if ident != i or typ != 0:
                            raise ValueError("rcon_response_mismatch")
                        parts.append(text)
                    else:
                        raise ValueError("rcon_response_limit")
                    output[command] = re.sub(r"\x1b\[[0-9;]*m|§.", "", "".join(parts))
                return parse_rcon(output)
            finally:
                await close_writer(writer)

        return await asyncio.wait_for(request(), timeout)

    @staticmethod
    async def send(writer, ident, typ, text):
        payload = struct.pack("<ii", ident, typ) + text.encode("utf-8") + b"\0\0"
        writer.write(struct.pack("<i", len(payload)) + payload)
        await writer.drain()

    @staticmethod
    async def receive(reader):
        size = struct.unpack("<i", await reader.readexactly(4))[0]
        if not 10 <= size <= 8192:
            raise ValueError("rcon_packet_size")
        payload = await reader.readexactly(size)
        if payload[-2:] != b"\0\0":
            raise ValueError("rcon_packet_terminator")
        ident, typ = struct.unpack("<ii", payload[:8])
        return ident, typ, payload[8:-2].decode("utf-8", errors="replace")


def parse_rcon(output):
    result = {"players": None, "online": None, "tps": None, "mspt": None}
    match = re.fullmatch(
        r"There are (\d+) of a max of \d+ players online:\s*(.*)", output.get("list", "").strip()
    )
    if match:
        names = [x.strip() for x in match[2].split(",") if x.strip()]
        if all(re.fullmatch(r"[A-Za-z0-9_]{1,16}", x) for x in names) and len(names) == int(
            match[1]
        ):
            result.update(players=names, online=int(match[1]))
    tps = re.search(r"TPS from last 1m, 5m, 15m:\s*\*?([\d.]+)", output.get("tps", ""))
    if tps:
        value = float(tps[1])
        result["tps"] = value if 0 <= value <= 20.1 else None
    # Paper prints: Server tick times (avg/min/max) from last 5s, 10s, 1m:
    # 12.3/1.2/45.6, ... . Only interpret the explicitly labelled avg column.
    mspt = re.search(
        r"Server tick times \(avg/min/max\) from last 5s, 10s, 1m:\s*([\d.]+)/[\d.]+/[\d.]+",
        output.get("mspt", ""),
    )
    if mspt:
        value = float(mspt[1])
        result["mspt"] = value if 0 <= value <= 100000 else None
    return result
