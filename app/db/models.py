import enum

from sqlalchemy import (
    BigInteger,
    DateTime,
    Enum,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base


class TaskStatus(enum.StrEnum):
    queued = "queued"
    running = "running"
    paused = "paused"
    done = "done"
    failed = "failed"
    canceled = "canceled"
    stopped = "stopped"


class SubTaskStatus(enum.StrEnum):
    pending = "pending"
    running = "running"
    done = "done"
    failed = "failed"
    skipped = "skipped"


class EventType(enum.StrEnum):
    plan = "plan"
    search = "search"
    fetch = "fetch"
    note = "note"
    reflect = "reflect"
    degrade = "degrade"
    budget = "budget"
    synthesize = "synthesize"
    control = "control"


class ResearchTask(Base):
    __tablename__ = "research_tasks"

    id: Mapped[int] = mapped_column(primary_key=True)
    question: Mapped[str] = mapped_column(String(500))
    background: Mapped[str | None] = mapped_column(String(1000), default=None)
    depth: Mapped[str] = mapped_column(String(16), default="std")  # quick/std/deep
    status: Mapped[str] = mapped_column(
        Enum(TaskStatus, name="task_status"), default=TaskStatus.queued
    )
    thread_id: Mapped[str | None] = mapped_column(String(64), default=None)
    token_budget: Mapped[int] = mapped_column(Integer, default=0)
    token_used: Mapped[int] = mapped_column(Integer, default=0)
    cost_cny: Mapped[float] = mapped_column(Float, default=0.0)
    error_msg: Mapped[str | None] = mapped_column(Text, default=None)
    created_at: Mapped[DateTime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[DateTime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )
    finished_at: Mapped[DateTime | None] = mapped_column(DateTime(timezone=True), default=None)

    sub_tasks: Mapped[list["SubTask"]] = relationship(back_populates="task")
    events: Mapped[list["AgentEvent"]] = relationship(back_populates="task")
    sources: Mapped[list["Source"]] = relationship(back_populates="task")
    notes: Mapped[list["Note"]] = relationship(back_populates="task")
    reports: Mapped[list["Report"]] = relationship(back_populates="task")


class SubTask(Base):
    __tablename__ = "sub_tasks"

    id: Mapped[int] = mapped_column(primary_key=True)
    task_id: Mapped[int] = mapped_column(ForeignKey("research_tasks.id"), index=True)
    title: Mapped[str] = mapped_column(String(200))
    keywords: Mapped[str | None] = mapped_column(Text, default=None)
    status: Mapped[str] = mapped_column(
        Enum(SubTaskStatus, name="subtask_status"), default=SubTaskStatus.pending
    )
    round_no: Mapped[int] = mapped_column(Integer, default=1)
    error_msg: Mapped[str | None] = mapped_column(Text, default=None)

    task: Mapped[ResearchTask] = relationship(back_populates="sub_tasks")


class AgentEvent(Base):
    __tablename__ = "agent_events"
    __table_args__ = (
        Index("ix_agent_events_task_seq", "task_id", "seq", unique=True),
        Index("ix_agent_events_payload_gin", "payload", postgresql_using="gin"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    task_id: Mapped[int] = mapped_column(ForeignKey("research_tasks.id"), index=True)
    sub_task_id: Mapped[int | None] = mapped_column(ForeignKey("sub_tasks.id"), default=None)
    seq: Mapped[int] = mapped_column(Integer)
    type: Mapped[str] = mapped_column(Enum(EventType, name="event_type"))
    payload: Mapped[dict] = mapped_column(JSONB, default=dict)
    tokens: Mapped[int | None] = mapped_column(Integer, default=None)
    latency_ms: Mapped[int | None] = mapped_column(Integer, default=None)
    created_at: Mapped[DateTime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    task: Mapped[ResearchTask] = relationship(back_populates="events")


class Source(Base):
    __tablename__ = "sources"

    id: Mapped[int] = mapped_column(primary_key=True)
    task_id: Mapped[int] = mapped_column(ForeignKey("research_tasks.id"), index=True)
    url: Mapped[str] = mapped_column(String(1000))
    title: Mapped[str | None] = mapped_column(String(300), default=None)
    domain: Mapped[str | None] = mapped_column(String(200), default=None)
    credibility: Mapped[int | None] = mapped_column(Integer, default=None)
    freshness: Mapped[str | None] = mapped_column(Float, default=None)
    content_hash: Mapped[str | None] = mapped_column(String(64), index=True)

    task: Mapped[ResearchTask] = relationship(back_populates="sources")


class Note(Base):
    __tablename__ = "notes"

    id: Mapped[int] = mapped_column(primary_key=True)
    task_id: Mapped[int] = mapped_column(ForeignKey("research_tasks.id"), index=True)
    sub_task_id: Mapped[int | None] = mapped_column(ForeignKey("sub_tasks.id"), index=True)
    source_id: Mapped[int | None] = mapped_column(ForeignKey("sources.id"), index=True)
    content: Mapped[str] = mapped_column(Text)

    task: Mapped[ResearchTask] = relationship(back_populates="notes")


class Report(Base):
    __tablename__ = "reports"

    id: Mapped[int] = mapped_column(primary_key=True)
    task_id: Mapped[int] = mapped_column(ForeignKey("research_tasks.id"), index=True)
    version: Mapped[int] = mapped_column(Integer, default=1)
    markdown: Mapped[str] = mapped_column(Text)
    chart_specs: Mapped[dict | None] = mapped_column(JSONB, default=None)
    token_total: Mapped[int] = mapped_column(Integer, default=0)

    task: Mapped[ResearchTask] = relationship(back_populates="reports")


class EvalRun(Base):
    __tablename__ = "eval_runs"

    id: Mapped[int] = mapped_column(primary_key=True)
    question: Mapped[str] = mapped_column(String(500))
    report_id: Mapped[int | None] = mapped_column(
        ForeignKey("reports.id"), default=None, index=True
    )
    coverage: Mapped[float] = mapped_column(Float, default=0.0)
    accuracy: Mapped[float] = mapped_column(Float, default=0.0)
    citation: Mapped[float] = mapped_column(Float, default=0.0)
    judge_model: Mapped[str] = mapped_column(String(64))
    details: Mapped[dict | None] = mapped_column(JSONB, default=None)
    created_at: Mapped[DateTime] = mapped_column(DateTime(timezone=True), server_default=func.now())
