import pytest

from mc_monitor.config import Settings
from mc_monitor.storage import Store

TARGET = "qq-main:FriendMessage:10001"


@pytest.fixture
def cfg():
    return Settings.parse(
        {
            "enabled": True,
            "logs": {"enabled": False},
            "llm": {"enabled": False},
            "notifications": {"recipients": [TARGET], "merge_seconds": 0},
        }
    )


@pytest.fixture
async def store(tmp_path):
    store = Store(tmp_path / "state.sqlite3")
    await store.initialize()
    return store


def no_log(*args, **kwargs):
    pass
