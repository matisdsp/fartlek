"""Health domain entities.

Kept intentionally light for the prototype: the LLM consumes filtered Garmin
payloads directly as JSON. As soon as we compute things (training load, ACWR,
weekly aggregates) we promote those calculations into rich entities here.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date


@dataclass(frozen=True, slots=True)
class DateRange:
    start: date
    end: date

    def __post_init__(self) -> None:
        if self.start > self.end:
            raise ValueError(f"start {self.start} > end {self.end}")
