"""Shared fake-session test doubles for P1-4/5 endpoint tests. No DB.

FakeSession.execute dispatches on substrings of the compiled SQL; each test
file registers (substring, result) pairs via the `responders` list — first
match wins, so order specific substrings before general ones.
"""

from datetime import datetime, timezone

TS = datetime(2026, 7, 22, 12, 0, 0, tzinfo=timezone.utc)


class FakeUser:
    def __init__(self, uid="u1"):
        self.id = uid
        self.email = f"{uid}@thirulaw.com"
        self.display_name = uid


class FakeResult:
    def __init__(self, items=None, scalar=None, rows=None):
        self._items = items or []
        self._scalar = scalar
        self._rows = rows if rows is not None else []

    def scalars(self):
        return self

    def unique(self):
        return self

    def all(self):
        return self._items if self._items else self._rows

    def scalar(self):
        return self._scalar

    def scalar_one_or_none(self):
        return self._scalar


class FakeSession:
    """get() serves objects by (ModelName, key); execute() dispatches on SQL
    substrings via `responders`: list of (substring, FakeResult-or-callable)."""

    def __init__(self, get_objects=None, responders=None):
        self._get_objects = get_objects or {}
        self.responders = responders or []
        self.executed = []
        self.added = []
        self.deleted = []

    async def get(self, model, key):
        return self._get_objects.get((model.__name__, key))

    async def execute(self, stmt):
        sql = str(stmt)
        self.executed.append(sql)
        for substring, result in self.responders:
            if substring in sql:
                return result(sql) if callable(result) else result
        return FakeResult()

    def add(self, obj):
        if getattr(obj, "id", None) is None:
            obj.id = 1000 + len(self.added)
        if getattr(obj, "decided_at", None) is None and hasattr(obj, "decided_at"):
            obj.decided_at = TS
        self.added.append(obj)

    async def flush(self):
        pass

    async def commit(self):
        pass

    async def refresh(self, obj):
        if getattr(obj, "decided_at", None) is None and hasattr(obj, "decided_at"):
            obj.decided_at = TS

    async def delete(self, obj):
        self.deleted.append(obj)
