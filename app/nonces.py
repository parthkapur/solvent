"""Single-use bookkeeping for approval signatures.

An Azure Table when one is configured, so a token stays spent across a restart or a second
replica; the process dict otherwise, which is what tests and local development use.
"""

import itertools
import os
from collections.abc import Callable

from azure.core.exceptions import ResourceExistsError

_PARTITION = "nonce"
_PURGE_PER_WRITE = 20
_table_client = None
_PROCESS: dict[str, int] = {}


def _table():
    """The configured table, or None. Built once; the credential chain is not cheap."""
    global _table_client
    endpoint = os.environ.get("NONCE_TABLE_ENDPOINT", "")
    name = os.environ.get("NONCE_TABLE_NAME", "")
    if not (endpoint and name):
        return None
    if _table_client is None:
        from azure.data.tables import TableClient

        from app.azure_clients import _cred

        _table_client = TableClient(endpoint=endpoint, table_name=name, credential=_cred())
    return _table_client


def _consume_in(store: dict[str, int], sig: str, ts: int, now: float, max_ttl: int) -> bool:
    """True the first time a signature is seen. Drops entries no live token could still hold."""
    for s, t in list(store.items()):
        if t < now - max_ttl:
            del store[s]
    if sig in store:
        return False
    store[sig] = ts
    return True


def local_consumer(store: dict[str, int] | None = None) -> Callable[[str, int, float, int], bool]:
    """A ledger of its own, for tests that want isolation from every other test.

    Takes a store when the caller needs to look inside it afterwards.
    """
    store = {} if store is None else store
    return lambda sig, ts, now, max_ttl: _consume_in(store, sig, ts, now, max_ttl)


def consume(sig: str, ts: int, now: float, max_ttl: int) -> bool:
    table = _table()
    if table is None:
        return _consume_in(_PROCESS, sig, ts, now, max_ttl)
    try:
        table.create_entity({"PartitionKey": _PARTITION, "RowKey": sig, "ts": ts})
    except ResourceExistsError:
        return False
    # ponytail: purge on the write path, bounded per call, because Azure Tables has no TTL. A
    # timer-triggered sweeper is the upgrade if writes ever outpace the bound.
    #
    # Best-effort: the signature above already proved unique, so a failure here (a transient
    # query/delete error) must never turn a good approval into a hard denial - it just leaves
    # a row for the next successful write, or the eventual sweeper, to clean up.
    try:
        cutoff = int(now - max_ttl)
        expired = table.query_entities(f"PartitionKey eq '{_PARTITION}' and ts lt {cutoff}")
        for row in itertools.islice(expired, _PURGE_PER_WRITE):
            table.delete_entity(row["PartitionKey"], row["RowKey"])
    except Exception:  # noqa: BLE001, S110 - cleanup failing must not fail the approval it follows
        pass
    return True
