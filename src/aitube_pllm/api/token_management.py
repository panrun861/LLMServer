"""PLLM-Token 管理 API"""

import hashlib
import uuid
from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, HTTPException, status, Request
from pydantic import BaseModel, Field

from ..db import db, TokenRepo, AuditRepo, IssuerRepo

router = APIRouter(prefix="/admin/pllm-tokens", tags=["Token Management"])


def _require_issuer(request: Request) -> str:
    """要求 Ed25519 签名认证，返回签名者 issuer_id（由中间件注入）。

    取代原 x-local-admin 弱鉴权：token / issuer 管理接口必须通过 Ed25519 签名。
    """
    issuer_id = getattr(request.state, "issuer_id", None)
    if not issuer_id:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Ed25519 signature authentication required",
        )
    return issuer_id


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
    issuer_id = _require_issuer(request)

    # issuer_id 必须与签名头一致
    if body.issuer_id != issuer_id:
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
    issuer_id = _require_issuer(request)

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
            
            # 验证权限：只能吊销自己签发的 Token
            if token["issuer_id"] != issuer_id:
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail="Cannot revoke tokens issued by other issuers",
                )
            
            revoked = await TokenRepo.revoke_by_id(conn, body.pllm_token_id)
            action_detail = {"pllm_token_id": str(body.pllm_token_id)}
        else:
            # 吊销指定 subject 的所有 Token（仅限自己签发的）
            if body.issuer_id != issuer_id:
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
    request_issuer_id = _require_issuer(request)

    # 只能查询自己签发的 Token
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
    issuer_id = _require_issuer(request)

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
        
        if token["issuer_id"] != issuer_id:
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
    需 Ed25519 签名认证。
    """
    _require_issuer(request)
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


class IssuerCreateRequest(BaseModel):
    """注册 Ed25519 签发者（用于管理 API 签名）

    - public_key 不传：由服务端生成 Ed25519 密钥对，返回私钥 seed（仅此一次），公钥入库。
    - public_key 传入：导入用户自带公钥（32 字节 hex），服务端只存公钥。
    """
    issuer_id: str = Field(..., max_length=64, description="签发者唯一标识")
    key_id: str = Field(..., max_length=128, description="密钥标识（同一 issuer 下的 key 版本号）")
    public_key: Optional[str] = Field(None, description="可选。Ed25519 公钥 32 字节 hex；不传则服务端生成")
    force: bool = Field(False, description="issuer_id 已存在时是否覆盖公钥（轮换 key 用）")


@router.post("/issuers", status_code=status.HTTP_201_CREATED)
async def create_issuer(request: Request, body: IssuerCreateRequest):
    """注册新的 Ed25519 签发者公钥。

    需 Ed25519 签名认证。generate 模式返回的 private_key_seed 是签名私钥，
    服务端不存储，请立即保存。
    """
    admin_issuer = _require_issuer(request)

    async with db.pool.acquire() as conn:
        if await IssuerRepo.exists(conn, body.issuer_id):
            if not body.force:
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail=f"issuer_id '{body.issuer_id}' already exists; use force=true to rotate key",
                )

        if body.public_key:
            try:
                raw = bytes.fromhex(body.public_key)
            except ValueError:
                raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="public_key must be hex-encoded")
            if len(raw) != 32:
                raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="public_key must be 32 bytes (64 hex chars)")
            public_key_hex = body.public_key
            private_key_seed = None
            mode = "import"
        else:
            from nacl.signing import SigningKey
            signing_key = SigningKey.generate()
            public_key_hex = signing_key.verify_key.encode().hex()
            private_key_seed = signing_key.encode().hex()
            mode = "generate"

        await IssuerRepo.upsert(conn, body.issuer_id, body.key_id, public_key_hex)
        await AuditRepo.record_event(
            conn, actor_type="issuer", actor_id=admin_issuer,
            action="create_issuer", target_type="issuer", target_id=body.issuer_id,
            result="success", detail={"key_id": body.key_id, "mode": mode, "force": body.force},
        )

    resp = {
        "issuer_id": body.issuer_id,
        "key_id": body.key_id,
        "mode": mode,
        "public_key_fingerprint": hashlib.sha256(public_key_hex.encode()).hexdigest()[:16],
    }
    if mode == "generate":
        resp["public_key"] = public_key_hex
        resp["private_key_seed"] = private_key_seed
        resp["warning"] = "private_key_seed 是签名私钥，服务端不存储，请立即妥善保存"
    return resp


@router.delete("/issuers/{issuer_id}", status_code=status.HTTP_204_NO_CONTENT)
async def revoke_issuer(request: Request, issuer_id: str):
    """吊销（停用）指定签发者。

    吊销后该 issuer 的 Ed25519 签名失效（is_active=FALSE），但已签发的 Bearer token 不受影响。
    需 Ed25519 签名认证。
    """
    admin_issuer = _require_issuer(request)

    async with db.pool.acquire() as conn:
        n = await IssuerRepo.deactivate(conn, issuer_id)
        if n == 0:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"issuer_id '{issuer_id}' not found")
        await AuditRepo.record_event(
            conn, actor_type="issuer", actor_id=admin_issuer,
            action="revoke_issuer", target_type="issuer", target_id=issuer_id,
            result="success", detail={},
        )


class IssuerVerifyRequest(BaseModel):
    """校验 Ed25519 密钥对是否可用且已登记。

    - private_key_seed：generate 模式返回的私钥 seed（hex）。后端派生公钥并做一次自洽签名验签，
      同时比对 issuers 表中已登记的公钥是否一致。
    - public_key：也可只给公钥，核对是否已登记且 active。
    """
    issuer_id: str = Field(..., max_length=64)
    key_id: str = Field(..., max_length=128)
    private_key_seed: Optional[str] = Field(None, description="Ed25519 私钥 seed（hex），generate 模式返回的值")
    public_key: Optional[str] = Field(None, description="或仅提供公钥（hex）核对登记状态")


@router.post("/issuers/verify")
async def verify_issuer(request: Request, body: IssuerVerifyRequest):
    """校验 Ed25519 密钥：私钥自洽 + 与已登记公钥一致 + 是否已 active。

    需 Ed25519 签名认证。用于前端「校验签名」工具，确认密钥对可用来调用管理 API。
    """
    _require_issuer(request)

    async with db.pool.acquire() as conn:
        row = await IssuerRepo.get_active(body.issuer_id, body.key_id, conn)
        if not row:
            return {"registered": False, "active": False, "detail": "issuer/key 不存在或已停用"}

        stored_pk = row["public_key"]
        if body.private_key_seed:
            try:
                from nacl.signing import SigningKey
                sk = SigningKey(bytes.fromhex(body.private_key_seed))
                derived_pk = sk.verify_key.encode().hex()
            except Exception:
                return {"registered": True, "active": row["is_active"], "private_key_valid": False,
                        "detail": "private_key_seed 非合法 hex 或长度不对"}
            # 自洽验签：用私钥签名，再用派生公钥验签
            try:
                msg = b"pllm-ed25519-verify"
                sig = sk.sign(msg)
                sk.verify_key.verify(sig)
                sig_ok = True
            except Exception:
                sig_ok = False
            return {
                "registered": True,
                "active": row["is_active"],
                "private_key_valid": True,
                "signature_self_check": sig_ok,
                "public_key_matches_registered": (derived_pk == stored_pk),
                "derived_public_key": derived_pk,
            }
        if body.public_key:
            return {
                "registered": True,
                "active": row["is_active"],
                "public_key_matches_registered": (body.public_key == stored_pk),
            }
        return {"registered": True, "active": row["is_active"],
                "detail": "请提供 private_key_seed 或 public_key 以完成校验"}
