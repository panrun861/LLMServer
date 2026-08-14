"""推理网关 API - /v1/models 和 /v1/chat/completions"""

import asyncio
import hashlib
import json
import logging
import time
import uuid
from collections import defaultdict, deque
from datetime import datetime, timezone
from typing import Any, Optional, Union

import httpx
from fastapi import APIRouter, HTTPException, status, Request, Depends
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, ConfigDict

from ..config import settings
from ..db import db, TokenRepo, ModelRepo, UsageRepo, AuditRepo
from ..core.queue import QueueItem, queue_router, STREAM_SENTINEL, _HttpException

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
    """Chat Completion 请求（OpenAI 兼容，完整参数 + 扩展透传）。

    1) 显式声明 OpenAI 官方 /v1/chat/completions 的全部标准参数；
    2) model_config = ConfigDict(extra="allow") 兜底透传所有未声明字段
       （如 vLLM/LiteLLM 扩展的 enable_thinking / thinking / repetition_penalty 等），
       未知字段进入 model_extra，model_dump() 默认包含，最终原样转发给 LiteLLM。
    """
    model_config = ConfigDict(extra="allow")

    # ---- 必填 ----
    model: str
    messages: list[dict]

    # ---- 采样 / 生成控制（OpenAI 标准）----
    temperature: Optional[float] = 1.0
    top_p: Optional[float] = 1.0
    n: Optional[int] = 1
    stream: Optional[bool] = False
    stop: Optional[Union[list[str], str]] = None
    max_tokens: Optional[int] = None
    max_completion_tokens: Optional[int] = None
    presence_penalty: Optional[float] = 0.0
    frequency_penalty: Optional[float] = 0.0
    logit_bias: Optional[dict[str, float]] = None
    logprobs: Optional[bool] = None
    top_logprobs: Optional[int] = None
    seed: Optional[int] = None
    user: Optional[str] = None

    # ---- 推理 / think 控制（OpenAI o-series 标准 + vLLM/LiteLLM 透传）----
    reasoning_effort: Optional[str] = None          # low | medium | high
    reasoning: Optional[dict] = None                # 新版 reasoning 控制对象
    enable_thinking: Optional[bool] = None          # vLLM/Qwen 思考开关（透传）
    thinking: Optional[dict] = None                 # {"type":"enabled","budget_tokens":N}（透传）
    include_reasoning: Optional[bool] = None        # 流中返回 reasoning（透传）

    # ---- 工具调用（OpenAI 标准）----
    tools: Optional[list[dict]] = None
    tool_choice: Optional[Any] = None               # str | dict | None
    parallel_tool_calls: Optional[bool] = None
    # legacy 兼容
    functions: Optional[list[dict]] = None
    function_call: Optional[Any] = None

    # ---- 结构化输出 / 预测（OpenAI 标准）----
    response_format: Optional[dict] = None
    prediction: Optional[dict] = None

    # ---- 多模态 / 音频（OpenAI 多模态标准）----
    modalities: Optional[list[str]] = None          # ["text"] | ["text","audio"]
    audio: Optional[dict] = None                    # {"voice":..,"format":..}

    # ---- 流式（OpenAI 标准，内部强制 include_usage=True）----
    stream_options: Optional[dict] = None

    # ---- 存储 / 元数据 / 服务层级（OpenAI 标准）----
    store: Optional[bool] = None
    metadata: Optional[dict] = None
    service_tier: Optional[str] = None

    # ---- PLLM 扩展：队列挡位选择（覆盖对话默认 medium）----
    pllm_tier: Optional[str] = None

    # ---- vLLM 常用采样扩展（经 LiteLLM 透传）----
    repetition_penalty: Optional[float] = None
    top_k: Optional[int] = None
    min_p: Optional[float] = None
    typical_p: Optional[float] = None
    add_generation_prompt: Optional[bool] = None

    # ---- LiteLLM 透传开关 ----
    drop_params: Optional[bool] = None


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
                pllm_token_ref_id=token_record["id"],
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
        # 全局当前模型取自 models 表 is_current 标记（持久化在 DB，切换全局模型只需改
        # is_current，无需改 env / 重建容器）。存在 current 模型时统一路由到它（忽略客户端
        # model）；不存在时退回客户端传入的 body.model。
        global_model = await ModelRepo.get_current_model_name(conn)
        effective_model = global_model or body.model

        model_config = await ModelRepo.get_current(conn, effective_model)
        if not model_config:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Model not found: {effective_model}",
            )

        # is_enabled 检查：禁用的模型不可调用
        if not model_config["is_enabled"]:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Model not found: {effective_model}",
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

    # 队列挡位选择：header X-PLLM-Tier > body.pllm_tier > 默认 medium
    selected_tier = request.headers.get("X-PLLM-Tier") or body.pllm_tier
    if selected_tier not in ("high", "medium", "low"):
        selected_tier = None

    # 构造请求体：模型 request_params 作为默认值，客户端参数优先覆盖
    request_body = body.model_dump(exclude_none=True)
    rp = model_config.get("request_params")
    if isinstance(rp, str):
        # asyncpg 读 jsonb 列默认返回 JSON 字符串，需先反序列化为 dict
        try:
            rp = json.loads(rp)
        except (ValueError, TypeError):
            rp = None
    if rp:
        # 先以模型参数为底，再用客户端显式传入的值覆盖
        merged = {**rp, **request_body}
        request_body = merged
    # 全局强制模型：无条件覆盖 model，确保不被 request_params 或客户端值覆盖
    request_body["model"] = effective_model

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

    # ---- 队列路由分支：投入三级挡位队列，由 worker 异步转发 ----
    if settings.queue_enabled and queue_router.started:
        return await _route_via_queue(
            request_id=request_id,
            request_body=request_body,
            headers=headers,
            litellm_url=litellm_url,
            token_record=token_record,
            model_config=model_config,
            effective_model=effective_model,
            correlation_id=correlation_id,
            start_time=start_time,
            is_stream=bool(body.stream),
            selected_tier=selected_tier,
        )

    # ---- 否则走原直连转发（与历史行为一致，零回归）----
    try:
        if body.stream:
            # 流式响应：httpx 客户端生命周期必须由生成器自身持有。
            # 若沿用外层 async with，函数返回后 StreamingResponse 惰性消费生成器时
            # 客户端已关闭，导致 0 字节输出 / "client has been closed" 错误。
            async def stream_generator():
                prompt_tokens = 0
                completion_tokens = 0
                total_tokens = 0
                upstream_status = 200
                client = None
                try:
                    client = httpx.AsyncClient(timeout=120.0)
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
                except httpx.TimeoutException:
                    upstream_status = 504
                except httpx.RequestError:
                    upstream_status = 502
                finally:
                    if client is not None:
                        await client.aclose()
                    # 无论正常结束还是客户端断开，都记录 usage
                    latency_ms = int((time.time() - start_time) * 1000)
                    await _record_usage(
                        request_id=request_id,
                        token_record=token_record,
                        model_config=model_config,
                        model_name=effective_model,
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
                        model_name=effective_model,
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
            async with httpx.AsyncClient(timeout=120.0) as client:
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
                        model_name=effective_model,
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
                    model_name=effective_model,
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
                    model_name=effective_model,
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
            model_name=effective_model,
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
            model_name=effective_model,
            status_code=502,
            result="error",
        )
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"LiteLLM request failed: {e!s}",
        )


# ---------------------------------------------------------------------------
# 队列路由：请求入队 / 流式消费 / worker 转发回调
# ---------------------------------------------------------------------------
async def _route_via_queue(
    *,
    request_id: uuid.UUID,
    request_body: dict,
    headers: dict,
    litellm_url: str,
    token_record: dict,
    model_config: dict,
    effective_model: str,
    correlation_id: Optional[str],
    start_time: float,
    is_stream: bool,
    selected_tier: Optional[str] = None,
):
    """将请求投入三级挡位队列并等待结果。

    非流式：结果经 ``item.future`` 返回；流式：逐行经 ``item.stream_q`` 返回。
    """
    item = QueueItem(
        request_id=str(request_id),
        request_body=request_body,
        headers=headers,
        litellm_url=litellm_url,
        token_record=token_record,
        model_config=model_config,
        target_model=effective_model,
        current_tier=(selected_tier or "medium"),
        is_stream=is_stream,
        correlation_id=correlation_id,
        start_time=start_time,
    )
    await queue_router.enqueue(item)

    if is_stream:
        return StreamingResponse(
            _stream_from_queue_item(item),
            media_type="text/event-stream",
            headers={"X-Request-Id": str(request_id)},
        )

    try:
        await asyncio.wait_for(item.future, timeout=settings.queue_http_timeout_seconds)
    except asyncio.TimeoutError:
        raise HTTPException(
            status_code=status.HTTP_504_GATEWAY_TIMEOUT,
            detail="队列处理超时",
        )
    exc = item.future.exception()
    if exc is not None:
        if isinstance(exc, _HttpException):
            raise HTTPException(status_code=exc.status_code, detail=exc.detail)
        raise exc
    _status_code, payload = item.future.result()
    payload["request_id"] = str(request_id)
    return JSONResponse(content=payload, headers={"X-Request-Id": str(request_id)})


async def _stream_from_queue_item(item: QueueItem):
    """从队列工作项消费流式 SSE 行，直到哨兵。"""
    try:
        while True:
            chunk = await item.stream_q.get()
            if chunk is STREAM_SENTINEL:
                break
            if isinstance(chunk, tuple) and chunk[0] == "error":
                _, code, detail = chunk
                yield (
                    "data: "
                    + json.dumps({"error": {"message": str(detail), "code": code}})
                    + "\n\n"
                )
                break
            else:
                # 已是 "data: ...\n\n" 行
                yield chunk
    finally:
        item.stream_done.set()


async def _queue_forward(item: QueueItem) -> None:
    """队列 worker 调用的实际转发：发往 LiteLLM 并录制 usage/审计。

    非流式结果写入 ``item.future``（(status, json) 或异常）；
    流式结果逐行写入 ``item.stream_q``，结束塞入 :data:`STREAM_SENTINEL`。
    """
    request_body = {**item.request_body, "model": item.target_model}
    headers = item.headers
    litellm_url = item.litellm_url
    token_record = item.token_record
    model_config = item.model_config
    effective_model = item.target_model
    tier_snapshot = item.current_tier
    inference_engine = model_config.get("inference_engine")
    correlation_id = item.correlation_id
    start_time = item.start_time

    try:
        if item.is_stream:
            prompt_tokens = completion_tokens = total_tokens = 0
            upstream_status = 200
            client = None
            try:
                client = httpx.AsyncClient(timeout=settings.queue_http_timeout_seconds)
                async with client.stream(
                    "POST", litellm_url, json=request_body, headers=headers
                ) as response:
                    upstream_status = response.status_code
                    if response.status_code != 200:
                        error_body = await response.aread()
                        item.stream_q.put_nowait(
                            ("error", response.status_code, error_body.decode())
                        )
                        item.stream_q.put_nowait(STREAM_SENTINEL)
                        item.stream_done.set()
                        await _record_usage(
                            request_id=uuid.UUID(item.request_id),
                            token_record=token_record,
                            model_config={"tier": tier_snapshot,
                                          "inference_engine": inference_engine},
                            model_name=effective_model,
                            prompt_tokens=0, completion_tokens=0, total_tokens=0,
                            latency_ms=int((time.time() - start_time) * 1000),
                            status_code=response.status_code,
                            correlation_id=correlation_id,
                        )
                        await _record_inference_audit(
                            request_id=uuid.UUID(item.request_id),
                            token_record=token_record,
                            model_name=effective_model,
                            status_code=response.status_code,
                            result="error",
                        )
                        return
                    async for line in response.aiter_lines():
                        if line.startswith("data: "):
                            item.stream_q.put_nowait(line + "\n\n")
                            if line == "data: [DONE]":
                                break
                            try:
                                data = json.loads(line[6:])
                                if "usage" in data:
                                    usage = data["usage"]
                                    prompt_tokens = usage.get("prompt_tokens", 0)
                                    completion_tokens = usage.get("completion_tokens", 0)
                                    total_tokens = usage.get("total_tokens", 0)
                            except json.JSONDecodeError:
                                pass
            except httpx.TimeoutException:
                upstream_status = 504
            except httpx.RequestError:
                upstream_status = 502
            finally:
                if client is not None:
                    await client.aclose()
                latency_ms = int((time.time() - start_time) * 1000)
                await _record_usage(
                    request_id=uuid.UUID(item.request_id),
                    token_record=token_record,
                    model_config={"tier": tier_snapshot,
                                  "inference_engine": inference_engine},
                    model_name=effective_model,
                    prompt_tokens=prompt_tokens,
                    completion_tokens=completion_tokens,
                    total_tokens=total_tokens,
                    latency_ms=latency_ms,
                    status_code=upstream_status,
                    correlation_id=correlation_id,
                )
                await _record_inference_audit(
                    request_id=uuid.UUID(item.request_id),
                    token_record=token_record,
                    model_name=effective_model,
                    status_code=upstream_status,
                    result="success" if upstream_status == 200 else "error",
                )
                item.stream_q.put_nowait(STREAM_SENTINEL)
                item.stream_done.set()
        else:
            async with httpx.AsyncClient(timeout=settings.queue_http_timeout_seconds) as client:
                response = await client.post(
                    litellm_url, json=request_body, headers=headers
                )
                if response.status_code != 200:
                    await _record_inference_audit(
                        request_id=uuid.UUID(item.request_id),
                        token_record=token_record,
                        model_name=effective_model,
                        status_code=response.status_code,
                        result="error",
                    )
                    item.future.set_exception(
                        _HttpException(response.status_code, response.text)
                    )
                    return
                result = response.json()
                usage = result.get("usage", {})
                await _record_usage(
                    request_id=uuid.UUID(item.request_id),
                    token_record=token_record,
                    model_config={"tier": tier_snapshot,
                                  "inference_engine": inference_engine},
                    model_name=effective_model,
                    prompt_tokens=usage.get("prompt_tokens", 0),
                    completion_tokens=usage.get("completion_tokens", 0),
                    total_tokens=usage.get("total_tokens", 0),
                    latency_ms=int((time.time() - start_time) * 1000),
                    status_code=200,
                    correlation_id=correlation_id,
                )
                await _record_inference_audit(
                    request_id=uuid.UUID(item.request_id),
                    token_record=token_record,
                    model_name=effective_model,
                    status_code=200,
                    result="success",
                )
                item.future.set_result((200, result))
    except httpx.TimeoutException:
        item.future.set_exception(_HttpException(504, "LiteLLM request timeout"))
    except httpx.RequestError as e:
        item.future.set_exception(
            _HttpException(502, f"LiteLLM request failed: {e!s}")
        )
    except Exception as e:  # noqa: BLE001
        logger.exception("队列转发异常 request_id=%s", item.request_id)
        if not item.future.done():
            item.future.set_exception(_HttpException(500, f"internal error: {e!s}"))


# worker 通过回调注入转发逻辑，避免与网关模块形成循环依赖
queue_router.forward_fn = _queue_forward


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
