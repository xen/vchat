import base64
import enum
from datetime import datetime
from decimal import Decimal
from typing import Annotated, Any, ClassVar

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import ARRAY, BYTEA, JSONB, NUMERIC
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.sql.sqltypes import TIMESTAMP

from vchat.db import Base as _Base
from vchat.utils import json


class JsonError(Exception):
    pass


class JsonTypeError(JsonError, TypeError):
    def __init__(self, type_):
        self.type_ = type_
        super().__init__(f"Unexpected type during (un)jsoning values: {type_.__name__}")


class JsonColumnError(JsonError, ValueError):
    def __init__(self, column, klass):
        self.column = column
        self.klass = klass
        super().__init__(
            f"Unhandable column {column} during unjsoning {klass.__name__}",
        )


str_66 = Annotated[str, 66]
str_42 = Annotated[str, 42]


class DateTime_(sa.TypeDecorator):
    impl = TIMESTAMP(timezone=True)
    cache_ok = True

    def process_bind_param(self, value, _):
        if isinstance(value, str):
            return datetime.fromisoformat(value)
        return value

    @property
    def python_type(self):
        return datetime


def dict_to_json(d: dict[str, Any]):
    d = d.copy()

    for key, value in d.items():
        if isinstance(value, (int, str, float, dict, list)) or value is None:
            continue

        if isinstance(value, datetime):
            d[key] = value.isoformat()
        elif isinstance(value, Decimal):
            d[key] = str(value)
        elif isinstance(value, enum.Enum):
            d[key] = value.name
        elif isinstance(value, bytes):
            d[key] = base64.b64encode(value).decode("utf-8")
        else:
            raise JsonTypeError(type(value))

    return json.dumps(d)


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

    def asdict(self, *, columnwise: bool = False):
        if columnwise:
            return {column: getattr(self, column) for column in self.__mapper__.columns}

        d = self.__dict__.copy()
        d.pop("_sa_instance_state", None)
        return d

    def to_dict(self, *, columnwise: bool = False):
        return self.asdict(columnwise=columnwise)

    def to_json(self):
        return dict_to_json(self.asdict())

    @classmethod
    def from_json(cls, s):
        d = json.loads(s)
        if not isinstance(d, dict):
            raise JsonTypeError(type(d))

        columns = sa.inspect(cls).columns
        for key, value in d.items():
            if key not in columns:
                raise JsonColumnError(key, cls)

            type_ = columns[key].type.python_type
            if issubclass(type_, (int, str, float, dict, list)) or value is None:
                continue

            if issubclass(type_, datetime):
                d[key] = datetime.fromisoformat(value)
            elif issubclass(type_, Decimal):
                d[key] = Decimal(value)
            elif issubclass(type_, Enum):
                d[key] = type_[value]
            elif issubclass(type_, bytes):
                d[key] = base64.b64decode(value)
            else:
                raise JsonTypeError(type_)

        return cls(**d)


class Created:
    created_at: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True),
        server_default=sa.text("CURRENT_TIMESTAMP"),
    )


class Updated:
    updated_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True))


class ShortId:
    short_id: Mapped[str] = mapped_column(sa.String(20), unique=True, nullable=True)


class Enum(enum.Enum):
    @property
    def literal_value(self):
        return sa.literal(self, type_=sa.Enum(self.__class__))


class TimeInterval(Enum):
    minute = 1
    hour = 60
    day = 1440
