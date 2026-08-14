"""Ed25519 签名验证中间件 - 用于外部管理 API 认证"""

from __future__ import annotations

import hashlib
import uuid
from datetime import datetime, timezone, timedelta

from fastapi import Request, HTTPException, status
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.responses import JSONResponse, Response

from ..config import settings
from ..db import db, IssuerRepo, NonceRepo, AuditRepo


class SignatureVerificationMiddleware(BaseHTTPMiddleware):
    """外部管理 API 的 Ed25519 签名验证"""

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        # 仅对 /admin/* 路径进行签名验证
        if not request.url.path.startswith("/admin/"):
            return await call_next(request)

        # 放行浏览器 CORS 预检（OPTIONS）。预检请求不含业务签名头，
        # 若在此拦截会导致所有跨域带自定义 header 的请求预检失败（curl 测试无此问题，
        # 故只在浏览器端暴露）。真实请求仍走完整签名校验，安全不受影响。
        if request.method == "OPTIONS":
            return await call_next(request)

        # 仪表盘只读接口与队列路由状态/重载接口使用专用 token 认证，跳过 Ed25519 签名
        if request.url.path.startswith("/admin/dashboard") or request.url.path.startswith("/admin/queue"):
            return await call_next(request)

        # 跳过 localhost CLI 端点（模型管理 / token 管理使用 x-local-admin 认证）
        if request.headers.get("x-local-admin") == "true" and (
            request.url.path.startswith("/admin/models")
            or request.url.path.startswith("/admin/pllm-tokens")
        ):
            return await call_next(request)

        try:
            return await self._verify(request, call_next)
        except HTTPException as e:
            # BaseHTTPMiddleware 中 raise 的 HTTPException 不会走 FastAPI 的
            # exception handler，会冒泡成 500。此处显式转换为 JSON 响应，
            # 保证 401/403/409 等业务状态码正确透传给调用方。
            return JSONResponse(
                status_code=e.status_code,
                content={"detail": e.detail},
                headers=getattr(e, "headers", None),
            )

    async def _verify(
        self, request: Request, call_next: RequestResponseEndpoint
    ) -> Response:
        gateway_request_id = uuid.uuid4()
        occurred_at = datetime.now(timezone.utc)

        # 提取签名相关的请求头
        issuer_id = request.headers.get("x-issuer-id")
        key_id = request.headers.get("x-key-id")
        audience = request.headers.get("x-audience")
        timestamp_str = request.headers.get("x-timestamp")
        nonce = request.headers.get("x-nonce")
        signature_hex = request.headers.get("x-signature")

        # 验证必需的头部
        required_headers = {
            "x-issuer-id": issuer_id,
            "x-key-id": key_id,
            "x-audience": audience,
            "x-timestamp": timestamp_str,
            "x-nonce": nonce,
            "x-signature": signature_hex,
        }
        missing = [k for k, v in required_headers.items() if not v]
        if missing:
            await self._log_security_event(
                gateway_request_id, request, occurred_at,
                decision="rejected",
                issuer_id_claim=issuer_id,
                key_id_claim=key_id,
                reason_code="missing_headers",
                response_status=401,
                detail={"missing_headers": missing},
            )
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail=f"Missing required headers: {', '.join(missing)}",
            )

        # 验证 audience
        if audience != settings.audience:
            await self._log_security_event(
                gateway_request_id, request, occurred_at,
                decision="rejected",
                issuer_id_claim=issuer_id,
                key_id_claim=key_id,
                reason_code="invalid_audience",
                response_status=401,
            )
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail=f"Invalid audience: expected {settings.audience}",
            )

        # 验证 timestamp（允许窗口 ±300 秒）
        try:
            timestamp = datetime.fromisoformat(timestamp_str.replace("Z", "+00:00"))
            now = datetime.now(timezone.utc)
            delta = abs((now - timestamp).total_seconds())
            if delta > settings.signature_timestamp_window:
                await self._log_security_event(
                    gateway_request_id, request, occurred_at,
                    decision="rejected",
                    issuer_id_claim=issuer_id,
                    key_id_claim=key_id,
                    reason_code="timestamp_expired",
                    response_status=401,
                    detail={"delta_seconds": int(delta), "window_seconds": settings.signature_timestamp_window},
                )
                raise HTTPException(
                    status_code=status.HTTP_401_UNAUTHORIZED,
                    detail=f"Timestamp expired: delta={delta:.0f}s exceeds window={settings.signature_timestamp_window}s",
                )
        except ValueError:
            await self._log_security_event(
                gateway_request_id, request, occurred_at,
                decision="rejected",
                issuer_id_claim=issuer_id,
                key_id_claim=key_id,
                reason_code="invalid_timestamp_format",
                response_status=401,
            )
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Invalid timestamp format",
            )

        # 从数据库获取签发者公钥
        async with db.pool.acquire() as conn:
            issuer_record = await IssuerRepo.get_active(issuer_id, key_id, conn)
            if not issuer_record:
                await self._log_security_event(
                    gateway_request_id, request, occurred_at,
                    decision="rejected",
                    issuer_id_claim=issuer_id,
                    key_id_claim=key_id,
                    reason_code="issuer_not_found_or_inactive",
                    response_status=401,
                )
                raise HTTPException(
                    status_code=status.HTTP_401_UNAUTHORIZED,
                    detail=f"Issuer not found or inactive: {issuer_id}:{key_id}",
                )

            # 检查 nonce 是否已使用（防重放）
            if await NonceRepo.is_used(issuer_id, nonce, conn):
                await self._log_security_event(
                    gateway_request_id, request, occurred_at,
                    decision="rejected",
                    issuer_id_claim=issuer_id,
                    key_id_claim=key_id,
                    reason_code="nonce_reused",
                    response_status=401,
                )
                raise HTTPException(
                    status_code=status.HTTP_401_UNAUTHORIZED,
                    detail="Nonce already used (replay attack detected)",
                )

            # 构造签名内容并验证
            # 格式: METHOD\nREQUEST_TARGET\n{X-Audience}\n{X-Timestamp}\n{X-Nonce}\nSHA256(body)
            method = request.method
            request_target = str(request.url.path)
            if request.url.query:
                request_target += f"?{request.url.query}"

            # 读取请求体
            body = await request.body()
            body_hash = hashlib.sha256(body).hexdigest()

            # 构造待签名内容
            sign_content = f"{method}\n{request_target}\n{audience}\n{timestamp_str}\n{nonce}\n{body_hash}"

            # 验证 Ed25519 签名
            try:
                from nacl.signing import VerifyKey
                from nacl.exceptions import BadSignatureError

                public_key_bytes = bytes.fromhex(issuer_record["public_key"])
                verify_key = VerifyKey(public_key_bytes)
                signature_bytes = bytes.fromhex(signature_hex)

                verify_key.verify(sign_content.encode("utf-8"), signature_bytes)
            except (BadSignatureError, ValueError) as e:
                await self._log_security_event(
                    gateway_request_id, request, occurred_at,
                    decision="rejected",
                    issuer_id_claim=issuer_id,
                    key_id_claim=key_id,
                    reason_code="signature_verification_failed",
                    response_status=401,
                    detail={"error": type(e).__name__},
                )
                raise HTTPException(
                    status_code=status.HTTP_401_UNAUTHORIZED,
                    detail="Signature verification failed",
                )

            # 记录 nonce 为已使用
            expires_at = timestamp + timedelta(seconds=settings.signature_timestamp_window * 2)
            await NonceRepo.record(issuer_id, nonce, expires_at, conn)

            # 记录成功的审计事件
            await self._log_security_event(
                gateway_request_id, request, occurred_at,
                decision="accepted",
                issuer_id_claim=issuer_id,
                key_id_claim=key_id,
                reason_code="signature_valid",
                response_status=200,
            )

        # 将 gateway_request_id 和 issuer_id 注入到 request.state 供后续使用
        request.state.gateway_request_id = gateway_request_id
        request.state.issuer_id = issuer_id

        response = await call_next(request)
        return response

    async def _log_security_event(
        self,
        gateway_request_id: uuid.UUID,
        request: Request,
        occurred_at: datetime,
        *,
        decision: str,
        issuer_id_claim: str | None,
        key_id_claim: str | None,
        reason_code: str,
        response_status: int,
        detail: dict | str | None = None,
    ) -> None:
        """记录安全事件审计日志"""
        # 防御性截断：security_event_logs.reason_code 为 VARCHAR(64)，
        # 避免超长 reason_code 导致审计写入失败（曾因 missing_headers 拼接超长被静默吞掉）。
        reason_code = reason_code[:64]
        try:
            async with db.pool.acquire() as conn:
                await AuditRepo.record_security_event(
                    conn,
                    gateway_request_id=gateway_request_id,
                    occurred_at=occurred_at,
                    method=request.method,
                    endpoint=request.url.path,
                    decision=decision,
                    issuer_id_claim=issuer_id_claim,
                    key_id_claim=key_id_claim,
                    source_address=request.client.host if request.client else None,
                    reason_code=reason_code,
                    response_status=response_status,
                    detail=detail,
                )
        except Exception as e:
            # 审计日志记录失败时，根据 fail-closed 原则，应该拒绝请求
            # 但这里已经在处理拒绝流程，所以只记录错误
            import logging
            logger = logging.getLogger(__name__)
            logger.error(f"Failed to record security event: {e}", exc_info=True)
