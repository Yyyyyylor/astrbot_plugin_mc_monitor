from datetime import datetime

import pytest

from mc_monitor.parser import Parser

UUID = "12345678-1234-1234-1234-123456789abc"
MTIME = datetime.fromisoformat("2026-10-11T10:00:00+08:00").timestamp()


def line(message, time="10:00:00", level="INFO"):
    return f"[{time}] [Server thread/{level}]: {message}"


def test_join_leave_uuid_and_truthful_count():
    parser = Parser("Asia/Shanghai")
    parser.parse(line(f"UUID of player Alice is {UUID}"), "uuid", MTIME)
    event = parser.parse(line("Alice joined the game"), "join", MTIME)[0]
    assert event.facts["uuid"] == UUID
    assert event.facts["online"] is None
    assert parser.parse(line("Alice joined the game"), "duplicate", MTIME) == []
    parser.parse(line('Done (1.23s)! For help, type "help"'), "start", MTIME)
    event = parser.parse(line("Alice joined the game"), "join2", MTIME)[0]
    assert event.facts["online"] == 1
    assert parser.parse(line("Alice left the game"), "leave", MTIME)[0].facts["online"] == 0


@pytest.mark.parametrize(
    "text,kind",
    [
        (
            "Disconnecting Alice (/127.0.0.1:1234): You are not white-listed on this server!",
            "whitelist",
        ),
        ("Alice lost connection: You are not whitelisted on this server!", "whitelist"),
        ("Username 'Alice' tried to join with an invalid session", "auth"),
        ("Alice lost connection: Failed to verify username!", "auth"),
        ("Failed to verify username!", "auth"),
        ("Kicked Alice: reason", "kick"),
        ("Alice lost connection: Flying is not enabled on this server", "kick"),
        ("Banned Alice: reason", "ban"),
        ("Alice lost connection: You are banned from this server!", "ban"),
        ("Stopping server", "server_stop"),
        ("Encountered an unexpected exception", "crash"),
    ],
)
def test_server_events(text, kind):
    events = Parser("Asia/Shanghai").parse(line(text), "x", MTIME)
    assert events[0].kind == kind
    assert "127.0.0.1" not in str(events[0].facts)


@pytest.mark.parametrize(
    "text",
    [
        "<Alice> Stopping server",
        "<Alice> Banned Bob: stop monitoring",
        "[CHAT] Kicked Alice: foo",
        "[Not Secure] <Alice> Done (1.23s)!",
        "random plugin says Banned Alice: foo",
    ],
)
def test_chat_cannot_forge_events(text):
    assert Parser("Asia/Shanghai").parse(line(text), "x", MTIME) == []


def test_midnight_and_explicit_date():
    parser = Parser("Asia/Shanghai")
    before = parser.parse(line("Alice joined the game", "23:59:59"), "a", MTIME)[0]
    after = parser.parse(line("Alice left the game", "00:00:01"), "b", MTIME)[0]
    assert datetime.fromisoformat(after.at) > datetime.fromisoformat(before.at)
    event = parser.parse(line("Bob joined the game", "2026-10-12 02:00:00"), "c", MTIME)[0]
    assert event.at == "2026-10-12T02:00:00+08:00"
    assert event.facts["time_inferred"] is False


def test_crash_episode_and_unclean_restart():
    parser = Parser("Asia/Shanghai")
    ready = 'Done (1.23s)! For help, type "help"'
    parser.parse(line(ready), "a", MTIME)
    assert [e.kind for e in parser.parse(line(ready), "b", MTIME)] == ["server_start", "restart"]
    assert len(parser.parse(line("Encountered an unexpected exception"), "c", MTIME)) == 1
    assert parser.parse(line("This crash report has been saved to: /secret"), "d", MTIME) == []
    parser.parse(line(ready), "e", MTIME)
    assert len(parser.parse(line("Encountered an unexpected exception"), "f", MTIME)) == 1


def test_gameprofile_whitelist_uuid_and_nearby_duplicate():
    parser = Parser("Asia/Shanghai")
    text = (
        f"Disconnecting com.mojang.authlib.GameProfile@123[id={UUID},name=Alice,"
        "properties={}] (/127.0.0.1:1234): You are not white-listed on this server!"
    )
    event = parser.parse(line(text), "a", MTIME)[0]
    assert event.kind == "whitelist" and event.facts["uuid"] == UUID
    assert (
        parser.parse(
            line("Alice lost connection: You are not white-listed on this server!"), "b", MTIME
        )
        == []
    )


def test_malformed_timestamp_does_not_stall_batch():
    parser = Parser("Asia/Shanghai")
    assert parser.parse(line("Alice joined the game", "99:99:99"), "a", MTIME) == []
    assert parser.parse(line("Bob joined the game"), "b", MTIME)[0].facts["player"] == "Bob"
