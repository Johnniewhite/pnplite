from datetime import datetime
from typing import Any, Dict, Optional

from pydantic import BaseModel, Field


class BroadcastLog(BaseModel):
    city: str
    message: str
    template_sid: Optional[str] = None
    sent_count: int = 0
    error_count: int = 0
    created_at: datetime = Field(default_factory=datetime.utcnow)
    message_sids: list[str] = Field(default_factory=list)


class MessageStatusLog(BaseModel):
    message_sid: str
    status: str
    to: Optional[str] = None
    error_code: Optional[str] = None
    error_message: Optional[str] = None
    ts: datetime = Field(default_factory=datetime.utcnow)
    raw: Dict[str, Any] = Field(default_factory=dict)
