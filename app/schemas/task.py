from datetime import UTC, datetime

from pydantic import BaseModel, Field


class TaskCreate(BaseModel):
    question: str = Field(min_length=2, max_length=200)
    background: str | None = Field(default=None, max_length=500)
    depth: str = Field(default="std", pattern="^(quick|std|deep)$")


class TaskOut(BaseModel):
    id: int
    question: str
    background: str | None
    depth: str
    status: str
    token_budget: int
    token_used: int
    cost_cny: float
    error_msg: str | None
    created_at: datetime
    finished_at: datetime | None

    model_config = {"from_attributes": True}


class SubTaskOut(BaseModel):
    id: int
    title: str
    keywords: str | None
    status: str
    round_no: int

    model_config = {"from_attributes": True}


class TaskDetail(TaskOut):
    sub_tasks: list[SubTaskOut] = []
    instructions: list[dict] = Field(default_factory=list, description="追加指示（含已消费轮次）")


class TaskControl(BaseModel):
    action: str = Field(pattern="^(pause|resume|stop)$")


class InstructionCreate(BaseModel):
    text: str = Field(
        min_length=2, max_length=500, description="追加指示：下一轮反思消费并转为补搜子任务"
    )


class InstructionOut(BaseModel):
    id: int
    text: str
    created_at: datetime
    consumed_round: int | None = None


def now_utc() -> datetime:
    return datetime.now(UTC)
