"""Learn: schemas define HTTP data contracts, not database tables or permissions.

Compare TicketInput with db/models/support.py and the preview tests in test_main.py.
"""

from datetime import datetime
from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator


class TicketStatus(StrEnum):
    OPEN = "open"
    IN_PROGRESS = "in_progress"
    RESOLVED = "resolved"
    CLOSED = "closed"


class TicketInput(BaseModel):
    # Learn: normalize input and reject unknown fields before the use case runs.
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")
    priority: Literal["low", "medium", "high"] = Field(default="medium")
    subject: str = Field(min_length=1, max_length=120)
    message: str = Field(min_length=10, max_length=4000)
    customer_reference: str | None = Field(
        default=None,
        min_length=1,
        max_length=64,
    )

    @field_validator("priority")
    def validate_priority(cls, value):
        if value not in {"low", "medium", "high"}:
            raise ValueError("Invalid priority")
        return value


class TicketPreview(BaseModel):
    subject: str
    message: str
    customer_reference: str | None
    status: Literal["validated"]


class TicketResponse(BaseModel):
    # Read ORM attributes while exposing only fields in this public response model.
    model_config = ConfigDict(from_attributes=True)

    id: int
    subject: str
    message: str
    customer_reference: str | None
    status: TicketStatus
    version: int
    created_at: datetime


class TicketPage(BaseModel):
    items: list[TicketResponse]
    next_cursor: int | None


class TicketStatusUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: TicketStatus
    expected_version: int = Field(ge=1)
