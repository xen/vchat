from __future__ import annotations

from typing import Any

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.exc import SQLAlchemyError

from vchat.db import async_session_factory
from vchat.models import Settings
from vchat.utils import json

SETTINGS_DEFAULTS: dict[str, str] = {
    "project.title": "vchat",
    "project.system_prompt": "",
    "project.agent_style": "",
    "project.provider": "gigachat",
    "project.model": "gigachat-pro",
    "project.agent_name": "",
    "project.welcome_message": "",
    "project.secret": "",
    "project.topics": "[]",
    "project.intents": "[]",
}

JSON_SETTING_KEYS = {
    "project.topics",
    "project.intents",
}


def _normalize_value(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, str):
        return value
    if isinstance(value, (list, dict)):
        return json.dumps(value)
    return str(value)


def merge_with_defaults(values: dict[str, str | None]) -> dict[str, str | None]:
    merged: dict[str, str | None] = dict(SETTINGS_DEFAULTS)
    merged.update(values)
    return merged


async def load_settings_map() -> dict[str, str | None]:
    try:
        async with async_session_factory() as session:
            rows = (
                await session.execute(sa.select(Settings.key, Settings.value))
            ).all()
        values = {row.key: row.value for row in rows}
        return merge_with_defaults(values)
    except SQLAlchemyError:
        return merge_with_defaults({})


async def init_settings_cache(app) -> None:
    from vchat.app_keys import SETTINGS_KEY

    app[SETTINGS_KEY] = await load_settings_map()


def get_setting(app, key: str, default: str | None = None) -> str | None:
    from vchat.app_keys import SETTINGS_KEY

    settings = app.get(SETTINGS_KEY, {})
    if key in settings:
        return settings[key]
    return default


def get_setting_int(app, key: str, default: int = 0) -> int:
    raw = get_setting(app, key)
    if raw is None:
        return default
    try:
        return int(raw)
    except (TypeError, ValueError):
        return default


def get_setting_json(app, key: str, default: Any):
    raw = get_setting(app, key)
    if raw is None or raw == "":
        return default
    try:
        return json.loads(raw)
    except Exception:
        return default


async def upsert_settings(session, updates: dict[str, Any]) -> dict[str, str | None]:
    cleaned = {key: _normalize_value(value) for key, value in updates.items()}
    if not cleaned:
        return {}

    stmt = insert(Settings).values(
        [{"key": key, "value": value} for key, value in cleaned.items()]
    )
    stmt = stmt.on_conflict_do_update(
        index_elements=[Settings.key],
        set_={"value": stmt.excluded.value},
    )
    try:
        await session.execute(stmt)
        return cleaned
    except SQLAlchemyError:
        return {}


async def apply_settings_updates(
    app, session, updates: dict[str, Any]
) -> dict[str, str | None]:
    from vchat.app_keys import SETTINGS_KEY

    cleaned = await upsert_settings(session, updates)
    cache = dict(app.get(SETTINGS_KEY, {}))
    cache.update(cleaned)
    app[SETTINGS_KEY] = merge_with_defaults(cache)
    return cleaned
