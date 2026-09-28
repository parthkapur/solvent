"""Ledger behaviour against a fake table client — no Azure, no network."""

import pytest
from azure.core.exceptions import ResourceExistsError

from app import nonces


class FakeTable:
    def __init__(self):
        self.rows = {}

    def create_entity(self, entity):
        if entity["RowKey"] in self.rows:
            raise ResourceExistsError("exists")
        self.rows[entity["RowKey"]] = entity

    def query_entities(self, query, **kw):
        cutoff = int(query.rsplit(" ", 1)[1])
        return [e for e in list(self.rows.values()) if e["ts"] < cutoff]

    def delete_entity(self, partition_key, row_key):
        self.rows.pop(row_key, None)


class RaisingPurgeTable(FakeTable):
    """A table whose cleanup step is broken - the write itself still succeeds."""

    def query_entities(self, query, **kw):
        raise RuntimeError("query failed")


class RaisingCreateTable(FakeTable):
    """A table that is down for the write itself, not just cleanup."""

    def create_entity(self, entity):
        raise RuntimeError("table unavailable")


def test_local_consumer_accepts_once_then_refuses(monkeypatch):
    consume = nonces.local_consumer()
    assert consume("sig-a", 100, 100.0, 600) is True
    assert consume("sig-a", 100, 100.0, 600) is False


def test_local_consumer_forgets_entries_no_token_could_hold(monkeypatch):
    consume = nonces.local_consumer()
    assert consume("sig-a", 100, 100.0, 600) is True
    # 700s later nothing minted at ts=100 can still be live, so the row is dropped and the
    # signature is accepted again - which is safe, because the TTL check already refused it.
    assert consume("sig-b", 800, 800.0, 600) is True
    assert consume("sig-a", 100, 800.0, 600) is True


def test_table_accepts_once_then_refuses(monkeypatch):
    table = FakeTable()
    monkeypatch.setattr(nonces, "_table", lambda: table)
    assert nonces.consume("sig-a", 100, 100.0, 600) is True
    assert nonces.consume("sig-a", 100, 100.0, 600) is False


def test_table_purges_expired_rows_on_write(monkeypatch):
    table = FakeTable()
    monkeypatch.setattr(nonces, "_table", lambda: table)
    nonces.consume("old", 100, 100.0, 600)
    nonces.consume("new", 900, 900.0, 600)
    assert "old" not in table.rows
    assert "new" in table.rows


def test_falls_back_to_process_memory_when_unconfigured(monkeypatch):
    monkeypatch.setattr(nonces, "_table", lambda: None)
    assert nonces.consume("sig-z", 100, 100.0, 600) is True
    assert nonces.consume("sig-z", 100, 100.0, 600) is False


def test_table_purge_is_capped_at_purge_per_write(monkeypatch):
    table = FakeTable()
    for i in range(nonces._PURGE_PER_WRITE + 5):
        table.rows[f"old-{i}"] = {"PartitionKey": "nonce", "RowKey": f"old-{i}", "ts": 100}
    monkeypatch.setattr(nonces, "_table", lambda: table)
    nonces.consume("new", 900, 900.0, 600)
    # 25 rows are expired; the cap lets only 20 go, so 5 survive alongside the fresh write.
    assert len(table.rows) == 5 + 1


def test_purge_failure_does_not_fail_a_valid_consume(monkeypatch):
    """The signature already proved unique; a broken cleanup step must not undo that."""
    table = RaisingPurgeTable()
    monkeypatch.setattr(nonces, "_table", lambda: table)
    assert nonces.consume("sig-a", 100, 100.0, 600) is True


def test_create_entity_failure_propagates_from_consume(monkeypatch):
    """decide() is the layer that turns this into a denial; consume() itself just raises."""
    table = RaisingCreateTable()
    monkeypatch.setattr(nonces, "_table", lambda: table)
    with pytest.raises(RuntimeError):
        nonces.consume("sig-a", 100, 100.0, 600)
