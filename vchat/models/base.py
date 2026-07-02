import enum
import uuid6
from datetime import datetime
from decimal import Decimal
from typing import Annotated, Any, ClassVar

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import ARRAY, BYTEA, JSONB, NUMERIC
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.sql.sqltypes import TIMESTAMP

from vchat.db import Base as _Base


str_66 = Annotated[str, 66]
str_42 = Annotated[str, 42]


class DateTime_(sa.TypeDecorator):
    impl = TIMESTAMP(timezone=True)
    cache_ok = True

    def process_bind_param(self, value, dialect):
        if isinstance(value, str):
            return datetime.fromisoformat(value)
        return value

    @property
    def python_type(self):
        return datetime


class Base(_Base):
    __abstract__ = True
    type_annotation_map: ClassVar[dict[Any, Any]] = {
        dict[str, Any]: JSONB,
        dict[str, str]: JSONB,
        list[str]: ARRAY(sa.String),
        list[dict[str, Any]]: JSONB,
        datetime: DateTime_,
        bytes: BYTEA,
        Decimal: NUMERIC,
    }


class Created:
    created_at: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True),
        server_default=sa.text("CURRENT_TIMESTAMP"),
    )


class Updated:
    updated_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True))


def generate_uuid7() -> str:
    return str(uuid6.uuid7())


class Enum(enum.Enum):
    @property
    def literal_value(self):
        return sa.literal(self, type_=sa.Enum(self.__class__))


class TimeInterval(Enum):
    minute = 1
    hour = 60
    day = 1440
