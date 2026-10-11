"""Recognize server messages only; chat text is deliberately excluded."""

from __future__ import annotations

import re
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from .models import Event

NAME = r"[A-Za-z0-9_]{1,16}"
UUID = r"[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}"
PREFIX = re.compile(
    r"^\[(?:(?P<date>\d{4}-\d{2}-\d{2})[ T])?(?P<time>\d{2}:\d{2}:\d{2})(?:[.,]\d+)?\] "
    r"\[(?P<thread>[^\]\r\n]{1,100})/(?P<level>INFO|WARN|ERROR|FATAL)\](?: \[[^\]]+\])?: "
    r"(?P<message>.*)$"
)


class Parser:
    def __init__(self, timezone, state=None):
        self.tz = ZoneInfo(timezone)
        self.state = state or {"uuids": {}, "players": [], "complete": False, "running": None}

    def parse(self, line, origin, mtime):
        match = PREFIX.match(line)
        if not match:
            return []
        msg = match["message"]
        if msg.startswith(("<", "[Not Secure] <", "[CHAT]", "[Async Chat")):
            return []
        reference = datetime.fromtimestamp(mtime, self.tz)
        if match["date"]:
            try:
                at = datetime.fromisoformat(match["date"] + "T" + match["time"]).replace(
                    tzinfo=self.tz
                )
            except ValueError:
                return []
        else:
            previous = self.state.get("last_at")
            reference = (
                datetime.fromisoformat(previous).astimezone(self.tz) if previous else reference
            )
            try:
                at = reference.replace(
                    hour=int(match["time"][:2]),
                    minute=int(match["time"][3:5]),
                    second=int(match["time"][6:]),
                    microsecond=0,
                )
            except ValueError:
                return []
            # For a retained log use nearest file date. Consecutive lines cross midnight forward.
            if previous:
                if at < reference - timedelta(hours=12):
                    at += timedelta(days=1)
            else:
                at = min(
                    (at - timedelta(days=1), at, at + timedelta(days=1)),
                    key=lambda x: abs((x - reference).total_seconds()),
                )
        self.state["last_at"] = at.isoformat()
        stamp = at.isoformat(timespec="seconds")

        def event(kind, facts=None):
            return Event(
                kind, stamp, {"time_inferred": not bool(match["date"]), **(facts or {})}
            ).identify(origin + ":" + kind)

        uuids = self.state.setdefault("uuids", {})
        players = set(self.state.setdefault("players", []))
        uuid = re.fullmatch(rf"UUID of player ({NAME}) is ({UUID})", msg)
        if uuid:
            if len(uuids) >= 4096 and uuid[1] not in uuids:
                del uuids[next(iter(uuids))]
            uuids[uuid[1]] = uuid[2].lower()
            return []
        joined = re.fullmatch(rf"({NAME}) joined the game", msg)
        left = re.fullmatch(rf"({NAME}) left the game", msg)
        if joined or left:
            name = (joined or left)[1]
            if (joined and name in players) or (
                left and name not in players and self.state.get("complete")
            ):
                return []
            players.add(name) if joined else players.discard(name)
            self.state["players"] = sorted(players)
            return [
                event(
                    "join" if joined else "quit",
                    {
                        "player": name,
                        "uuid": uuids.get(name),
                        "online": len(players) if self.state.get("complete") else None,
                        "observed_online": len(players),
                    },
                )
            ]
        if re.match(r"^Done \([\d.,]+s\)!", msg):
            was_running = self.state.get("running")
            self.state.update(
                running=True, complete=True, players=[], crashed=False, last_start_at=stamp
            )
            events = [event("server_start")]
            if was_running:
                events.append(
                    event("restart", {"basis": "ready_without_observed_stop", "inferred": True})
                )
            return events
        if msg == "Stopping server":
            self.state.update(running=False, complete=True, players=[], last_stop_at=stamp)
            return [event("server_stop")]
        if (
            msg.startswith("Encountered an unexpected exception")
            or msg.startswith("This crash report has been saved to:")
            or "A single server tick took" in msg
            and match["level"] in ("ERROR", "FATAL")
            or msg.startswith("Considering it to be crashed, server will forcibly shutdown.")
        ):
            if self.state.get("crashed"):
                return []
            self.state.update(crashed=True, running=False, complete=False, players=[])
            return [event("crash", {"basis": "server_crash_log"})]
        # Exact server prefixes only. Never store arbitrary reasons/IPs/chat in an event.
        banned = re.match(rf"Banned ({NAME})(?::|\s|$)", msg)
        if banned:
            return [
                event(
                    "ban",
                    {"player": banned[1], "uuid": uuids.get(banned[1]), "reason": "ban_command"},
                )
            ]
        invalid = re.fullmatch(rf"Username '({NAME})' tried to join with an invalid session", msg)
        if invalid:
            return [
                event(
                    "auth",
                    {
                        "player": invalid[1],
                        "uuid": uuids.get(invalid[1]),
                        "reason": "invalid_session",
                    },
                )
            ]
        disconnect = re.match(
            rf"(?:Disconnecting )?({NAME})(?: \([^\r\n]{{1,300}}\))?: "
            r"(?P<reason>.+)$",
            msg,
        )
        lost = re.match(rf"({NAME}) lost connection: (?P<reason>.+)$", msg)
        profile = None
        if msg.startswith("Disconnecting com.mojang.authlib.GameProfile"):
            profile = re.search(rf"name=({NAME}),.*\)?: (?P<reason>[^\r\n]+)$", msg)
            profile_uuid = re.search(rf"id=({UUID})", msg)
            if profile and profile_uuid:
                uuids[profile[1]] = profile_uuid[1].lower()
        selected = disconnect or lost or profile
        if selected:
            reason = selected["reason"].lower()
            kind, code = None, None
            if "whitelist" in reason or "white-listed" in reason or "white listed" in reason:
                kind, code = "whitelist", "not_whitelisted"
            elif "banned" in reason:
                kind, code = "ban", "banned_login"
            elif any(
                x in reason
                for x in (
                    "verify username",
                    "invalid session",
                    "authentication",
                    "unverified_username",
                )
            ):
                kind, code = "auth", "authentication_failed"
            elif any(
                x in reason
                for x in ("kicked", "flying is not enabled", "multiplayer.disconnect.kicked")
            ):
                kind, code = "kick", "server_kick"
            if kind:
                name = selected[1]
                key = f"{kind}:{name}:{code}"
                prior = self.state.get("last_security", {})
                if (
                    prior.get("key") == key
                    and abs((at - datetime.fromisoformat(prior["at"])).total_seconds()) <= 2
                ):
                    return []
                self.state["last_security"] = {"key": key, "at": stamp}
                return [event(kind, {"player": name, "uuid": uuids.get(name), "reason": code})]
        kicked = re.match(rf"Kicked ({NAME})(?::|\s|$)", msg)
        if kicked:
            return [
                event(
                    "kick",
                    {"player": kicked[1], "uuid": uuids.get(kicked[1]), "reason": "kick_command"},
                )
            ]
        if msg.startswith("Failed to verify username!") or msg.startswith(
            "Authentication servers are down"
        ):
            return [
                event(
                    "auth", {"player": None, "uuid": None, "reason": "verification_service_failed"}
                )
            ]
        return []
