import pytest

from mc_monitor.config import KINDS, Settings, defaults


def test_schema_covers_all_events_and_defaults_safe():
    cfg = Settings.parse({})
    assert cfg["enabled"] is False
    assert set(defaults()["events"]) == set(KINDS)
    assert cfg["docker"]["enabled"] is False
    assert cfg["rcon"]["enabled"] is False


@pytest.mark.parametrize(
    "data",
    [
        {"poll_seconds": 0},
        {"poll_seconds": float("nan")},
        {"enabled": "true"},
        {"rcon": {"port": 70000}},
        {"timezone": "does/not/exist"},
        {"notifications": {"recipients": ["bot:GroupMessage:123"]}},
        {"notifications": {"quiet_start": "25:00"}},
        {"docker": {"enabled": True, "bridge_url": "http://user:password@host"}},
        {"rcon": {"secret_env": "the-password-itself!"}},
        {"logs": {"max_bytes": 4096, "max_line_bytes": 16384}},
        {"notifications": {"retry_base": 100, "retry_max": 10}},
        {"events": {"join": {"level": "panic"}}},
        [],
    ],
)
def test_invalid_config_rejected_without_echoing_values(data):
    with pytest.raises(ValueError) as exc:
        Settings.parse(data)
    assert "password" not in str(exc.value)


def test_defaults_are_not_mutated_between_instances():
    one = Settings.parse({})
    one["events"]["join"]["enabled"] = False
    assert Settings.parse({})["events"]["join"]["enabled"] is True
