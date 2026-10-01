"""Save UI edits back to the TOML files without losing their comments or layout.

tomlkit keeps every comment and blank line. An existing entry is updated key by
key, in place: inline comments on values and the style of inline tables survive,
and only the values that changed are rewritten. A rewritten value keeps the file's
number style (a float stays a float: 2 is written 2.0 where the file had 1.0), and
its inline comment stays in the same column. Files are read and written as bytes,
so their line endings are kept as they are.
"""

import re
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import tomlkit
from tomlkit.container import Container
from tomlkit.items import Float, InlineTable, Item, Table

# Nested tables written as their own [section] in a new entry. Other nested
# tables (travel, limits) are written inline, matching the shipped files.
_SECTION_TABLES = frozenset({"fanuc"})


def read_tables(path: Path, section: str) -> dict[str, dict[str, Any]]:
    """Every entry of ``[section.*]`` as plain Python values, in file order."""
    document = _read(path)
    entries = document.get(section, {})
    return {key: entries[key].unwrap() for key in entries}


def save_entry(
    path: Path,
    section: str,
    key: str,
    values: Mapping[str, object],
    *,
    renamed_from: str | None = None,
) -> None:
    """Create or update ``[section.key]`` with ``values``: exactly these keys, in this order.

    With ``renamed_from``, that existing entry is renamed to ``key`` first. It keeps
    its place in the file and its comments.
    """
    document = _read(path)
    if renamed_from is not None and renamed_from != key:
        document = _renamed(document, section, renamed_from, key)
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


def _renamed(
    document: tomlkit.TOMLDocument, section: str, old: str, new: str
) -> tomlkit.TOMLDocument:
    """The document with ``[section.old]`` renamed to ``[section.new]``.

    The usual file layout writes each entry under ``[section.old]`` / ``[section.old.sub]``
    headers. Renaming those header lines changes nothing else, so the entry keeps its
    place and every comment. The result is checked: same entries, same order, same
    contents. A file in another layout (e.g. inline tables) falls back to moving the
    table to the new key at the end of the section, which keeps its comments but not
    its position.
    """
    entries = document[section]
    header = re.compile(rf"^(\[\s*{re.escape(section)}\s*\.\s*){re.escape(old)}(?=\s*[.\]])", re.M)
    candidate = tomlkit.parse(
        header.sub(lambda match: match.group(1) + new, tomlkit.dumps(document))
    )
    expected_order = [new if key == old else key for key in entries]
    renamed = candidate.get(section, {})
    if list(renamed) == expected_order and renamed[new].unwrap() == entries[old].unwrap():
        return candidate
    entries[new] = entries[old]
    del entries[old]
    return document


def _update(table: Table | InlineTable | Container, values: Mapping[str, object]) -> None:
    for stale in [key for key in table if key not in values]:
        del table[stale]
    for key, value in values.items():
        current = _item(table, key)
        if isinstance(value, Mapping) and isinstance(current, Table | InlineTable):
            _update(current, value)  # in place, keeping comments and inline style
        elif isinstance(value, Mapping):
            table[key] = _inline(value)
        elif current is None:
            table[key] = value
        elif current.unwrap() != value:  # an unchanged value keeps its text: 1.0 stays 1.0
            _replace(table, key, current, value)


def _replace(
    table: Table | InlineTable | Container, key: str, current: Item, value: object
) -> None:
    """Replace a value, keeping the file's number style and its comment's column."""
    if isinstance(current, Float) and isinstance(value, int) and not isinstance(value, bool):
        value = float(value)  # the form writes whole numbers as ints
    width = len(current.as_string())
    table[key] = value  # tomlkit keeps the old value's inline comment, and the space before it
    new = _item(table, key)
    assert new is not None
    if new.trivia.comment:
        spaces = len(new.trivia.comment_ws) + width - len(new.as_string())
        new.trivia.comment_ws = " " * max(1, spaces)


def _item(table: Table | InlineTable | Container, key: str) -> Item | None:
    """The tomlkit item under ``key`` (indexing returns a plain bool for booleans)."""
    container = table if isinstance(table, Container) else table.value
    item = container.item(key) if key in container else None
    return item if isinstance(item, Item) else None  # not an out-of-order table's proxy


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
