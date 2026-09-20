from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone

from sqlalchemy import DateTime, String, create_engine, delete, func, select
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column

from .config import settings


def _normalize_db_url(url: str) -> str:
    if url.startswith("postgres://"):
        url = "postgresql://" + url[len("postgres://") :]
    if url.startswith("postgresql://") and "+" not in url.split("://", 1)[0]:
        return "postgresql+psycopg://" + url[len("postgresql://") :]
    return url


engine = create_engine(
    _normalize_db_url(settings.database_url),
    pool_pre_ping=True,
    future=True,
)


class Base(DeclarativeBase):
    pass


class ContactHistory(Base):
    __tablename__ = "contact_history"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    order_id: Mapped[str] = mapped_column(String(80), unique=True, index=True)
    phone: Mapped[str] = mapped_column(String(40), default="")
    address_hash: Mapped[str] = mapped_column(String(64), default="")
    contacted_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), index=True
    )


class NaverConfirmHistory(Base):
    __tablename__ = "naver_confirm_history"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    product_order_id: Mapped[str] = mapped_column(String(80), unique=True, index=True)
    confirmed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), index=True
    )


def init_db() -> None:
    Base.metadata.create_all(engine)


@contextmanager
def db_session():
    with Session(engine) as session:
        yield session


def contacted_order_ids(order_ids: list[str]) -> set[str]:
    if not order_ids:
        return set()
    with db_session() as session:
        rows = session.scalars(
            select(ContactHistory.order_id).where(ContactHistory.order_id.in_(order_ids))
        ).all()
        return set(rows)


def mark_orders_contacted(records: list[dict]) -> int:
    if not records:
        return 0
    inserted = 0
    now = datetime.now(timezone.utc)
    with db_session() as session:
        existing = set(
            session.scalars(
                select(ContactHistory.order_id).where(
                    ContactHistory.order_id.in_([r["order_id"] for r in records])
                )
            ).all()
        )
        for record in records:
            if record["order_id"] in existing:
                continue
            session.add(
                ContactHistory(
                    order_id=record["order_id"],
                    phone=record.get("phone", ""),
                    address_hash=record.get("address_hash", ""),
                    contacted_at=now,
                )
            )
            inserted += 1
        session.commit()
    return inserted


def contact_history_stats() -> dict:
    with db_session() as session:
        count, oldest, newest = session.execute(
            select(
                func.count(ContactHistory.id),
                func.min(ContactHistory.contacted_at),
                func.max(ContactHistory.contacted_at),
            )
        ).one()
        return {"count": int(count or 0), "oldest": oldest, "newest": newest}


def reset_contact_history() -> int:
    with db_session() as session:
        count = session.scalar(select(func.count(ContactHistory.id))) or 0
        session.execute(delete(ContactHistory))
        session.commit()
        return int(count)


def confirmed_product_order_ids(product_order_ids: list[str]) -> set[str]:
    if not product_order_ids:
        return set()
    with db_session() as session:
        rows = session.scalars(
            select(NaverConfirmHistory.product_order_id).where(
                NaverConfirmHistory.product_order_id.in_(product_order_ids)
            )
        ).all()
        return set(rows)


def mark_product_orders_confirmed(product_order_ids: list[str]) -> int:
    if not product_order_ids:
        return 0
    with db_session() as session:
        existing = set(
            session.scalars(
                select(NaverConfirmHistory.product_order_id).where(
                    NaverConfirmHistory.product_order_id.in_(product_order_ids)
                )
            ).all()
        )
        now = datetime.now(timezone.utc)
        for pid in product_order_ids:
            if pid not in existing:
                session.add(NaverConfirmHistory(product_order_id=pid, confirmed_at=now))
        session.commit()
    return len(set(product_order_ids) - existing)
