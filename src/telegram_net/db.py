"""Стан обробки: SQLite для старту, той самий код працює з Postgres через DATABASE_URL."""

from __future__ import annotations

import datetime as dt

from sqlalchemy import (
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
    create_engine,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship, sessionmaker


class Base(DeclarativeBase):
    pass


class Source(Base):
    """Курсор читання одного джерела (last_seen_cursor) — щоб не перечитувати те саме щогодини."""

    __tablename__ = "sources"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    channel_id: Mapped[str] = mapped_column(String, index=True)
    type: Mapped[str] = mapped_column(String)
    handle_or_url: Mapped[str] = mapped_column(String)
    last_seen_cursor: Mapped[str | None] = mapped_column(String, nullable=True)

    __table_args__ = (UniqueConstraint("channel_id", "handle_or_url", name="uq_source_channel_url"),)


class RawItem(Base):
    __tablename__ = "raw_items"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    source_id: Mapped[int] = mapped_column(ForeignKey("sources.id"), index=True)
    external_id: Mapped[str] = mapped_column(String)
    url: Mapped[str] = mapped_column(String)
    text: Mapped[str] = mapped_column(Text)
    content_hash: Mapped[str] = mapped_column(String, index=True)
    origin_published_at: Mapped[dt.datetime | None] = mapped_column(DateTime, nullable=True)
    fetched_at: Mapped[dt.datetime] = mapped_column(DateTime, default=dt.datetime.utcnow)

    media: Mapped[list["MediaAsset"]] = relationship(back_populates="raw_item")
    classification: Mapped["Classification | None"] = relationship(back_populates="raw_item", uselist=False)

    __table_args__ = (UniqueConstraint("source_id", "external_id", name="uq_raw_item_source_external"),)


class MediaAsset(Base):
    __tablename__ = "media_assets"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    raw_item_id: Mapped[int] = mapped_column(ForeignKey("raw_items.id"), index=True)
    type: Mapped[str] = mapped_column(String)  # "photo" | "video"
    source_url: Mapped[str] = mapped_column(String)
    order_in_gallery: Mapped[int] = mapped_column(Integer, default=0)
    duration_sec: Mapped[float | None] = mapped_column(Float, nullable=True)

    raw_item: Mapped[RawItem] = relationship(back_populates="media")


class Classification(Base):
    __tablename__ = "classifications"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    raw_item_id: Mapped[int] = mapped_column(ForeignKey("raw_items.id"), unique=True)
    is_advertisement: Mapped[bool] = mapped_column(Boolean)
    relevance_to_region: Mapped[float] = mapped_column(Float)
    local_news_interest: Mapped[float] = mapped_column(Float)
    reason: Mapped[str] = mapped_column(Text)
    model: Mapped[str] = mapped_column(String)
    classified_at: Mapped[dt.datetime] = mapped_column(DateTime, default=dt.datetime.utcnow)

    raw_item: Mapped[RawItem] = relationship(back_populates="classification")


class GeneratedPost(Base):
    __tablename__ = "generated_posts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    raw_item_id: Mapped[int] = mapped_column(ForeignKey("raw_items.id"))
    channel_id: Mapped[str] = mapped_column(String, index=True)
    text: Mapped[str] = mapped_column(Text)
    length_style: Mapped[str] = mapped_column(String)  # "short" | "medium" | "long"
    has_headline: Mapped[bool] = mapped_column(Boolean)
    status: Mapped[str] = mapped_column(String, default="draft")  # draft|published|rejected
    created_at: Mapped[dt.datetime] = mapped_column(DateTime, default=dt.datetime.utcnow)


class PublishLog(Base):
    __tablename__ = "publish_log"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    generated_post_id: Mapped[int] = mapped_column(ForeignKey("generated_posts.id"))
    telegram_message_id: Mapped[int] = mapped_column(Integer)
    published_at: Mapped[dt.datetime] = mapped_column(DateTime, default=dt.datetime.utcnow)


def make_engine(database_url: str):
    return create_engine(database_url)


def init_db(engine) -> None:
    Base.metadata.create_all(engine)


def make_session_factory(engine):
    return sessionmaker(bind=engine)
