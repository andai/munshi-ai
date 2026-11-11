"""Every sealed column andai stores, and the jobs that walk them for a key rotation.

A sealed column holds a value from `Keyring.seal` (`crypto.py`) next to the `key_id` of the KEK
that wrapped it. `SEALED_COLUMNS` lists them all; `test_sealed_registry.py` fails when a table gains
a `*_ciphertext` column with a sibling `key_id` that is not listed here. Every new sealed column
must be added (the Phase 8 provider keys and the 10-05 export bundles will be).

* `rewrap_all` moves every value that is not under the active KEK onto it with
  `Keyring.rewrap`: only the wrapped data key changes, the encrypted body is kept and the secret is
  never decrypted. Each row is written by compare-and-set on its old ciphertext and key id, so a
  value re-sealed meanwhile (a TOTP re-enrolment, say) is left alone and counted as `skipped`.
  Batches commit one by one, so a run that stops half way loses nothing and a second run picks up
  only what is left: the job is idempotent.
* `counts_by_key` counts the values each KEK still wraps, per table: the runbook retires a key only
  when it wraps nothing (`python -m andai_core kek-status`, `munshi_sealed_values`).

Global tables (no row-level security) are read directly. Org-scoped tables are visited one org at a
time, each in a transaction with that org's `app.org_id`; the orgs are those
`andai_principal_org_ids()` (migration 0003) lists, and every org gets its principal when it is
created. An org-scoped sealed table whose rows can belong to an org without a principal needs its
own org listing before it is added here.

Nothing here logs or returns key material, ciphertext or plaintext: only table names, key ids and
counts.
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Iterator
from dataclasses import dataclass

from sqlalchemy import Engine, text

from andai_core.crypto import DecryptionError, Keyring, KeyUnavailable
from andai_core.db.session import transaction
from andai_core.platform.principals import org_ids_with_principal

log = logging.getLogger(__name__)

DEFAULT_BATCH = 100


@dataclass(frozen=True)
class SealedColumn:
    table: str
    pk: tuple[str, ...]
    ciphertext: str
    key_id: str
    org_scoped: bool


SEALED_COLUMNS: tuple[SealedColumn, ...] = (
    SealedColumn("user_totp", ("user_id",), "secret_ciphertext", "key_id", org_scoped=False),
    SealedColumn(
        "platform_principals", ("org_id",), "key_ciphertext", "key_id", org_scoped=True
    ),
)


@dataclass
class RewrapCount:
    """Per table: values moved to the active KEK (in a dry run: that would be), values changed by
    someone else between read and write (`skipped`), and values that cannot be re-wrapped
    (`failed`: their KEK is not loaded, or the value is damaged)."""

    rewrapped: int = 0
    skipped: int = 0
    failed: int = 0


def _scopes(engine: Engine, column: SealedColumn) -> Iterator[uuid.UUID | None]:
    """`None` for a global table; each org's id for an org-scoped one."""
    if not column.org_scoped:
        yield None
        return
    yield from org_ids_with_principal(engine)


def _batch_sql(column: SealedColumn, after: bool) -> str:
    pk = ", ".join(column.pk)
    where = f"{column.key_id} <> :active"
    if after:
        where += f" AND ({pk}) > ({', '.join(f':after_{i}' for i in range(len(column.pk)))})"
    return (
        f"SELECT {pk}, {column.ciphertext}, {column.key_id} FROM {column.table}"
        f" WHERE {where} ORDER BY {pk} LIMIT :batch"
    )


def _update_sql(column: SealedColumn) -> str:
    match = " AND ".join(f"{name} = :pk_{i}" for i, name in enumerate(column.pk))
    return (
        f"UPDATE {column.table} SET {column.ciphertext} = :new_ct, {column.key_id} = :active"
        f" WHERE {match} AND {column.key_id} = :old_key AND {column.ciphertext} = :old_ct"
    )


def _rewrap_scope(
    engine: Engine,
    keyring: Keyring,
    column: SealedColumn,
    org_id: uuid.UUID | None,
    count: RewrapCount,
    *,
    batch: int,
    dry_run: bool,
) -> None:
    after: tuple | None = None
    while True:
        with transaction(engine, org_id=org_id) as tx:
            params: dict = {"active": keyring.key_id, "batch": batch}
            if after is not None:
                params.update({f"after_{i}": value for i, value in enumerate(after)})
            rows = tx.execute(text(_batch_sql(column, after is not None)), params).all()
            for row in rows:
                pk = tuple(row[: len(column.pk)])
                old_ct, old_key = row[len(column.pk)], row[len(column.pk) + 1]
                try:
                    moved = keyring.rewrap(old_ct, old_key)
                except DecryptionError as exc:
                    count.failed += 1
                    log.warning(
                        "kek-rewrap: a value in %s under %s cannot be re-wrapped (%s)",
                        column.table, old_key, exc,
                    )
                    continue
                if dry_run:
                    count.rewrapped += 1
                    continue
                written = tx.execute(
                    text(_update_sql(column)),
                    {
                        **{f"pk_{i}": value for i, value in enumerate(pk)},
                        "new_ct": moved.ciphertext,
                        "active": moved.key_id,
                        "old_key": old_key,
                        "old_ct": old_ct,
                    },
                ).rowcount
                if written == 1:
                    count.rewrapped += 1
                else:
                    count.skipped += 1
        if len(rows) < batch:
            return
        after = tuple(rows[-1][: len(column.pk)])


def rewrap_all(
    engine: Engine,
    keyring: Keyring,
    *,
    batch: int = DEFAULT_BATCH,
    dry_run: bool = False,
    columns: tuple[SealedColumn, ...] = SEALED_COLUMNS,
) -> dict[str, RewrapCount]:
    """Re-wrap every sealed value not under the active KEK; returns the counts per table."""
    if keyring.ephemeral:
        raise KeyUnavailable("re-wrapping onto an ephemeral key would make every value unreadable")
    if batch < 1:
        raise ValueError("batch must be at least 1")
    result: dict[str, RewrapCount] = {}
    for column in columns:
        count = result.setdefault(column.table, RewrapCount())
        for org_id in _scopes(engine, column):
            _rewrap_scope(
                engine, keyring, column, org_id, count, batch=batch, dry_run=dry_run
            )
    return result


def counts_by_key(
    engine: Engine, columns: tuple[SealedColumn, ...] = SEALED_COLUMNS
) -> dict[tuple[str, str], int]:
    """How many values each KEK wraps: `{(table, key_id): count}` (key ids with no values are
    absent)."""
    counts: dict[tuple[str, str], int] = {}
    for column in columns:
        sql = text(
            f"SELECT {column.key_id}, count(*) FROM {column.table} GROUP BY {column.key_id}"
        )
        for org_id in _scopes(engine, column):
            with transaction(engine, org_id=org_id) as tx:
                for key_id, n in tx.execute(sql).all():
                    key = (column.table, key_id)
                    counts[key] = counts.get(key, 0) + int(n)
    return counts
