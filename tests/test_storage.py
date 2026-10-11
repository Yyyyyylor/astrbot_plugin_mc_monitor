import sqlite3

import pytest


@pytest.mark.parametrize("fail", [False, True])
async def test_transaction_closes_connection_and_preserves_commit_semantics(
    store, monkeypatch, fail
):
    connections = []

    def tracked_open():
        # Allow the test thread to check closure after the worker finishes.
        # The application still uses sqlite3's default thread affinity.
        db = sqlite3.connect(store.path, check_same_thread=False)
        connections.append(db)
        return db

    monkeypatch.setattr(store, "_open", tracked_open)

    def write(db):
        db.execute("INSERT INTO kv VALUES('transaction_probe', '42')")
        if fail:
            raise RuntimeError("simulated_transaction_failure")

    if fail:
        with pytest.raises(RuntimeError, match="simulated_transaction_failure"):
            await store.run(write)
    else:
        await store.run(write)

    assert await store.get("transaction_probe") == (None if fail else 42)
    assert len(connections) == 2
    for connection in connections:
        with pytest.raises(sqlite3.ProgrammingError, match="closed database"):
            connection.execute("SELECT 1")
