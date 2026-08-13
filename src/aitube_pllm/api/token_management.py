"""PLLM-Token 管理 API"""

import hashlib
import uuid
from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, HTTPException, status, Request
from pydantic import BaseModel, Field

from ..db import db, TokenRepo, AuditRepo, IssuerRepo

router = APIRouter(prefix="/admin/pllm-tokens", tags=["Token Management"])


def _is_local_admin(request: Request) -> bool:
    """判断是否为本地管理台调用（x-local-admin 弱鉴权）。

    该模式下绕过 Ed25519 签名的 issuer 隔离，供内网管理台查看/管理全部 token；
    真实签名请求语义不受影响。
    """
    return request.headers.get("x-local-admin") == "true"


class TokenIssueRequest(BaseModel):
    """Token 签发请求"""
    issuer_id: str = Field(..., max_length=64)
    subject_id: Optional[str] = Field(None, max_length=128)
    name: Optional[str] = None
    rate_limit_rpm: Optional[int] = None
    token_budget: Optional[int] = None
    token_budget_period: Optional[str] = Field(None, pattern="^(daily|monthly|total)$")


class TokenRevokeRequest(BaseModel):
    """Token 吊销请求"""
    pllm_token_id: Optional[uuid.UUID] = None
    issuer_id: Optional[str] = None
    subject_id: Optional[str] = None


class TokenBudgetUpdateRequest(BaseModel):
    """Token 预算更新请求"""
    token_budget: Optional[int] = None
    token_budget_period: Optional[str] = Field(None, pattern="^(daily|monthly|total)$")


@router.post("", status_code=status.HTTP_201_CREATED)
async def issue_token(request: Request, body: TokenIssueRequest):
    """签发新 Token
    
    每次调用都会生成新的 Token（非幂等），使用新的 nonce 防止重放。
    """
    is_admin = _is_local_admin(request)
    issuer_id = request.state.issuer_id if not is_admin else body.issuer_id

    # 验证 issuer_id 与签名头一致（本地管理模式直接用 body.issuer_id）
    if not is_admin and body.issuer_id != issuer_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="issuer_id in body must match X-Issuer-Id header",
        )
    
    async with db.pool.acquire() as conn:
        # 检查是否已存在相同 subject_id 的 active token（警告但不阻止）
        if body.subject_id:
            existing = await TokenRepo.get_active_by_subject(
                conn, issuer_id, body.subject_id
            )
            if existing:
                # 记录警告日志，但允许签发（重复签发允许）
                pass
        
        # 签发新 Token
        token_record, plaintext = await TokenRepo.issue(
            conn,
            issuer_id=body.issuer_id,
            subject_id=body.subject_id,
            name=body.name,
            rate_limit_rpm=body.rate_limit_rpm,
            token_budget=body.token_budget,
            token_budget_period=body.token_budget_period,
        )
        
        # 记录业务审计
        await AuditRepo.record_event(
            conn,
            actor_type="issuer",
            actor_id=issuer_id,
            action="issue",
            target_type="pllm_token",
            target_id=str(token_record["pllm_token_id"]),
            result="success",
            detail={
                "subject_id": body.subject_id,
                "name": body.name,
                "rate_limit_rpm": body.rate_limit_rpm,
                "token_budget": body.token_budget,
                "token_budget_period": body.token_budget_period,
            },
        )
    
    # 返回明文（仅此一次）和 token_id
    return {
        "pllm_token_id": str(token_record["pllm_token_id"]),
        "pllm_token": plaintext,
        "pllm_token_prefix": plaintext[:12] + "...",
        "created_at": token_record["created_at"].isoformat(),
    }


@router.delete("", status_code=status.HTTP_204_NO_CONTENT)
async def revoke_tokens(request: Request, body: TokenRevokeRequest):
    """吊销 Token
    
    可以通过 pllm_token_id 吊销单个，或通过 issuer_id + subject_id 吊销所有。
    """
    is_admin = _is_local_admin(request)
    issuer_id = request.state.issuer_id if not is_admin else None

    # 必须指定一种吊销方式
    if body.pllm_token_id and (body.issuer_id or body.subject_id):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Cannot specify both pllm_token_id and issuer_id/subject_id",
        )
    
    if not body.pllm_token_id and not (body.issuer_id and body.subject_id):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Must specify either pllm_token_id or both issuer_id and subject_id",
        )
    
    async with db.pool.acquire() as conn:
        if body.pllm_token_id:
            # 吊销单个 Token
            token = await TokenRepo.get_by_id(conn, body.pllm_token_id)
            if not token:
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND,
                    detail=f"Token not found: {body.pllm_token_id}",
                )
            
            # 验证权限：只能吊销自己签发的 Token（本地管理模式可吊销任意）
            if not is_admin and token["issuer_id"] != issuer_id:
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail="Cannot revoke tokens issued by other issuers",
                )
            
            revoked = await TokenRepo.revoke_by_id(conn, body.pllm_token_id)
            action_detail = {"pllm_token_id": str(body.pllm_token_id)}
        else:
            # 吊销指定 subject 的所有 Token（本地管理模式不受限）
            if not is_admin and body.issuer_id != issuer_id:
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail="Cannot revoke tokens for other issuers",
                )
            
            revoked = await TokenRepo.revoke_by_subject(
                conn, body.issuer_id, body.subject_id
            )
            action_detail = {
                "issuer_id": body.issuer_id,
                "subject_id": body.subject_id,
                "revoked_count": revoked,
            }
        
        # 记录业务审计
        await AuditRepo.record_event(
            conn,
            actor_type="issuer",
            actor_id=issuer_id,
            action="revoke",
            target_type="pllm_token",
            target_id=str(body.pllm_token_id) if body.pllm_token_id else f"{body.issuer_id}:{body.subject_id}",
            result="success",
            detail=action_detail,
        )
    
    return None


@router.get("")
async def query_tokens(
    request: Request,
    issuer_id: Optional[str] = None,
    subject_id: Optional[str] = None,
    pllm_token_id: Optional[uuid.UUID] = None,
    page: int = 1,
    page_size: int = 20,
):
    """查询 Token
    
    返回 Token 列表（包含已吊销的），但不返回明文。
    """
    request_issuer_id = request.state.issuer_id if not _is_local_admin(request) else None

    # 非本地管理模式：只能查询自己签发的 Token
    if not _is_local_admin(request):
        if issuer_id and issuer_id != request_issuer_id:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Cannot query tokens for other issuers",
            )
        if not issuer_id:
            issuer_id = request_issuer_id
    
    if page_size > 100:
        page_size = 100
    
    async with db.pool.acquire() as conn:
        tokens, total = await TokenRepo.query(
            conn,
            issuer_id=issuer_id,
            subject_id=subject_id,
            pllm_token_id=pllm_token_id,
            page=page,
            page_size=page_size,
        )
    
    # 格式化返回（不返回 hash）
    items = []
    for t in tokens:
        items.append({
            "pllm_token_id": str(t["pllm_token_id"]),
            "issuer_id": t["issuer_id"],
            "subject_id": t["subject_id"],
            "name": t["name"],
            "rate_limit_rpm": t["rate_limit_rpm"],
            "token_budget": t["token_budget"],
            "token_budget_period": t["token_budget_period"],
            "is_active": t["is_active"],
            "created_at": t["created_at"].isoformat(),
            "revoked_at": t["revoked_at"].isoformat() if t["revoked_at"] else None,
        })
    
    return {
        "items": items,
        "total": total,
        "page": page,
        "page_size": page_size,
    }


@router.patch("/{pllm_token_id}")
async def update_token_budget(
    request: Request,
    pllm_token_id: uuid.UUID,
    body: TokenBudgetUpdateRequest,
):
    """更新 Token 预算"""
    is_admin = _is_local_admin(request)
    issuer_id = request.state.issuer_id if not is_admin else None

    # 至少需要指定一个字段
    if body.token_budget is None and body.token_budget_period is None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Must specify at least one of token_budget or token_budget_period",
        )
    
    # 如果指定了 budget，必须同时指定 period
    if body.token_budget is not None and body.token_budget_period is None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="token_budget_period is required when token_budget is specified",
        )
    
    async with db.pool.acquire() as conn:
        # 验证 Token 存在且属于当前 issuer
        token = await TokenRepo.get_by_id(conn, pllm_token_id)
        if not token:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Token not found: {pllm_token_id}",
            )
        
        if not is_admin and token["issuer_id"] != issuer_id:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Cannot update tokens issued by other issuers",
            )
        
        old_budget = token["token_budget"]
        old_period = token["token_budget_period"]
        
        # 更新预算
        updated = await TokenRepo.update_budget(
            conn,
            pllm_token_id,
            token_budget=body.token_budget,
            token_budget_period=body.token_budget_period,
        )
        
        # 记录业务审计
        await AuditRepo.record_event(
            conn,
            actor_type="issuer",
            actor_id=issuer_id,
            action="budget_update",
            target_type="pllm_token",
            target_id=str(pllm_token_id),
            result="success",
            detail={
                "old_budget": old_budget,
                "old_period": old_period,
                "new_budget": body.token_budget,
                "new_period": body.token_budget_period,
            },
        )
    
    return {
        "pllm_token_id": str(updated["pllm_token_id"]),
        "token_budget": updated["token_budget"],
        "token_budget_period": updated["token_budget_period"],
        "updated_at": updated["created_at"].isoformat(),
    }


@router.get("/issuers")
async def list_issuers(request: Request):
    """查询已注册签发者（公钥指纹）。

    仅暴露公钥前缀与 SHA256 指纹用于核对，不返回完整公钥明文。
    x-local-admin 放行，供内网管理台查看已注册的 Ed25519 签发者。
    """
    async with db.pool.acquire() as conn:
        rows = await IssuerRepo.list_all(conn)
    items = []
    for r in rows:
        pk = r.get("public_key") or ""
        items.append({
            "issuer_id": r["issuer_id"],
            "key_id": r["key_id"],
            "is_active": r["is_active"],
            "public_key_prefix": (pk[:16] + "…") if pk else None,
            "public_key_fingerprint": hashlib.sha256(pk.encode()).hexdigest()[:16] if pk else None,
            "created_at": r["created_at"].isoformat() if r["created_at"] else None,
        })
    return {"items": items, "total": len(items)}
