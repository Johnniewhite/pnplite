from datetime import datetime
from enum import Enum
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field


class MembershipType(str, Enum):
    lifetime = "lifetime"
    monthly = "monthly"
    onetime = "onetime"


class OrderStatus(str, Enum):
    new = "NEW"
    waiting_payment = "WAITING_PAYMENT"
    paid = "PAID"
    dispatched = "DISPATCHED"
    delivered = "DELIVERED"


class MessageDirection(str, Enum):
    inbound = "in"
    outbound = "out"


class Member(BaseModel):
    phone: str
    name: Optional[str] = None
    city: Optional[str] = None
    membership_type: Optional[MembershipType] = None
    status: str = "pending_payment"
    referral_code: Optional[str] = None
    referred_by: Optional[str] = None
    payment_status: str = "pending_review"
    address: Optional[str] = None
    join_date: datetime = Field(default_factory=datetime.utcnow)
    current_cluster_id: Optional[str] = None


class CustomCluster(BaseModel):
    name: str
    owner_phone: str
    max_people: int
    members: List[str] = Field(default_factory=list) # phone numbers
    items: List[dict] = Field(default_factory=list) # shared cart
    created_at: datetime = Field(default_factory=datetime.utcnow)
    is_active: bool = True


class OrderItem(BaseModel):
    sku: str
    qty: int


class Order(BaseModel):
    member_phone: str
    items: List[OrderItem] = Field(default_factory=list)
    raw_text: Optional[str] = None
    total: Optional[float] = None
    status: OrderStatus = OrderStatus.new
    payment_ref: Optional[str] = None
    city: Optional[str] = None
    address: Optional[str] = None
    cycle_date: Optional[str] = None
    slug: Optional[str] = None
    created_at: datetime = Field(default_factory=datetime.utcnow)
    cluster_id: Optional[str] = None
    cluster_name: Optional[str] = None
    cluster_owner_phone: Optional[str] = None
    cluster_members: List[str] = Field(default_factory=list)
    cluster_payments: List[Dict[str, Any]] = Field(default_factory=list)
    cluster_paid_amount_kobo: int = 0


class MessageLog(BaseModel):
    phone: str
    direction: MessageDirection
    body: str
    intent: Optional[str] = None
    state_before: Optional[str] = None
    state_after: Optional[str] = None
    ts: datetime = Field(default_factory=datetime.utcnow)
    ai_used: bool = False
    media_url: Optional[str] = None
