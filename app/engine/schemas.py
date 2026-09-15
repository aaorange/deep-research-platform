from pydantic import BaseModel, Field


class NoteFact(BaseModel):
    claim: str = Field(description="一条关键事实或数据点，尽量含量化数字")
    citation: int = Field(ge=1, description="支撑该事实的信源序号")


class WorkerNote(BaseModel):
    """单子任务的压缩笔记（目标 800 token 内）。"""

    summary: str = Field(description="150-300 字核心发现，事实句末尾标注 [N] 引用")
    facts: list[NoteFact] = Field(default_factory=list, max_length=10)
    gaps: list[str] = Field(default_factory=list, description="材料未覆盖、值得后续补充的问题")


class NoteUsage(BaseModel):
    prompt_tokens: int
    completion_tokens: int
    total_tokens: int
