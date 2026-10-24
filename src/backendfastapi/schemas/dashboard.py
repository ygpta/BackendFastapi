from datetime import datetime

from pydantic import BaseModel


class TicketCounts(BaseModel):
    total: int
    open: int
    in_progress: int
    resolved: int
    closed: int
    created_last_24h: int


class DashboardSummary(BaseModel):
    tickets: TicketCounts
    conversations: int
    messages: int
    chat_enabled: bool
    generated_at: datetime
