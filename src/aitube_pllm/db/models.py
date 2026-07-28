"""数据库实体模型"""

from datetime import datetime
from uuid import UUID
from pydantic import BaseModel, Field
from typing import Optional, Any


class Issuer(BaseModel):
    """签发者公钥"""
    issuer_id: str = Field(max_length=64)
    key_id: str = Field(max_length=128)
    public_key: str
    is_active: bool = True
    created_at: datetime


class PLLMToken(BaseModel):
    """PLLM Token"""
    id: int
    pllm_token_id: UUID
    issuer_id: str = Field(max_length=64)
    subject_id: Optional[str] = Field(None, max_length=128)
    pllm_token_hash: str = Field(max_length=64)
    name: Optional[str] = None
    rate_limit_rpm: Optional[int] = None
    token_budget: Optional[int] = None
    token_budget_period: Optional[str] = None
    is_active: bool = True
    created_at: datetime
    revoked_at: Optional[datetime] = None


class UsedNonce(BaseModel):
    """已使用的nonce"""
    issuer_id: str = Field(max_length=64)
    nonce: str = Field(max_length=128)
    created_at: datetime
    expires_at: datetime


class Model(BaseModel):
    """模型登记"""
    id: int
    model_name: str = Field(max_length=128)
    tier: str = Field(default='medium', max_length=32)
    model_artifact: str = Field(max_length=255)
    inference_engine: str = Field(max_length=64)
    context_length: int
    api_base: Optional[str] = None
    runtime_params: Optional[dict[str, Any]] = None
    request_params: Optional[dict[str, Any]] = None
    is_current: bool = False
    is_enabled: bool = True
    sync_status: str = Field(default='pending', max_length=32)
    last_synced_at: Optional[datetime] = None
    created_at: datetime
    updated_at: datetime


class UsageLog(BaseModel):
    """使用量明细"""
    id: int
    request_id: UUID
    pllm_token_ref_id: Optional[int] = None
    pllm_token_id_snapshot: UUID
    issuer_id_snapshot: str = Field(max_length=64)
    subject_id_snapshot: Optional[str] = None
    model: str = Field(max_length=128)
    tier_snapshot: Optional[str] = None
    inference_engine_snapshot: Optional[str] = None
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0
    latency_ms: int
    status_code: int
    correlation_id: Optional[str] = None
    created_at: datetime


class UsageCounter(BaseModel):
    """使用量计数器"""
    id: int
    pllm_token_id: UUID
    period_type: str = Field(max_length=16)
    period_start: datetime
    used_tokens: int = 0
    updated_at: datetime


class EventLog(BaseModel):
    """业务操作审计日志"""
    id: int
    event_id: UUID
    actor_type: str = Field(max_length=32)
    actor_id: Optional[str] = None
    action: str = Field(max_length=64)
    target_type: Optional[str] = None
    target_id: Optional[str] = None
    detail: Optional[dict[str, Any]] = None
    result: str = Field(max_length=32)
    created_at: datetime


class SecurityEventLog(BaseModel):
    """安全事件审计日志"""
    id: int
    security_event_id: UUID
    gateway_request_id: UUID
    occurred_at: datetime
    method: str = Field(max_length=10)
    endpoint: str = Field(max_length=255)
    decision: str = Field(max_length=16)
    issuer_id_claim: Optional[str] = None
    key_id_claim: Optional[str] = None
    source_address: Optional[str] = None
    reason_code: str = Field(max_length=64)
    response_status: Optional[int] = None
    detail: Optional[dict[str, Any]] = None
