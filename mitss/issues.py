"""A single shared way to report problems.

Validation and constraint checking both return lists of Issue rather than
raising, so one run surfaces every problem at once instead of stopping at the
first one.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List

ERROR = "error"
WARN = "warn"
INFO = "info"

_RANK = {ERROR: 0, WARN: 1, INFO: 2}


@dataclass
class Issue:
    severity: str
    code: str
    message: str
    where: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "severity": self.severity,
            "code": self.code,
            "message": self.message,
            "where": self.where,
        }

    def __str__(self) -> str:
        tag = self.severity.upper().ljust(5)
        loc = f" [{self.where}]" if self.where else ""
        return f"{tag} {self.code}{loc}: {self.message}"


def error(code: str, message: str, where: str = "") -> Issue:
    return Issue(ERROR, code, message, where)


def warn(code: str, message: str, where: str = "") -> Issue:
    return Issue(WARN, code, message, where)


def info(code: str, message: str, where: str = "") -> Issue:
    return Issue(INFO, code, message, where)


def sort_issues(issues: List[Issue]) -> List[Issue]:
    return sorted(issues, key=lambda i: (_RANK.get(i.severity, 9), i.code, i.where))


def count_by_severity(issues: List[Issue]) -> Dict[str, int]:
    counts = {ERROR: 0, WARN: 0, INFO: 0}
    for issue in issues:
        counts[issue.severity] = counts.get(issue.severity, 0) + 1
    return counts


def has_errors(issues: List[Issue]) -> bool:
    return any(i.severity == ERROR for i in issues)


def format_issues(issues: List[Issue]) -> str:
    if not issues:
        return "no issues"
    return "\n".join(str(i) for i in sort_issues(issues))
