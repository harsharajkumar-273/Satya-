"""
Backend state snapshots and diffing.

The reconciler needs to know what the backend *actually* did, independent of
what the UI claimed. Rather than per-flow API assertions, we take a generic
snapshot of the backend's records before and after an action and diff them.
An inferred Claim (REMOVAL / MUTATION / CREATION) is then checked against this
diff generically.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable


@dataclass
class BackendSnapshot:
    records: dict[str, dict]  # id -> full record

    @classmethod
    def from_list(
        cls,
        rows: list[dict],
        id_key: str | Callable[[dict], str] = "id",
    ) -> "BackendSnapshot":
        records: dict[str, dict] = {}
        for r in rows:
            if not isinstance(r, dict):
                continue
            if callable(id_key):
                rid = id_key(r)
                if rid:
                    records[str(rid)] = r
            elif id_key in r:
                records[str(r[id_key])] = r
        return cls(records=records)


@dataclass
class BackendDiff:
    removed_ids: set[str] = field(default_factory=set)
    added_ids: set[str] = field(default_factory=set)
    changed: dict[str, dict] = field(default_factory=dict)  # id -> {field: (old, new)}

    @property
    def any_change(self) -> bool:
        return bool(self.removed_ids or self.added_ids or self.changed)

    def field_changed_to(self, record_id: str, expected_val: Any) -> bool:
        """Check if any field in record_id was changed to expected_val."""
        from reconcile.reconciler import _values_match  # local import: avoids a module cycle
        field_changes = self.changed.get(str(record_id), {})
        return any(_values_match(expected_val, new) for (_old, new) in field_changes.values())


def diff_backend(before: BackendSnapshot, after: BackendSnapshot) -> BackendDiff:
    before_ids, after_ids = set(before.records), set(after.records)
    diff = BackendDiff(
        removed_ids=before_ids - after_ids,
        added_ids=after_ids - before_ids,
    )
    for rid in before_ids & after_ids:
        b, a = before.records[rid], after.records[rid]
        field_changes = {
            k: (b.get(k), a.get(k))
            for k in set(b) | set(a)
            if not k.startswith("_") and b.get(k) != a.get(k)
        }
        if field_changes:
            diff.changed[rid] = field_changes
    return diff
