from datetime import UTC, datetime

from pydantic import BaseModel, Field


class TaskCreate(BaseModel):
    question: str = Field(min_length=2, max_length=200)
    background: str | None = Field(default=None, max_length=500)
    depth: str = Field(default="std", pattern="^(quick|std|deep)$")
    token_budget: int | None = Field(
        default=None,
        ge=1000,
        le=1_000_000,
        description="token 预算覆盖（缺省用深度档位默认值）；消耗达 80% 触发降级",
    )


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


class EventOut(BaseModel):
    id: int
    seq: int
    task_id: int
    sub_task_id: int | None
    type: str
    payload: dict
    tokens: int | None
    latency_ms: int | None
    created_at: datetime | None

    model_config = {"from_attributes": True}


def now_utc() -> datetime:
    return datetime.now(UTC)
