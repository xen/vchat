"""randomize demo widget code

Revision ID: ab4c7d8e9f01
Revises: bb2c3d4e5f6a
Create Date: 2026-06-08 00:00:00.000000
"""

from typing import Sequence, Union

from alembic import op


revision: str = "ab4c7d8e9f01"
down_revision: Union[str, None] = "bb2c3d4e5f6a"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute(
        """
        DO $$
        DECLARE
            generated_code text;
        BEGIN
            IF EXISTS (
                SELECT 1 FROM widget_integration WHERE code = 'demo-widget'
            ) THEN
                LOOP
                    generated_code := 'w-' || substr(
                        md5(random()::text || clock_timestamp()::text),
                        1,
                        16
                    );
                    EXIT WHEN NOT EXISTS (
                        SELECT 1
                        FROM widget_integration
                        WHERE code = generated_code
                    );
                END LOOP;

                UPDATE widget_integration
                SET code = generated_code,
                    updated_at = CURRENT_TIMESTAMP
                WHERE code = 'demo-widget';
            END IF;
        END $$;
        """
    )


def downgrade() -> None:
    pass
