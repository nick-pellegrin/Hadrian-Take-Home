"""Save UI edits back to the TOML files without losing their comments or layout.

tomlkit keeps every comment and blank line. An existing entry is updated key by
key, in place: inline comments on values and the style of inline tables survive,
and only the values that changed are rewritten. Files are read and written as
bytes, so their line endings are kept as they are.
"""

from collections.abc import Mapping
from pathlib import Path
from typing import Any

import tomlkit
from tomlkit.container import Container
from tomlkit.items import InlineTable, Table

# Nested tables written as their own [section] in a new entry. Other nested
# tables (travel, limits) are written inline, matching the shipped files.
_SECTION_TABLES = frozenset({"fanuc"})


def read_tables(path: Path, section: str) -> dict[str, dict[str, Any]]:
    """Every entry of ``[section.*]`` as plain Python values, in file order."""
    document = _read(path)
    entries = document.get(section, {})
    return {key: entries[key].unwrap() for key in entries}


def save_entry(path: Path, section: str, key: str, values: Mapping[str, object]) -> None:
    """Create or update ``[section.key]`` with ``values``: exactly these keys, in this order."""
    document = _read(path)
    if section not in document:
        document[section] = tomlkit.table(is_super_table=True)
    entries = document[section]
    if key in entries:
        _update(entries[key], values)
    else:
        entries[key] = _new_table(values)
    _write(path, document)


def delete_entry(path: Path, section: str, key: str) -> None:
    document = _read(path)
    del document[section][key]
    _write(path, document)


def _update(table: Table | InlineTable | Container, values: Mapping[str, object]) -> None:
    for stale in [key for key in table if key not in values]:
        del table[stale]
    for key, value in values.items():
        current = table.get(key)
        if isinstance(value, Mapping) and isinstance(current, Table | InlineTable):
            _update(current, value)  # in place, keeping comments and inline style
        elif isinstance(value, Mapping):
            table[key] = _inline(value)
        else:
            table[key] = value  # tomlkit keeps the old value's inline comment


def _new_table(values: Mapping[str, object]) -> Table:
    table = tomlkit.table()
    for key, value in values.items():
        if isinstance(value, Mapping):
            table[key] = _new_table(value) if key in _SECTION_TABLES else _inline(value)
        else:
            table[key] = value
    return table


def _inline(values: Mapping[str, object]) -> InlineTable:
    table = tomlkit.inline_table()
    for key, value in values.items():
        table[key] = _inline(value) if isinstance(value, Mapping) else value
    return table


def _read(path: Path) -> tomlkit.TOMLDocument:
    return tomlkit.parse(path.read_bytes().decode("utf-8"))


def _write(path: Path, document: tomlkit.TOMLDocument) -> None:
    path.write_bytes(tomlkit.dumps(document).encode("utf-8"))
