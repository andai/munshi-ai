"""The sealed-column registry (`andai_core.sealed.SEALED_COLUMNS`) matches the data model.

A table that gains a `*_ciphertext` column next to a `key_id` holds envelope-encrypted values; a key
rotation re-wraps only what the registry lists, so a sealed column missing from it would keep its
old KEK and become unreadable once that key is retired.
"""

from __future__ import annotations

from andai_core.db.models import Base
from andai_core.sealed import SEALED_COLUMNS


def _sealed_in_metadata() -> set[tuple[str, str, str]]:
    found = set()
    for table in Base.metadata.tables.values():
        if "key_id" not in table.c:
            continue
        for column in table.c:
            if column.name.endswith("_ciphertext"):
                found.add((table.name, column.name, "key_id"))
    return found


def test_every_sealed_column_in_the_data_model_is_registered() -> None:
    registered = {(c.table, c.ciphertext, c.key_id) for c in SEALED_COLUMNS}

    assert _sealed_in_metadata() <= registered


def test_every_registry_entry_exists_with_its_primary_key_and_org_scope() -> None:
    tables = Base.metadata.tables
    for entry in SEALED_COLUMNS:
        assert entry.table in tables, entry.table
        table = tables[entry.table]
        assert entry.ciphertext in table.c and entry.key_id in table.c, entry.table
        assert tuple(c.name for c in table.primary_key.columns) == entry.pk, entry.table
        assert entry.org_scoped == ("org_id" in table.c), entry.table


def test_the_registry_lists_each_column_once() -> None:
    keys = [(c.table, c.ciphertext) for c in SEALED_COLUMNS]

    assert len(keys) == len(set(keys))
    assert {c.table for c in SEALED_COLUMNS} >= {"user_totp", "platform_principals"}
