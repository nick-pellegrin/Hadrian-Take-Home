"""Problems found while loading configuration or planning a warm-up."""

from dataclasses import dataclass
from enum import StrEnum


class Severity(StrEnum):
    ERROR = "error"  # blocks generation
    WARNING = "warning"  # reported, but a program can still be generated


@dataclass(frozen=True)
class Issue:
    """One problem, located by its dotted TOML path (e.g. ``machines.M2.travel.x``).

    The path lets the CLI point at the offending setting and the UI show the
    message next to the matching form field.
    """

    path: str
    message: str
    severity: Severity = Severity.ERROR
    source: str | None = None  # file the value came from, if any

    def __str__(self) -> str:
        location = ": ".join(part for part in (self.source, self.path) if part)
        return f"{location}: {self.message}" if location else self.message
