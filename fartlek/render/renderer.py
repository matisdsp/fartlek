"""Shared response renderer (DESIGN.md §5 + §4.4). CONTRACT STUB.

Every synthesis tool builds a Report; render() emits the one authoritative
markdown payload under a hard token cap:

    [⚠ ACTIVE banner]                       ← undroppable, from active alerts
    # Title — Ddd YYYY-MM-DD (data as of HH:MM)   ← undroppable
    **VERDICT: …**                          ← undroppable
    [evidence table(s)]
    [watch-list, ≤3 items]
    [detail sections]
    Next: tool(args) · tool(args)           ← undroppable

Token estimator (runtime): estimate_tokens(text) = ceil(len(text)/3.2).
CI asserts the estimator never undercounts a real tokenizer on golden renders.

Drop order when over cap (§5 rule 7), each drop disclosed with a one-line
notice (e.g. "(5 more rows — garmin_activities(start_date=…) for all)"):
  ① detail-section rows beyond the 6 most recent
  ② whole secondary sections (Section.priority='secondary'), last first
  ③ watch-list items beyond 3
  ④ method parentheticals (Section.method_note)
Never dropped: banner, title, verdict, alert lines, activity IDs already
rendered, breadcrumb.

Verdict confidence (§5 rule 5): Report.provisional → verdict text must be
prefixed 'PROVISIONAL (n=… of … days) — ' by the caller; render() asserts
provisional verdicts never start with a bare GREEN/RED.

Breadcrumbs: Report.next_steps is a list of literal call strings
('garmin_recovery()'); render() joins with ' · '. Validity against the tool
registry is CI's job, not runtime's.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any


def estimate_tokens(text: str) -> int:
    return math.ceil(len(text) / 3.2)


@dataclass
class Row:
    cells: list[str]
    undroppable: bool = False  # e.g. carries an activity ID already referenced


@dataclass
class Section:
    title: str | None
    header: list[str] | None            # table header, None => prose section
    rows: list[Row] = field(default_factory=list)
    prose: str = ""                     # used when header is None
    priority: str = "primary"           # 'primary' | 'secondary'
    method_note: str | None = None      # short parenthetical, droppable last
    overflow_hint: str | None = None    # breadcrumb-style pointer used in drop notices


@dataclass
class Report:
    title: str                          # without date — render() appends dates
    date: str                           # YYYY-MM-DD
    data_as_of: str                     # HH:MM
    verdict: str
    provisional: bool = False
    banner: str | None = None           # pre-formatted ⚠ ACTIVE line (from alerts)
    sections: list[Section] = field(default_factory=list)
    watch_list: list[str] = field(default_factory=list)
    next_steps: list[str] = field(default_factory=list)


def format_banner(active_alerts: list[dict[str, Any]]) -> str | None:
    """'⚠ ACTIVE (since Ddd MM-DD): <msg> + <msg> — see garmin_recovery()' from
    RED/AMBER alerts only (WATCH never banners); None when no RED/AMBER."""
    raise NotImplementedError


def render(report: Report, cap_tokens: int) -> str:
    """Emit markdown under cap_tokens (estimator-based), applying the drop
    order with disclosure lines. Raises ValueError if the undroppable skeleton
    alone exceeds the cap (a contract violation caught by CI, not runtime)."""
    raise NotImplementedError


def format_date(date: str) -> str:
    """'2026-07-20' → 'Sun 2026-07-20' (§5 rule 3)."""
    raise NotImplementedError


def arrow_series(values: list[float], max_points: int = 12, fmt: str = "{:.0f}") -> str:
    """'310→342→296→405', downsampled evenly to ≤max_points (§5 rule 4)."""
    raise NotImplementedError
