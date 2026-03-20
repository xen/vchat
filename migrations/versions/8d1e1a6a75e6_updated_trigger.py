"""updated trigger

Revision ID: 8d1e1a6a75e6
Revises: 09619a4b8f9c
Create Date: 2025-04-26 23:13:05.784481

"""

from alembic import op
from vchat.models import Base

# revision identifiers, used by Alembic.
revision = "8d1e1a6a75e6"
down_revision = "8e870215bf5f"
branch_labels = None
depends_on = None


def get_tables_with_updated_at(metadata):
    tables = []
    for table_name, table in metadata.tables.items():
        if "updated_at" in table.columns:
            tables.append(table_name)
    return tables


def upgrade():
    # Создать функцию обновления
    op.execute("""
    CREATE OR REPLACE FUNCTION update_updated_at_column()
    RETURNS TRIGGER AS $$
    BEGIN
        NEW.updated_at = now();
        RETURN NEW;
    END;
    $$ language 'plpgsql';
    """)

    # Получить все таблицы с updated_at
    tables = get_tables_with_updated_at(Base.metadata)

    for table in tables:
        op.execute(f"""
        CREATE TRIGGER set_updated_at_{table}
        BEFORE UPDATE ON {table}
        FOR EACH ROW
        EXECUTE FUNCTION update_updated_at_column();
        """)


def downgrade():
    tables = get_tables_with_updated_at(Base.metadata)

    for table in tables:
        op.execute(f"DROP TRIGGER IF EXISTS set_updated_at_{table} ON {table};")

    op.execute("DROP FUNCTION IF EXISTS update_updated_at_column;")
