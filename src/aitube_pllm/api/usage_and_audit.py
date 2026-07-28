"""使用统计API和审计日志API"""

import hashlib
import uuid
from datetime import datetime, timedelta, timezone
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status

from ..db import db, TokenRepo, UsageRepo, AuditRepo

router = APIRouter(tags=["Usage & Audit"])


# ========== 依赖注入 ==========

async def get_bearer_token(request: Request) -> dict:
    """从 Bearer Token 获取 token 记录"""
    auth_header = request.headers.get("authorization")
    if not auth_header or not auth_header.startswith("Bearer "):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing or invalid Authorization header",
        )
    
    token = auth_header[7:]
    token_hash = hashlib.sha256(token.encode()).hexdigest()
    
    async with db.pool.acquire() as conn:
        token_record = await TokenRepo.get_by_hash(conn, token_hash)
        if not token_record:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Invalid or expired token",
            )
    
    return token_record


async def require_external_admin(request: Request) -> str:
    """要求外部管理员认证（通过签名验证中间件已处理）"""
    issuer_id = getattr(request.state, "issuer_id", None)
    if not issuer_id:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="External admin authentication required",
        )
    return issuer_id


# ========== 使用统计API ==========


@router.get("/v1/usage")
async def get_usage_summary(
    days: int = Query(7, ge=1, le=30),
    token_record: dict = Depends(get_bearer_token),
):
    """获取使用量统计
    
    返回指定天数内的使用量统计，按日期和模型分组。
    仅返回当前Token的使用量。
    """
    async with db.pool.acquire() as conn:
        summary = await UsageRepo.get_usage_summary(
            conn,
            token_record["pllm_token_id"],
            days,
        )
    
    return summary


@router.get("/admin/usage-records")
async def get_usage_records(
    request: Request,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    pllm_token_id: Optional[uuid.UUID] = None,
    issuer_id: Optional[str] = None,
    subject_id: Optional[str] = None,
    model: Optional[str] = None,
    tier: Optional[str] = None,
    from_time: Optional[datetime] = None,
    to_time: Optional[datetime] = None,
    admin_issuer: str = Depends(require_external_admin),
):
    """获取使用量记录列表
    
    支持多种筛选条件，返回详细的使用量记录。
    仅外部管理API可调用。
    """
    # 设置默认时间范围（最近30天）
    now = datetime.now(timezone.utc)
    if from_time is None:
        from_time = now - timedelta(days=30)
    if to_time is None:
        to_time = now
    
    async with db.pool.acquire() as conn:
        records, total = await UsageRepo.query_records(
            conn,
            page=page,
            page_size=page_size,
            pllm_token_id_snapshot=pllm_token_id,
            issuer_id_snapshot=issuer_id,
            subject_id_snapshot=subject_id,
            model=model,
            tier_snapshot=tier,
            from_time=from_time,
            to_time=to_time,
        )
    
    return {
        "items": records,
        "total": total,
        "page": page,
        "page_size": page_size,
    }


# ========== 审计日志API ==========


@router.get("/admin/audit-events")
async def get_audit_events(
    request: Request,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    event_type: Optional[str] = Query(None, pattern="^(security_decision|business_result)$"),
    issuer_id: Optional[str] = None,
    from_time: Optional[datetime] = None,
    to_time: Optional[datetime] = None,
    decision: Optional[str] = Query(None, pattern="^(accepted|rejected)$"),
    result: Optional[str] = Query(None, pattern="^(success|failure)$"),
    admin_issuer: str = Depends(require_external_admin),
):
    """获取审计事件列表
    
    联合查询 security_event_logs 和 event_logs，返回统一的审计事件。
    支持多种筛选条件。
    仅外部管理API可调用。
    """
    # 设置默认时间范围（最近30天）
    now = datetime.now(timezone.utc)
    if from_time is None:
        from_time = now - timedelta(days=30)
    if to_time is None:
        to_time = now
    
    # 验证时间范围不超过30天
    if (to_time - from_time).days > 30:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Time range cannot exceed 30 days",
        )
    
    async with db.pool.acquire() as conn:
        events, total = await AuditRepo.query_audit_events(
            conn,
            page=page,
            page_size=page_size,
            event_type=event_type,
            from_time=from_time,
            to_time=to_time,
            issuer_id=issuer_id,
            decision=decision,
            result=result,
        )
    
    return {
        "items": events,
        "total": total,
        "page": page,
        "page_size": page_size,
    }
