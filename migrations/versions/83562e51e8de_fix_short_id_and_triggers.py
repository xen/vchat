"""fix_short_id_and_triggers

Revision ID: 83562e51e8de
Revises: 9badd855953b
Create Date: 2025-11-30 02:18:48.796827

"""

from alembic import op


# revision identifiers, used by Alembic.
revision = "83562e51e8de"
down_revision = "9badd855953b"
branch_labels = None
depends_on = None


def upgrade():
    # Ensure short_id columns exist (they should be there from previous migration, but we can double check or just add triggers)
    # The previous migration added columns and triggers.
    # This migration is to "fix" or ensure they are correct and backfill if missed.
    # Since the user said "make a new migration and add all changes", I will re-apply the logic here to be safe,
    # or rather, ensure the triggers are definitely there and run the update again.

    # Re-create triggers to be sure
    for table in ["chat", "document", "project", "source"]:
        op.execute("""
            CREATE OR REPLACE FUNCTION public.trigger_set_short_id()
            RETURNS TRIGGER AS $$
            BEGIN
                IF NEW.short_id IS NULL THEN
                    NEW.short_id = sqids.encode(array[NEW.id], 'xMXvWAEb78OqjrtGKwzhT51aFeLmuQ9PJsNSi24BgYdfn6HVoZ0pkUy3RDCclI', 10);
                END IF;
                RETURN NEW;
            END;
            $$ LANGUAGE plpgsql;
        """)

        op.execute(f"""
            DROP TRIGGER IF EXISTS set_short_id ON public.{table};
        """)
        op.execute(f"""
            CREATE TRIGGER set_short_id
            BEFORE INSERT OR UPDATE
            ON public.{table}
            FOR EACH ROW
            EXECUTE FUNCTION public.trigger_set_short_id();
        """)

        # Backfill existing records
        op.execute(f"""
            UPDATE public.{table}
            SET short_id = sqids.encode(array[id], 'xMXvWAEb78OqjrtGKwzhT51aFeLmuQ9PJsNSi24BgYdfn6HVoZ0pkUy3RDCclI', 10)
            WHERE short_id IS NULL;
        """)


def downgrade():
    # We don't really need to downgrade this as it just ensures triggers and data
    pass
