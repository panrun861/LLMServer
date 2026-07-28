"""推理网关 API - /v1/models 和 /v1/chat/completions"""

import hashlib
import json
import logging
import time
import uuid
from collections import defaultdict, deque
from datetime import datetime, timezone
from typing import Any, Optional

import httpx
from fastapi import APIRouter, HTTPException, status, Request, Depends
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel

from ..config import settings
from ..db import db, TokenRepo, ModelRepo, UsageRepo, AuditRepo

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/v1", tags=["Inference Gateway"])


# ---------------------------------------------------------------------------
# 速率限制器（内存滑动窗口，按 pllm_token_id 计数）
# ---------------------------------------------------------------------------
class _SlidingWindowRateLimiter:
    """简单的内存滑动窗口 RPM 限制器。

    按 pllm_token_id 维护一个 deque，每次请求时清除超过 60 秒的旧条目。
    进程重启后计数归零，对于轻量网关场景可接受；如需跨实例共享可替换为 Redis。
    """

    def __init__(self) -> None:
        self._windows: dict[uuid.UUID, deque[float]] = defaultdict(deque)

    def check(self, token_id: uuid.UUID, rpm: int) -> bool:
        """返回 True 表示允许，False 表示超限。"""
        now = time.monotonic()
        window = self._windows[token_id]
        # 清除 60 秒前的条目
        while window and window[0] < now - 60.0:
            window.popleft()
        if len(window) >= rpm:
            return False
        window.append(now)
        return True


_rate_limiter = _SlidingWindowRateLimiter()


# ---------------------------------------------------------------------------
# Bearer 认证
# ---------------------------------------------------------------------------
class BearerAuth:
    """Bearer Token 认证依赖"""

    async def __call__(self, request: Request) -> dict:
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
            await TokenRepo.touch_last_used(conn, token_record["pllm_token_id"])

        return token_record


bearer_auth = BearerAuth()


# ---------------------------------------------------------------------------
# /v1/models
# ---------------------------------------------------------------------------
@router.get("/models")
async def list_models(token_record: dict = Depends(bearer_auth)):
    """列出可用模型

    从数据库查询当前有效的模型，并附加 context_length 信息。
    """
    async with db.pool.acquire() as conn:
        models = await ModelRepo.list_all(conn)

    enabled_models = [m for m in models if m["is_enabled"]]

    data = []
    for m in enabled_models:
        data.append({
            "id": m["model_name"],
            "object": "model",
            "created": int(m["created_at"].timestamp()),
            "owned_by": "aitube-pllm",
            "context_length": m["context_length"],
            "tier": m["tier"],
            "inference_engine": m["inference_engine"],
        })

    return {
        "object": "list",
        "data": data,
    }


# ---------------------------------------------------------------------------
# Chat Completion 请求模型
# ---------------------------------------------------------------------------
class ChatCompletionRequest(BaseModel):
    """Chat Completion 请求（OpenAI 兼容）"""
    model: str
    messages: list[dict]
    temperature: Optional[float] = 1.0
    top_p: Optional[float] = 1.0
    n: Optional[int] = 1
    stream: Optional[bool] = False
    stop: Optional[list[str]] = None
    max_tokens: Optional[int] = None
    presence_penalty: Optional[float] = 0.0
    frequency_penalty: Optional[float] = 0.0
    user: Optional[str] = None
    # Function calling
    tools: Optional[list[dict]] = None
    tool_choice: Optional[Any] = None  # str | dict | None
    # Structured output
    response_format: Optional[dict] = None
    # Reproducibility
    seed: Optional[int] = None
    # Advanced
    logit_bias: Optional[dict[str, float]] = None
    logprobs: Optional[bool] = None
    top_logprobs: Optional[int] = None
    # Stream options (client-customizable; overridden for include_usage)
    stream_options: Optional[dict] = None


# ---------------------------------------------------------------------------
# 辅助：记录 usage + 审计
# ---------------------------------------------------------------------------
async def _record_usage(
    *,
    request_id: uuid.UUID,
    token_record: dict,
    model_config: dict,
    model_name: str,
    prompt_tokens: int,
    completion_tokens: int,
    total_tokens: int,
    latency_ms: int,
    status_code: int,
    correlation_id: str | None,
) -> None:
    """记录 usage_logs 并更新 usage_counters。"""
    try:
        async with db.pool.acquire() as conn:
            await UsageRepo.record(
                conn,
                request_id=request_id,
                pllm_token_ref_id=token_record["pllm_token_id"],
                pllm_token_id_snapshot=token_record["pllm_token_id"],
                issuer_id_snapshot=token_record["issuer_id"],
                subject_id_snapshot=token_record["subject_id"],
                model=model_name,
                tier_snapshot=model_config["tier"],
                inference_engine_snapshot=model_config["inference_engine"],
                prompt_tokens=prompt_tokens,
                completion_tokens=completion_tokens,
                total_tokens=total_tokens,
                latency_ms=latency_ms,
                status_code=status_code,
                correlation_id=correlation_id,
            )
            if total_tokens > 0:
                await UsageRepo.increment_counters(
                    conn,
                    token_record["pllm_token_id"],
                    total_tokens,
                )
    except Exception:
        logger.exception("Failed to record usage for request_id=%s", request_id)


async def _record_inference_audit(
    *,
    request_id: uuid.UUID,
    token_record: dict,
    model_name: str,
    status_code: int,
    result: str,
) -> None:
    """记录推理请求的业务审计日志到 event_logs。"""
    try:
        async with db.pool.acquire() as conn:
            await AuditRepo.record_event(
                conn,
                actor_type="pllm_token",
                actor_id=str(token_record["pllm_token_id"]),
                action="inference_request",
                target_type="model",
                target_id=model_name,
                result=result,
                detail=json.dumps({
                    "request_id": str(request_id),
                    "status_code": status_code,
                }),
            )
    except Exception:
        logger.exception(
            "Failed to record inference audit for request_id=%s", request_id,
        )


# ---------------------------------------------------------------------------
# /v1/chat/completions
# ---------------------------------------------------------------------------
@router.post("/chat/completions")
async def chat_completions(
    request: Request,
    body: ChatCompletionRequest,
    token_record: dict = Depends(bearer_auth),
):
    """Chat Completions API

    代理到 LiteLLM，并记录使用量。
    """
    request_id = uuid.uuid4()
    start_time = time.time()

    # X-Correlation-Id 长度校验（设计文档 §1.3: 超过 255 字符返回 422）
    correlation_id = request.headers.get("x-correlation-id")
    if correlation_id is not None and len(correlation_id) > 255:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="X-Correlation-Id exceeds 255 characters",
        )

    # 从数据库查询模型配置
    async with db.pool.acquire() as conn:
        model_config = await ModelRepo.get_current(conn, body.model)
        if not model_config:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Model not found: {body.model}",
            )

        # is_enabled 检查：禁用的模型不可调用
        if not model_config["is_enabled"]:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Model not found: {body.model}",
            )

        # 速率限制检查（内存滑动窗口）
        rpm = token_record["rate_limit_rpm"]
        if rpm and not _rate_limiter.check(token_record["pllm_token_id"], rpm):
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail=f"Rate limit exceeded: {rpm} requests per minute",
            )

        # 预算检查
        if token_record["token_budget"] and token_record["token_budget_period"]:
            used = await UsageRepo.get_used_tokens(
                conn,
                token_record["pllm_token_id"],
                token_record["token_budget_period"],
            )
            if used >= token_record["token_budget"]:
                raise HTTPException(
                    status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                    detail=(
                        f"Token budget exceeded: {used}/{token_record['token_budget']}"
                    ),
                )

    # 构造请求体：模型 request_params 作为默认值，客户端参数优先覆盖
    request_body = body.model_dump(exclude_none=True)
    if model_config["request_params"]:
        # 先以模型参数为底，再用客户端显式传入的值覆盖
        merged = {**model_config["request_params"], **request_body}
        request_body = merged

    # 流式响应强制包含 usage（客户端不可关闭）
    if body.stream:
        stream_opts = request_body.get("stream_options") or {}
        stream_opts["include_usage"] = True
        request_body["stream_options"] = stream_opts

    # 转发到 LiteLLM
    litellm_url = f"{settings.litellm_api_base}/v1/chat/completions"
    headers = {
        "Authorization": f"Bearer {settings.litellm_master_key}",
        "Content-Type": "application/json",
    }

    try:
        async with httpx.AsyncClient(timeout=120.0) as client:
            if body.stream:
                # ---- 流式响应 ----
                async def stream_generator():
                    prompt_tokens = 0
                    completion_tokens = 0
                    total_tokens = 0
                    upstream_status = 200

                    try:
                        async with client.stream(
                            "POST",
                            litellm_url,
                            json=request_body,
                            headers=headers,
                        ) as response:
                            upstream_status = response.status_code
                            if response.status_code != 200:
                                error_body = await response.aread()
                                raise HTTPException(
                                    status_code=response.status_code,
                                    detail=error_body.decode(),
                                )

                            async for line in response.aiter_lines():
                                if line.startswith("data: "):
                                    yield line + "\n\n"

                                    if line == "data: [DONE]":
                                        break

                                    try:
                                        data = json.loads(line[6:])
                                        if "usage" in data:
                                            usage = data["usage"]
                                            prompt_tokens = usage.get(
                                                "prompt_tokens", 0
                                            )
                                            completion_tokens = usage.get(
                                                "completion_tokens", 0
                                            )
                                            total_tokens = usage.get(
                                                "total_tokens", 0
                                            )
                                    except json.JSONDecodeError:
                                        pass
                    finally:
                        # 无论正常结束还是客户端断开，都记录 usage
                        latency_ms = int((time.time() - start_time) * 1000)
                        await _record_usage(
                            request_id=request_id,
                            token_record=token_record,
                            model_config=model_config,
                            model_name=body.model,
                            prompt_tokens=prompt_tokens,
                            completion_tokens=completion_tokens,
                            total_tokens=total_tokens,
                            latency_ms=latency_ms,
                            status_code=upstream_status,
                            correlation_id=correlation_id,
                        )
                        await _record_inference_audit(
                            request_id=request_id,
                            token_record=token_record,
                            model_name=body.model,
                            status_code=upstream_status,
                            result="success"
                            if upstream_status == 200
                            else "error",
                        )

                return StreamingResponse(
                    stream_generator(),
                    media_type="text/event-stream",
                    headers={"X-Request-Id": str(request_id)},
                )

            else:
                # ---- 非流式响应 ----
                response = await client.post(
                    litellm_url,
                    json=request_body,
                    headers=headers,
                )

                if response.status_code != 200:
                    # 上游错误也记录审计
                    await _record_inference_audit(
                        request_id=request_id,
                        token_record=token_record,
                        model_name=body.model,
                        status_code=response.status_code,
                        result="error",
                    )
                    raise HTTPException(
                        status_code=response.status_code,
                        detail=response.text,
                    )

                result = response.json()

                # 提取 usage
                usage = result.get("usage", {})
                prompt_tokens = usage.get("prompt_tokens", 0)
                completion_tokens = usage.get("completion_tokens", 0)
                total_tokens = usage.get("total_tokens", 0)

                # 记录使用量
                latency_ms = int((time.time() - start_time) * 1000)
                await _record_usage(
                    request_id=request_id,
                    token_record=token_record,
                    model_config=model_config,
                    model_name=body.model,
                    prompt_tokens=prompt_tokens,
                    completion_tokens=completion_tokens,
                    total_tokens=total_tokens,
                    latency_ms=latency_ms,
                    status_code=200,
                    correlation_id=correlation_id,
                )
                await _record_inference_audit(
                    request_id=request_id,
                    token_record=token_record,
                    model_name=body.model,
                    status_code=200,
                    result="success",
                )

                # 返回结果，通过响应头附加 request_id
                result["request_id"] = str(request_id)
                return JSONResponse(
                    content=result,
                    headers={"X-Request-Id": str(request_id)},
                )

    except httpx.TimeoutException:
        await _record_inference_audit(
            request_id=request_id,
            token_record=token_record,
            model_name=body.model,
            status_code=504,
            result="error",
        )
        raise HTTPException(
            status_code=status.HTTP_504_GATEWAY_TIMEOUT,
            detail="LiteLLM request timeout",
        )
    except httpx.RequestError as e:
        await _record_inference_audit(
            request_id=request_id,
            token_record=token_record,
            model_name=body.model,
            status_code=502,
            result="error",
        )
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"LiteLLM request failed: {e!s}",
        )


# ---------------------------------------------------------------------------
# /v1/usage
# ---------------------------------------------------------------------------
@router.get("/usage")
async def get_usage(
    days: int = 7,
    token_record: dict = Depends(bearer_auth),
):
    """查询使用量统计"""
    if days > 30:
        days = 30

    async with db.pool.acquire() as conn:
        summary = await UsageRepo.get_usage_summary(
            conn,
            token_record["pllm_token_id"],
            days,
        )

    return summary
