import sqlalchemy as sa
from sqlalchemy import Computed
from sqlalchemy.dialects.postgresql import TSVECTOR
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base, Created, Updated, ShortId


class PostCategory(Base):
    __tablename__ = "post_category"

    id: Mapped[int] = mapped_column(sa.Integer, primary_key=True)
    slug: Mapped[str] = mapped_column(sa.String(100), nullable=False)
    title: Mapped[str] = mapped_column(sa.String(100), nullable=False)
    description: Mapped[str] = mapped_column(sa.Text, nullable=False)
    description_html: Mapped[str] = mapped_column(sa.Text)
    is_tag: Mapped[bool] = mapped_column(sa.Boolean, nullable=False, default=True)
    is_term: Mapped[bool] = mapped_column(sa.Boolean, nullable=False, default=False)


class Post(Base, Created, Updated, ShortId):
    __tablename__ = "post"

    id: Mapped[int] = mapped_column(sa.Integer, primary_key=True)
    # short_id: Mapped[str] = mapped_column(sa.String(20), unique=True)
    title: Mapped[str] = mapped_column(sa.String(250), nullable=False)
    slug: Mapped[str] = mapped_column(sa.String(250), nullable=False)
    lead: Mapped[str] = mapped_column(sa.Text, nullable=True)
    body: Mapped[str] = mapped_column(sa.Text, nullable=True)
    body_html: Mapped[str] = mapped_column(sa.Text, nullable=True)
    body_toc: Mapped[str] = mapped_column(sa.Text, nullable=True)
    user_id: Mapped[int] = mapped_column(sa.ForeignKey("users.id"), index=True)
    picture: Mapped[str] = mapped_column(sa.String(500), nullable=True)
    show_toc: Mapped[bool] = mapped_column(
        sa.Boolean, nullable=False, server_default="false"
    )
    is_published: Mapped[bool] = mapped_column(
        sa.Boolean, nullable=False, index=True, server_default="false"
    )
    published_at: Mapped[sa.DateTime] = mapped_column(
        sa.DateTime(timezone=True), index=True, nullable=True
    )
    search: Mapped[TSVECTOR] = mapped_column(
        TSVECTOR(),
        Computed(
            (
                "setweight(to_tsvector('simple', coalesce(title, '')), 'A') || "
                "setweight(to_tsvector('simple', coalesce(body, '')), 'B')"
            ),
            persisted=True,
        ),
        nullable=True,
    )


class PostTag(Base):
    __tablename__ = "post_tag"

    id: Mapped[int] = mapped_column(sa.Integer, primary_key=True)
    tag: Mapped[str] = mapped_column(sa.String(140), nullable=False, index=True)
    post_id: Mapped[int] = mapped_column(
        sa.ForeignKey("post.id"), nullable=False, index=True
    )
    post_category_id: Mapped[int] = mapped_column(
        sa.ForeignKey("post_category.id"), nullable=True
    )
    is_primary: Mapped[bool] = mapped_column(sa.Boolean, nullable=False, default=False)
