"""Shared response renderer (DESIGN.md §5 + §4.4).

Every synthesis tool builds a Report; render() emits the one authoritative
markdown payload under a hard token cap:

    [⚠ ACTIVE banner]                       ← undroppable, from active alerts
    # Title — Ddd YYYY-MM-DD (data as of HH:MM)   ← undroppable
    **VERDICT: …**                          ← undroppable
    [evidence table(s)]
    [watch-list, ≤3 items]
    [detail sections]
    Next: tool(args) · tool(args)           ← undroppable

Runtime token estimator: estimate_tokens(text) = ceil(len(text)/3.2).
CI asserts the estimator never undercounts a real tokenizer on golden renders.

Drop order when over cap (§5 rule 7), each drop disclosed with a one-line
notice built from Section.overflow_hint when present:
  ① detail-section rows beyond the 6 most recent (undroppable rows survive)
  ② whole secondary sections, last first
  ③ watch-list items beyond 3 (structural: never rendered, always disclosed)
  ④ method parentheticals
  ⑤ (last resort) primary-section rows beyond the 3 most recent, then the
     watch list and whole remaining sections — undroppable rows survive.
Never dropped: banner, title, verdict, breadcrumb. ValueError only when that
four-line skeleton alone exceeds the cap.

Rows are assumed ordered most-recent-first; trims keep the head of the list.
Provisional verdicts must start with 'PROVISIONAL'; non-provisional verdicts
must not contain it (asserted). Breadcrumb validity is CI's job, not runtime's.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

_WEEKDAYS = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")
_SEVERITY_RANK = {"RED": 0, "AMBER": 1}


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


def format_date(date: str) -> str:
    """'2026-07-19' → 'Sun 2026-07-19' (§5 rule 3; real calendar weekday)."""
    dt = datetime.strptime(date, "%Y-%m-%d")
    return f"{_WEEKDAYS[dt.weekday()]} {date}"


def _format_date_short(date: str) -> str:
    """'2026-07-16' → 'Thu 07-16' (banner / table form)."""
    dt = datetime.strptime(date, "%Y-%m-%d")
    return f"{_WEEKDAYS[dt.weekday()]} {date[5:]}"


def arrow_series(values: list[float], max_points: int = 12, fmt: str = "{:.0f}") -> str:
    """'310→342→296→405', downsampled evenly to ≤max_points, always keeping
    the first and last point (§5 rule 4)."""
    n = len(values)
    if n == 0:
        return ""
    if n <= max_points:
        picked = values
    elif max_points <= 1:
        picked = [values[-1]]
    else:
        idx = [round(i * (n - 1) / (max_points - 1)) for i in range(max_points)]
        picked = [values[i] for i in dict.fromkeys(idx)]
    return "→".join(fmt.format(v) for v in picked)


def format_banner(active_alerts: list[dict[str, Any]]) -> str | None:
    """'⚠ ACTIVE (since Ddd MM-DD): msg1 + msg2 — see garmin_recovery()' from
    RED/AMBER alerts only (WATCH never banners); None when no RED/AMBER.
    Joins the ≤2 highest-severity messages, '+N more' beyond."""
    hot = [a for a in active_alerts if a.get("severity") in _SEVERITY_RANK]
    if not hot:
        return None
    hot.sort(key=lambda a: (_SEVERITY_RANK[a["severity"]], a["date"]))
    since = _format_date_short(min(a["date"] for a in hot))
    msgs = " + ".join(a["message"] for a in hot[:2])
    if len(hot) > 2:
        msgs += f" +{len(hot) - 2} more"
    return f"⚠ ACTIVE (since {since}): {msgs} — see garmin_recovery()"


# ---------------------------------------------------------------------------
# render() and its working state
# ---------------------------------------------------------------------------

@dataclass
class _WorkSection:
    sec: Section
    rows: list[Row]
    dropped_rows: int = 0
    dropped_whole: bool = False
    show_method_note: bool = True


class _WorkState:
    def __init__(self, report: Report):
        self.report = report
        self.sections = [_WorkSection(sec=s, rows=list(s.rows)) for s in report.sections]
        self.watch_kept = report.watch_list[:3]           # >3 never rendered (③)
        self.watch_extra = max(0, len(report.watch_list) - 3)
        self.watch_dropped = False
        self.method_notes_omitted = False

    # -- text assembly ------------------------------------------------------

    def _section_blocks(self, ws: _WorkSection) -> list[str]:
        sec = ws.sec
        if ws.dropped_whole:
            what = f"{sec.title} omitted" if sec.title else "section omitted"
            hint = f" — {sec.overflow_hint}" if sec.overflow_hint else ""
            return [f"({what}{hint})"]
        lines: list[str] = []
        if sec.title:
            lines.append(f"**{sec.title}**")
        if sec.header is not None:
            lines.append("| " + " | ".join(sec.header) + " |")
            lines.append("|" + "---|" * len(sec.header))
            for row in ws.rows:
                lines.append("| " + " | ".join(row.cells) + " |")
        elif sec.prose:
            lines.append(sec.prose)
        if ws.dropped_rows:
            if sec.overflow_hint:
                lines.append(f"({ws.dropped_rows} more rows — {sec.overflow_hint})")
            else:
                lines.append(f"({ws.dropped_rows} more rows omitted)")
        if sec.method_note and ws.show_method_note:
            lines.append(f"({sec.method_note})")
        return ["\n".join(lines)] if lines else []

    def build(self) -> str:
        r = self.report
        blocks: list[str] = []
        if r.banner:
            blocks.append(r.banner)
        blocks.append(f"# {r.title} — {format_date(r.date)} (data as of {r.data_as_of})")
        blocks.append(f"**VERDICT: {r.verdict}**")
        for ws in self.sections:
            blocks.extend(self._section_blocks(ws))
        if r.watch_list:
            if self.watch_dropped:
                blocks.append("(watch list omitted)")
            else:
                lines = ["Watch:"] + [f"- {item}" for item in self.watch_kept]
                if self.watch_extra:
                    lines.append(f"({self.watch_extra} more omitted)")
                blocks.append("\n".join(lines))
        if self.method_notes_omitted:
            blocks.append("(method notes omitted)")
        if r.next_steps:
            blocks.append("Next: " + " · ".join(r.next_steps))
        return "\n\n".join(blocks)

    # -- drop actions -------------------------------------------------------

    def trim_rows(self, ws: _WorkSection, keep: int) -> None:
        """Keep the `keep` most recent rows (list head); undroppable survive."""
        head, tail = ws.rows[:keep], ws.rows[keep:]
        surviving = [r for r in tail if r.undroppable]
        n_drop = len(tail) - len(surviving)
        if n_drop:
            ws.rows = head + surviving
            ws.dropped_rows += n_drop


def render(report: Report, cap_tokens: int) -> str:
    """Emit markdown under cap_tokens (estimator-based), applying the drop
    order with disclosure lines. Raises ValueError if the undroppable skeleton
    (banner/title/verdict/breadcrumb) alone exceeds the cap."""
    if report.provisional:
        assert report.verdict.startswith("PROVISIONAL"), (
            "provisional report requires a 'PROVISIONAL (n=…)'-prefixed verdict"
        )
    else:
        assert "PROVISIONAL" not in report.verdict, (
            "non-provisional verdict must not claim PROVISIONAL"
        )

    skeleton_state = _WorkState(
        Report(
            title=report.title, date=report.date, data_as_of=report.data_as_of,
            verdict=report.verdict, provisional=report.provisional,
            banner=report.banner, next_steps=report.next_steps,
        )
    )
    if estimate_tokens(skeleton_state.build()) > cap_tokens:
        raise ValueError(
            f"undroppable skeleton exceeds cap_tokens={cap_tokens} — contract violation"
        )

    st = _WorkState(report)

    def fits() -> bool:
        return estimate_tokens(st.build()) <= cap_tokens

    if fits():
        return st.build()

    # ① detail rows beyond the 6 most recent — secondary sections first so
    # primary evidence tables are only touched if the cap still overflows
    for priority in ("secondary", "primary"):
        for ws in st.sections:
            if ws.sec.header is not None and ws.sec.priority == priority:
                st.trim_rows(ws, 6)
                if fits():
                    return st.build()

    # ② whole secondary sections, last first
    for ws in reversed(st.sections):
        if ws.sec.priority == "secondary" and not ws.dropped_whole:
            ws.dropped_whole = True
            if fits():
                return st.build()

    # ③ watch-list items beyond 3 — structurally applied at build time

    # ④ method parentheticals
    if any(ws.sec.method_note and ws.show_method_note and not ws.dropped_whole
           for ws in st.sections):
        for ws in st.sections:
            ws.show_method_note = False
        st.method_notes_omitted = True
        if fits():
            return st.build()

    # ⑤ last resort: primary rows beyond the 3 most recent …
    for ws in st.sections:
        if ws.sec.header is not None and not ws.dropped_whole:
            st.trim_rows(ws, 3)
            if fits():
                return st.build()
    # … then the remaining watch list …
    if st.watch_kept and not st.watch_dropped:
        st.watch_dropped = True
        if fits():
            return st.build()
    # … then whole remaining sections, last first (undroppable rows survive).
    for ws in reversed(st.sections):
        if ws.dropped_whole:
            continue
        if any(r.undroppable for r in ws.rows):
            st.trim_rows(ws, 0)
        else:
            ws.dropped_whole = True
        if fits():
            return st.build()

    # Only undroppable content remains; skeleton fits, so return best effort.
    return st.build()
