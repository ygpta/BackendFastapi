from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class StrictInput(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class ToolRequest(StrictInput):
    name: Literal["resolve_ticket"]
    ticket_id: int = Field(ge=1)
    expected_version: int = Field(ge=1)


class RunCreate(StrictInput):
    message: str = Field(min_length=1, max_length=4000)
    conversation_id: UUID | None = None
    prompt_id: str | None = Field(default=None, min_length=1, max_length=64)
    use_rag: bool = True
    structured_output: bool = False
    tool: ToolRequest | None = None


class GeneratedAnswer(StrictInput):
    answer: str = Field(min_length=1, max_length=20000)
    citations: list[str] = Field(default_factory=list, max_length=10)


class RunResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    status: str
    version: int
    output: dict | None
    error_code: str | None
    cancel_requested: bool
    created_at: datetime


class BatchRunCreate(StrictInput):
    items: list[RunCreate] = Field(min_length=1, max_length=10)


class BatchRunResult(BaseModel):
    index: int
    run: RunResponse | None = None
    error: str | None = None


class Approval(StrictInput):
    expected_version: int = Field(ge=1)
    approved: bool


class GrantInput(StrictInput):
    enabled: bool


class DocumentInput(StrictInput):
    title: str = Field(min_length=1, max_length=200)
    content: str = Field(min_length=1, max_length=16000)


class DocumentResponse(DocumentInput):
    model_config = ConfigDict(from_attributes=True)
    id: str


class PromptInput(StrictInput):
    name: str = Field(min_length=1, max_length=120)
    content: str = Field(min_length=1, max_length=4000)


class PromptResponse(PromptInput):
    model_config = ConfigDict(from_attributes=True)
    id: str
    active: bool


class FeedbackInput(StrictInput):
    run_id: str = Field(min_length=1, max_length=64)
    score: int = Field(ge=1, le=5)
    comment: str = Field(default="", max_length=2000)


class EvaluationCase(StrictInput):
    run_id: str = Field(min_length=1, max_length=64)
    required: list[str] = Field(default_factory=list, max_length=20)
    forbidden: list[str] = Field(default_factory=list, max_length=20)
    require_citations: bool = False


class EvaluationInput(StrictInput):
    name: str = Field(min_length=1, max_length=120)
    cases: list[EvaluationCase] = Field(min_length=1, max_length=100)


class BudgetInput(StrictInput):
    allowance_microusd: int = Field(ge=0, le=1000000000000)
