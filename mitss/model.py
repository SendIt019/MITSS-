"""Data model for MITSS plans and schedules.

Everything here is plain-stdlib dataclasses. No third-party dependencies, so
the harness runs on any Python 3.9+ install without a pip step.

Time handling: all timestamps are ISO-8601 (International Organization for
Standardization date/time format), e.g. "2026-08-11T08:00:00". Timezone-aware
values are converted to UTC and then stored naive, so the whole timeline is
internally consistent. Do not mix zones in one plan.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

ISO_HINT = "ISO-8601 timestamp, e.g. 2026-08-11T08:00:00"


def parse_dt(value: Any) -> datetime:
    """Parse an ISO-8601 string into a naive datetime.

    Raises ValueError with a readable message on anything unparseable.
    """
    if isinstance(value, datetime):
        dt = value
    elif isinstance(value, str):
        text = value.strip()
        if text.endswith("Z") or text.endswith("z"):
            text = text[:-1] + "+00:00"
        try:
            dt = datetime.fromisoformat(text)
        except ValueError as exc:
            raise ValueError(f"{value!r} is not an {ISO_HINT}") from exc
    else:
        raise ValueError(
            f"expected an {ISO_HINT}, got {type(value).__name__}"
        )
    if dt.tzinfo is not None:
        dt = dt.astimezone(timezone.utc).replace(tzinfo=None)
    return dt


def fmt_dt(dt: datetime) -> str:
    """Render a datetime the same way we parse it."""
    return dt.strftime("%Y-%m-%dT%H:%M:%S")


@dataclass
class Interval:
    start: datetime
    end: datetime

    def overlaps(self, other: "Interval") -> bool:
        # Touching endpoints do not count as overlap: [08:00,09:00) and
        # [09:00,10:00) are back-to-back, not a conflict.
        return self.start < other.end and other.start < self.end

    def contains(self, other: "Interval") -> bool:
        return self.start <= other.start and other.end <= self.end

    @property
    def minutes(self) -> int:
        return int((self.end - self.start).total_seconds() // 60)

    def to_dict(self) -> Dict[str, str]:
        return {"start": fmt_dt(self.start), "end": fmt_dt(self.end)}


@dataclass
class Resource:
    """Something that performs work: a person, team, asset, or slot."""

    id: str
    name: str = ""
    capacity: int = 1
    available: List[Interval] = field(default_factory=list)

    def is_available_for(self, window: Interval) -> bool:
        """Empty availability means 'available for the whole horizon'."""
        if not self.available:
            return True
        return any(w.contains(window) for w in self.available)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "capacity": self.capacity,
            "available": [w.to_dict() for w in self.available],
        }


@dataclass
class Task:
    """A unit of work to be placed on the timeline."""

    id: str
    name: str = ""
    duration_minutes: int = 60
    depends_on: List[str] = field(default_factory=list)
    requires: List[str] = field(default_factory=list)
    earliest_start: Optional[datetime] = None
    deadline: Optional[datetime] = None
    priority: int = 3
    notes: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "duration_minutes": self.duration_minutes,
            "depends_on": list(self.depends_on),
            "requires": list(self.requires),
            "earliest_start": fmt_dt(self.earliest_start) if self.earliest_start else None,
            "deadline": fmt_dt(self.deadline) if self.deadline else None,
            "priority": self.priority,
            "notes": self.notes,
        }


@dataclass
class Plan:
    """The input side: what needs scheduling, and under what rules."""

    session: str
    horizon: Interval
    resources: List[Resource] = field(default_factory=list)
    tasks: List[Task] = field(default_factory=list)
    domain: str = "generic"
    granularity_minutes: int = 15
    objectives: List[str] = field(default_factory=list)
    notes: str = ""

    def task(self, task_id: str) -> Optional[Task]:
        for t in self.tasks:
            if t.id == task_id:
                return t
        return None

    def resource(self, resource_id: str) -> Optional[Resource]:
        for r in self.resources:
            if r.id == resource_id:
                return r
        return None

    @property
    def task_ids(self) -> List[str]:
        return [t.id for t in self.tasks]

    @property
    def resource_ids(self) -> List[str]:
        return [r.id for r in self.resources]

    def to_dict(self) -> Dict[str, Any]:
        return {
            "session": self.session,
            "domain": self.domain,
            "horizon": self.horizon.to_dict(),
            "granularity_minutes": self.granularity_minutes,
            "resources": [r.to_dict() for r in self.resources],
            "tasks": [t.to_dict() for t in self.tasks],
            "objectives": list(self.objectives),
            "notes": self.notes,
        }


@dataclass
class Assignment:
    """One scheduled placement of a task onto a resource."""

    task_id: str
    resource_id: str
    start: datetime
    end: datetime

    @property
    def window(self) -> Interval:
        return Interval(self.start, self.end)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "task_id": self.task_id,
            "resource_id": self.resource_id,
            "start": fmt_dt(self.start),
            "end": fmt_dt(self.end),
        }


@dataclass
class Schedule:
    """The output side: what the model decided."""

    session: str
    assignments: List[Assignment] = field(default_factory=list)
    unscheduled: List[Dict[str, str]] = field(default_factory=list)
    rationale: str = ""

    def for_task(self, task_id: str) -> List[Assignment]:
        return [a for a in self.assignments if a.task_id == task_id]

    def for_resource(self, resource_id: str) -> List[Assignment]:
        return sorted(
            (a for a in self.assignments if a.resource_id == resource_id),
            key=lambda a: a.start,
        )

    @property
    def makespan_minutes(self) -> int:
        if not self.assignments:
            return 0
        start = min(a.start for a in self.assignments)
        end = max(a.end for a in self.assignments)
        return int((end - start).total_seconds() // 60)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "session": self.session,
            "assignments": [a.to_dict() for a in self.assignments],
            "unscheduled": list(self.unscheduled),
            "rationale": self.rationale,
        }


def minutes(n: int) -> timedelta:
    return timedelta(minutes=n)
