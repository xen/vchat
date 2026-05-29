"""clean source config: keep only known keys

Revision ID: f7a8b9c0d1e2
Revises: e4f5a6b7c8d9
Create Date: 2026-05-29 15:30:00.000000

"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = "f7a8b9c0d1e2"
down_revision: Union[str, Sequence[str], None] = "e4f5a6b7c8d9"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

# Known top-level keys in source.config
_KNOWN_KEYS = [
    "crawler_user_agent",
    "crawler_concurrent_requests",
    "crawler_download_delay",
    "crawler_download_timeout",
    "rules",
]

# Valid rule types inside config.rules[]
_VALID_RULE_TYPES = ["xpath", "css", "param", "regex"]


def upgrade() -> None:
    bind = op.get_bind()

    # 1. Remove unknown top-level keys from config
    known = "{" + ",".join(f'"{k}"' for k in _KNOWN_KEYS) + "}"
    bind.execute(
        sa.text(f"""
            UPDATE source
            SET config = (
                SELECT jsonb_object_agg(key, value)
                FROM jsonb_each(config)
                WHERE key = ANY(ARRAY{known}::text[])
            )
            WHERE config IS NOT NULL AND config != '{{}}'::jsonb
        """)
    )

    # 2. Remove rules with invalid type or missing value
    valid_types = "{" + ",".join(f'"{t}"' for t in _VALID_RULE_TYPES) + "}"
    bind.execute(
        sa.text(f"""
            UPDATE source
            SET config = jsonb_set(
                config,
                '{{rules}}',
                COALESCE(
                    (
                        SELECT jsonb_agg(rule)
                        FROM jsonb_array_elements(config->'rules') AS rule
                        WHERE
                            rule->>'type' = ANY(ARRAY{valid_types}::text[])
                            AND (rule->>'value') IS NOT NULL
                            AND (rule->>'value') != ''
                    ),
                    '[]'::jsonb
                )
            )
            WHERE config ? 'rules'
        """)
    )

    # 3. Remove empty rules array
    bind.execute(
        sa.text("""
            UPDATE source
            SET config = config - 'rules'
            WHERE config->'rules' = '[]'::jsonb
        """)
    )

    # 4. Ensure config is never NULL
    bind.execute(
        sa.text("UPDATE source SET config = '{}'::jsonb WHERE config IS NULL")
    )


def downgrade() -> None:
    pass
