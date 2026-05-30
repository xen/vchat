from __future__ import annotations

from collections import deque
from types import SimpleNamespace

import pytest

from vchat.views.projects import views as project_views


class _Resp:
    def __init__(self, *, all_rows=None, one_row=None, scalar_value=None):
        self._all_rows = all_rows or []
        self._one_row = one_row
        self._scalar_value = scalar_value

    def all(self):
        return self._all_rows

    def one(self):
        return self._one_row

    def scalar(self):
        return self._scalar_value


class _DB:
    def __init__(self, *, execute_results=None):
        self.execute_results = deque(execute_results or [])

    async def execute(self, stmt):
        _ = stmt
        return self.execute_results.popleft()


class _Redis:
    async def lrange(self, key, start, end):
        _ = key, start, end
        return [b"1.5", b"2.5"]


class _Req(dict):
    def __getattr__(self, name):
        try:
            return self[name]
        except KeyError as exc:
            raise AttributeError(name) from exc


@pytest.mark.asyncio
async def test_project_progress_counts_only_fully_embedded_docs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db = _DB(
        execute_results=[
            _Resp(one_row=SimpleNamespace(total=10, done=4)),
            _Resp(
                all_rows=[
                    SimpleNamespace(id=1, title="Main", total=6, done=3),
                    SimpleNamespace(id=2, title="Docs", total=4, done=1),
                ]
            ),
            _Resp(scalar_value=12.0),
        ]
    )
    req = _Req(db=db, app={project_views.REDIS_KEY: _Redis()})
    monkeypatch.setattr(
        project_views, "_project_context", lambda request: SimpleNamespace(id="global")
    )

    raw = project_views.project_progress.__wrapped__.__wrapped__.__wrapped__
    payload = await raw(req)

    assert payload["total_docs"] == 10
    assert payload["done_docs"] == 4
    assert payload["remaining_docs"] == 6
    assert payload["pct"] == 40
    assert payload["sources"][0]["done"] == 3
    assert payload["sources"][0]["remaining"] == 3
    assert payload["sources"][1]["done"] == 1
    assert payload["sources"][1]["remaining"] == 3
