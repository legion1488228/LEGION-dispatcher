from __future__ import annotations

from datetime import date, datetime, time

from sqlalchemy import (
    BigInteger,
    Boolean,
    Date,
    DateTime,
    ForeignKey,
    Integer,
    String,
    Text,
    Time,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from database import Base


class Employee(Base):
    __tablename__ = "employees"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    tg_id: Mapped[int | None] = mapped_column(BigInteger, unique=True, nullable=True, index=True)
    full_name: Mapped[str] = mapped_column(String(160), index=True)
    phone: Mapped[str] = mapped_column(String(40), default="")
    group_code: Mapped[str] = mapped_column(String(30), index=True)  # brigadier/main/cashless/reserve
    height_cm: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    has_car: Mapped[bool] = mapped_column(Boolean, default=False)
    active: Mapped[bool] = mapped_column(Boolean, default=True, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    readiness: Mapped[list[Readiness]] = relationship(back_populates="employee", cascade="all, delete-orphan")
    assignments: Mapped[list[Assignment]] = relationship(back_populates="employee", cascade="all, delete-orphan")


class Agent(Base):
    __tablename__ = "agents"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    tg_id: Mapped[int] = mapped_column(BigInteger, unique=True, index=True)
    full_name: Mapped[str] = mapped_column(String(160))
    phone: Mapped[str] = mapped_column(String(40))
    organization: Mapped[str] = mapped_column(String(120), default="")
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class Order(Base):
    __tablename__ = "orders"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    public_id: Mapped[str] = mapped_column(String(24), unique=True, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, index=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    source: Mapped[str] = mapped_column(String(20), default="private", index=True)  # private/gbu
    work_date: Mapped[date] = mapped_column(Date, index=True)
    issue_time: Mapped[time] = mapped_column(Time)
    arrival_time: Mapped[time | None] = mapped_column(Time, nullable=True)
    deceased_name: Mapped[str] = mapped_column(String(200), index=True)
    route: Mapped[str] = mapped_column(Text, default="")
    category: Mapped[str] = mapped_column(String(20), default="standard", index=True)
    team_size: Mapped[int] = mapped_column(Integer, default=4)
    organization: Mapped[str] = mapped_column(String(120), default="")
    notes: Mapped[str] = mapped_column(Text, default="")
    kickback_rub: Mapped[int] = mapped_column(Integer, default=0)

    agent_tg_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True, index=True)
    agent_name: Mapped[str] = mapped_column(String(160), default="")
    agent_phone: Mapped[str] = mapped_column(String(40), default="")
    other_agent_phone: Mapped[str] = mapped_column(String(40), default="")

    status: Mapped[str] = mapped_column(String(30), default="new", index=True)
    # new / assigning / ready / sent / brigadier_confirmed / closed / cancelled
    finalized_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    sent_to_brigadier_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    brigadier_confirmed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    contact_time: Mapped[str] = mapped_column(String(20), default="")

    assignments: Mapped[list[Assignment]] = relationship(
        back_populates="order", cascade="all, delete-orphan", order_by="Assignment.id"
    )


class Assignment(Base):
    __tablename__ = "assignments"
    __table_args__ = (UniqueConstraint("order_id", "employee_id", name="uq_order_employee"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    order_id: Mapped[int] = mapped_column(ForeignKey("orders.id", ondelete="CASCADE"), index=True)
    employee_id: Mapped[int] = mapped_column(ForeignKey("employees.id", ondelete="CASCADE"), index=True)
    role: Mapped[str] = mapped_column(String(20), default="member")  # brigadier/member
    created_by_tg_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    order: Mapped[Order] = relationship(back_populates="assignments")
    employee: Mapped[Employee] = relationship(back_populates="assignments")


class Readiness(Base):
    __tablename__ = "readiness"
    __table_args__ = (UniqueConstraint("employee_id", "work_date", name="uq_employee_readiness_date"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    employee_id: Mapped[int] = mapped_column(ForeignKey("employees.id", ondelete="CASCADE"), index=True)
    work_date: Mapped[date] = mapped_column(Date, index=True)
    status: Mapped[str] = mapped_column(String(20), index=True)  # ready/not_ready/day_off
    reported_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, index=True)
    raw_text: Mapped[str] = mapped_column(Text, default="")

    employee: Mapped[Employee] = relationship(back_populates="readiness")


class AccessUser(Base):
    __tablename__ = "access_users"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    tg_id: Mapped[int] = mapped_column(BigInteger, unique=True, index=True)
    full_name: Mapped[str] = mapped_column(String(160), default="")
    role: Mapped[str] = mapped_column(String(20), default="assistant")  # owner/assistant
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class ImportLog(Base):
    __tablename__ = "import_logs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    kind: Mapped[str] = mapped_column(String(30), index=True)  # gbu_ocr/agent_text
    created_by_tg_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    raw_text: Mapped[str] = mapped_column(Text, default="")
    result_json: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
