"""backfill default ignored params for source configs

Revision ID: c1d2e3f4a5b6
Revises: aa12bb34cc56
Create Date: 2026-05-31 09:40:00.000000

"""

from __future__ import annotations

import json
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = "c1d2e3f4a5b6"
down_revision: Union[str, Sequence[str], None] = "aa12bb34cc56"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

DEFAULT_IGNORED_PARAMS = (
    "utm_source",
    "utm_medium",
    "utm_campaign",
    "utm_term",
    "utm_content",
    "gclid",
    "gclsrc",
    "fbclid",
    "msclkid",
    "twclid",
    "igshid",
    "mc_cid",
    "mc_eid",
    "_hsenc",
    "gtm_debug",
    "session_id",
    "page",
    "sort",
    "yclid",
)


def _merge_rules(rules: object) -> list[dict[str, str]]:
    merged: list[dict[str, str]] = []
    seen: set[tuple[str, str]] = set()

    for param in DEFAULT_IGNORED_PARAMS:
        merged.append({"type": "param", "value": param})
        seen.add(("param", param))

    if not isinstance(rules, list):
        return merged

    for rule in rules:
        if not isinstance(rule, dict):
            continue
        rule_type = str(rule.get("type") or "").strip()
        rule_value = str(rule.get("value") or "").strip()
        if not rule_type or not rule_value:
            continue
        key = (rule_type, rule_value)
        if key in seen:
            continue
        merged.append({"type": rule_type, "value": rule_value})
        seen.add(key)

    return merged


def _strip_default_rules(rules: object) -> list[dict[str, str]]:
    if not isinstance(rules, list):
        return []

    cleaned: list[dict[str, str]] = []
    for rule in rules:
        if not isinstance(rule, dict):
            continue
        rule_type = str(rule.get("type") or "").strip()
        rule_value = str(rule.get("value") or "").strip()
        if not rule_type or not rule_value:
            continue
        if rule_type == "param" and rule_value in DEFAULT_IGNORED_PARAMS:
            continue
        cleaned.append({"type": rule_type, "value": rule_value})
    return cleaned


def upgrade() -> None:
    bind = op.get_bind()
    rows = bind.execute(
        sa.text("SELECT id, config FROM source ORDER BY id")
    ).mappings().all()

    for row in rows:
        config = row["config"] if isinstance(row["config"], dict) else {}
        new_config = dict(config)
        new_config["rules"] = _merge_rules(config.get("rules"))
        bind.execute(
            sa.text("UPDATE source SET config = CAST(:config AS jsonb) WHERE id = :id"),
            {"config": json.dumps(new_config, ensure_ascii=False), "id": row["id"]},
        )


def downgrade() -> None:
    bind = op.get_bind()
    rows = bind.execute(
        sa.text("SELECT id, config FROM source ORDER BY id")
    ).mappings().all()

    for row in rows:
        config = row["config"] if isinstance(row["config"], dict) else {}
        new_config = dict(config)
        cleaned_rules = _strip_default_rules(config.get("rules"))
        if cleaned_rules:
            new_config["rules"] = cleaned_rules
        else:
            new_config.pop("rules", None)
        bind.execute(
            sa.text("UPDATE source SET config = CAST(:config AS jsonb) WHERE id = :id"),
            {"config": json.dumps(new_config, ensure_ascii=False), "id": row["id"]},
        )
