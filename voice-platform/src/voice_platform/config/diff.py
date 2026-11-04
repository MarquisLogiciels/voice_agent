"""Structural diff between two configuration documents."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal


@dataclass(frozen=True)
class Change:
    path: str
    op: Literal["added", "removed", "changed"]
    old: Any = None
    new: Any = None

    def as_dict(self) -> dict[str, Any]:
        return {"path": self.path, "op": self.op, "old": self.old, "new": self.new}


def diff_configs(old: Any, new: Any, path: str = "") -> list[Change]:
    changes: list[Change] = []
    if isinstance(old, dict) and isinstance(new, dict):
        for key in sorted(set(old) | set(new), key=str):
            sub = f"{path}.{key}" if path else str(key)
            if key not in old:
                changes.append(Change(sub, "added", new=new[key]))
            elif key not in new:
                changes.append(Change(sub, "removed", old=old[key]))
            else:
                changes.extend(diff_configs(old[key], new[key], sub))
    elif isinstance(old, list) and isinstance(new, list) and len(old) == len(new):
        for i, (a, b) in enumerate(zip(old, new, strict=True)):
            changes.extend(diff_configs(a, b, f"{path}[{i}]"))
    elif old != new:
        changes.append(Change(path or "$", "changed", old=old, new=new))
    return changes
