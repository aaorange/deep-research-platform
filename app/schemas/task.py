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


class SourceOut(BaseModel):
    id: int
    url: str
    title: str | None
    domain: str | None
    credibility: int | None
    freshness: float | None

    model_config = {"from_attributes": True}


class ReportSourceOut(BaseModel):
    """报告阅读页信源卡：展示编号 + 信源元数据 + 从笔记提取的摘录。"""

    no: int = Field(description="报告中的展示编号 [n]")
    id: int = Field(description="信源库 id")
    url: str
    title: str | None
    domain: str | None
    credibility: int | None
    freshness: float | None
    excerpts: list[str] = Field(default_factory=list)


class ReportOut(BaseModel):
    report_id: int
    task_id: int
    question: str
    depth: str
    markdown: str
    citation_map: dict[str, int] = Field(description="展示编号 → 信源库 id")
    token_total: int
    cost_cny: float
    sources: list[ReportSourceOut] = Field(description="按展示编号排序的被引用信源")
    chart_specs: list[dict] = Field(default_factory=list, description="ECharts 图表规格")


class ChatCreate(BaseModel):
    text: str = Field(min_length=2, max_length=2000)


class ChatMessageOut(BaseModel):
    id: int
    role: str
    content: str
    cited_source_ids: list[int] = Field(default_factory=list)
    created_at: datetime

    model_config = {"from_attributes": True}


class StatsModelUsage(BaseModel):
    model: str
    role: str
    prompt_tokens: int
    completion_tokens: int
    tokens: int
    cost_cny: float
    calls: int


class StatsDailyPoint(BaseModel):
    date: str
    cost_cny: float
    tokens: int
    by_model: dict[str, float] = {}


class StatsTaskPoint(BaseModel):
    id: int
    question: str
    depth: str
    status: str
    cost_cny: float
    token_used: int
    created_at: str | None


class StatsOut(BaseModel):
    days: int
    summary: dict
    cache: dict
    by_model: list[StatsModelUsage]
    daily: list[StatsDailyPoint]
    avg_by_depth: dict
    tasks: list[StatsTaskPoint]
    pricing_note: str


def now_utc() -> datetime:
    return datetime.now(UTC)
