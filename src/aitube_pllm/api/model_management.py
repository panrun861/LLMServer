"""模型登记管理 API - 仅 localhost CLI 可用"""

import json
from typing import Literal, Optional

from fastapi import APIRouter, HTTPException, Request, status
from pydantic import BaseModel, Field

from ..db import db, ModelRepo, AuditRepo
from ..tasks.model_sync import sync_models_from_upstream
from ..tasks.litellm_inject import inject_single_model
from ..utils.crypto import encrypt

router = APIRouter(prefix="/admin/models", tags=["Model Management"])


def _require_local_admin(request: Request):
    """验证 localhost CLI 认证"""
    local_admin = request.headers.get("x-local-admin")
    if local_admin != "true":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="This endpoint is only available from localhost CLI",
        )


def _extract_upstream_type(runtime_params) -> Optional[str]:
    """安全提取 runtime_params 中的 upstream_type（兼容 dict / JSON 字符串 / None）"""
    if not runtime_params:
        return None
    if isinstance(runtime_params, str):
        try:
            runtime_params = json.loads(runtime_params)
        except (ValueError, TypeError):
            return None
    return (runtime_params or {}).get("upstream_type")


class ModelRegisterRequest(BaseModel):
    model_name: str = Field(..., max_length=255)
    tier: str = Field("medium", max_length=32)
    model_artifact: str = Field(
        ...,
        max_length=255,
        description=(
            "模型标识：外部 API 时填 provider/model-name（如 openai/gpt-4o）；"
            "本地 vLLM 时填 vLLM served name（如 qwen3.6-27b）"
        ),
    )
    inference_engine: Literal["litellm"] = Field(
        "litellm",
        description="统一经 LiteLLM 网关路由。外部 API 由 PLLM 自动注入 LiteLLM",
    )
    upstream_type: Literal["local_vllm", "external_api"] = Field(
        "local_vllm",
        description=(
            "声明上游来源：local_vllm=本地 vLLM 部署（允许多节点，各配不同 api_base）；"
            "external_api=第三方 API（如 OpenAI/Claude）。"
            "所有类型均由 PLLM 全量注入 LiteLLM，不再依赖静态 config.yaml"
        ),
    )
    context_length: int = Field(
        ...,
        gt=0,
        description="上下文长度。本地 vLLM 可由同步任务自动回填；外部 API 建议手动填写",
    )
    api_base: Optional[str] = Field(
        None,
        max_length=500,
        description="模型后端地址：external_api 时必须（如 https://api.openai.com/v1）；local_vllm 时选填（如 http://10.0.0.5:8000/v1，多节点时分别填不同地址）",
    )
    api_key: Optional[str] = Field(
        None,
        max_length=1024,
        description="模型后端密钥：external_api 时必须；local_vllm 时选填。存入 DB 前 AES 加密",
    )
    runtime_params: Optional[dict] = None
    request_params: Optional[dict] = None
    is_current: bool = False
    is_enabled: bool = True


class ModelUpdateRequest(BaseModel):
    api_base: Optional[str] = Field(None, max_length=500)
    runtime_params: Optional[dict] = None
    request_params: Optional[dict] = None
    is_enabled: Optional[bool] = None
    sync_status: Optional[str] = Field(None, pattern="^(pending|synced|failed)$")


class TierActivateRequest(BaseModel):
    tier: str = Field(..., max_length=32)


@router.post("", status_code=status.HTTP_201_CREATED)
async def register_model(body: ModelRegisterRequest, request: Request):
    """登记新模型 (localhost CLI only)

    所有类型的模型均由 PLLM 统一管理并注入 LiteLLM（全量覆盖 /v1/config/save），
    不再依赖 LiteLLM 静态 config.yaml。

    - local_vllm（默认）：同一或多台 vLLM 节点，各配不同 api_base，api_key 可选
    - external_api：第三方 API，api_base + api_key 必填，key 存储前 AES 加密
    """
    _require_local_admin(request)

    # 外部 API 必填字段校验
    if body.upstream_type == "external_api":
        if not body.api_base:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="external_api 模型必须提供 api_base",
            )
        if not body.api_key:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="external_api 模型必须提供 api_key",
            )

    # 加密 api_key
    api_key_encrypted = encrypt(body.api_key) if body.api_key else None

    # 构建 payload（api_key 不入库，只存加密版本）
    payload = body.model_dump(exclude={"api_key"})
    rp = dict(payload.get("runtime_params") or {})
    rp["upstream_type"] = body.upstream_type
    payload["runtime_params"] = json.dumps(rp)
    payload["api_key_encrypted"] = api_key_encrypted

    async with db.pool.acquire() as conn:
        existing = await ModelRepo.get_by_name_and_tier(conn, body.model_name, body.tier)
        if existing:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=f"Model already registered: {body.model_name}:{body.tier}",
            )

        model = await ModelRepo.register(conn, **payload)

        await AuditRepo.record_event(
            conn,
            actor_type="admin_cli",
            actor_id="localhost",
            action="model_register",
            target_type="model",
            target_id=f"{body.model_name}:{body.tier}",
            result="success",
            detail={
                "model_name": body.model_name,
                "tier": body.tier,
                "upstream_type": body.upstream_type,
                "has_api_key": bool(body.api_key),
            },
        )

    # 注入 LiteLLM：所有类型注册/更新后均触发全量重注入
    litellm_result = None
    litellm_data = {
        "model_name": body.model_name,
        "model_artifact": body.model_artifact,
        "api_base": body.api_base,
        "api_key": body.api_key,
        "upstream_type": body.upstream_type,
        "is_enabled": body.is_enabled,
    }
    litellm_result = await inject_single_model(litellm_data)

    return {
        "id": model["id"],
        "model_name": model["model_name"],
        "tier": model["tier"],
        "model_artifact": model["model_artifact"],
        "inference_engine": model["inference_engine"],
        "upstream_type": _extract_upstream_type(model.get("runtime_params")),
        "context_length": model["context_length"],
        "api_base": model["api_base"],
        "is_current": model["is_current"],
        "is_enabled": model["is_enabled"],
        "sync_status": model["sync_status"],
        "litellm_injected": litellm_result.get("success") if litellm_result else None,
        "created_at": model["created_at"].isoformat(),
    }


@router.post("/sync", status_code=status.HTTP_200_OK)
async def sync_models(request: Request):
    """从上游(LiteLLM/vLLM)同步模型可用状态与上下文长度到 models 表 (localhost CLI only)

    行为：
    - 上游存在且已登记的模型 -> sync_status='synced'，刷新 is_enabled/context_length
    - 上游不存在的已登记模型 -> sync_status='failed'，并按配置置 is_enabled=False
    - 若上游整体不可达，返回 502，不做批量禁用
    """
    _require_local_admin(request)

    try:
        result = await sync_models_from_upstream()
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"模型同步失败（上游不可达或返回异常）: {exc}",
        )

    async with db.pool.acquire() as conn:
        await AuditRepo.record_event(
            conn,
            actor_type="admin_cli",
            actor_id="localhost",
            action="model_sync",
            target_type="model",
            target_id="all",
            result="success",
            detail=result,
        )

    return {
        "synced": result["synced"],
        "failed": result["failed"],
        "created": result["created"],
        "upstream_models": result["upstream_models"],
        "errors": result["errors"],
    }


@router.get("")
async def list_models(
    request: Request,
    model_name: Optional[str] = None,
    tier: Optional[str] = None,
):
    """列出所有登记的模型 (localhost CLI only)"""
    _require_local_admin(request)

    async with db.pool.acquire() as conn:
        models = await ModelRepo.list_all(conn, model_name=model_name, tier=tier)

    items = []
    for m in models:
        items.append({
            "id": m["id"],
            "model_name": m["model_name"],
            "tier": m["tier"],
            "model_artifact": m["model_artifact"],
            "inference_engine": m["inference_engine"],
            "upstream_type": _extract_upstream_type(m.get("runtime_params")),
            "context_length": m["context_length"],
            "api_base": m["api_base"],
            "is_current": m["is_current"],
            "is_enabled": m["is_enabled"],
            "sync_status": m["sync_status"],
            "created_at": m["created_at"].isoformat(),
            "updated_at": m["updated_at"].isoformat(),
        })
    return {"items": items, "total": len(items)}


@router.get("/{model_name}/tiers")
async def list_tiers(model_name: str, request: Request):
    """列出指定模型的所有 tier"""
    _require_local_admin(request)

    async with db.pool.acquire() as conn:
        tiers = await ModelRepo.list_tiers(conn, model_name)
    if not tiers:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Model not found: {model_name}",
        )
    return {
        "model_name": model_name,
        "tiers": [
            {
                "tier": t["tier"],
                "model_artifact": t["model_artifact"],
                "context_length": t["context_length"],
                "is_current": t["is_current"],
                "is_enabled": t["is_enabled"],
                "sync_status": t["sync_status"],
            }
            for t in tiers
        ],
    }


@router.patch("/{model_name}/tiers/{tier}")
async def update_model(model_name: str, tier: str, body: ModelUpdateRequest, request: Request):
    """更新模型配置"""
    _require_local_admin(request)

    async with db.pool.acquire() as conn:
        model = await ModelRepo.get_by_name_and_tier(conn, model_name, tier)
        if not model:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Model not found: {model_name}:{tier}",
            )

        update_fields = body.model_dump(exclude_none=True)
        if not update_fields:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="No fields to update",
            )

        old = {k: model.get(k) for k in update_fields}
        updated = await ModelRepo.update_row(conn, model_name, tier, **update_fields)

        await AuditRepo.record_event(
            conn,
            actor_type="admin_cli",
            actor_id="localhost",
            action="model_update",
            target_type="model",
            target_id=f"{model_name}:{tier}",
            result="success",
            detail={"old": old, "new": update_fields},
        )

    return {
        "id": updated["id"],
        "model_name": updated["model_name"],
        "tier": updated["tier"],
        "updated_at": updated["updated_at"].isoformat(),
    }


@router.delete("/{model_name}/tiers/{tier}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_model(model_name: str, tier: str, request: Request):
    """删除模型登记 (不能删除 current tier)"""
    _require_local_admin(request)

    async with db.pool.acquire() as conn:
        model = await ModelRepo.get_by_name_and_tier(conn, model_name, tier)
        if not model:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Model not found: {model_name}:{tier}",
            )
        if model["is_current"]:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="Cannot delete the current tier. Activate another tier first.",
            )

        await ModelRepo.delete_row(conn, model_name, tier)

        await AuditRepo.record_event(
            conn,
            actor_type="admin_cli",
            actor_id="localhost",
            action="model_delete",
            target_type="model",
            target_id=f"{model_name}:{tier}",
            result="success",
            detail={"model_name": model_name, "tier": tier},
        )

    return None


@router.put("/{model_name}/active-tier")
async def activate_tier(model_name: str, body: TierActivateRequest, request: Request):
    """激活指定 tier (用于拥塞降级或恢复)"""
    _require_local_admin(request)

    async with db.pool.acquire() as conn:
        target = await ModelRepo.get_by_name_and_tier(conn, model_name, body.tier)
        if not target:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Tier not found: {model_name}:{body.tier}",
            )
        if not target["is_enabled"]:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=f"Cannot activate disabled tier: {model_name}:{body.tier}",
            )

        # 获取当前 current
        current_rows = await ModelRepo.list_all(conn, model_name=model_name, is_current=True)
        current_tier = current_rows[0]["tier"] if current_rows else None

        activated = await ModelRepo.activate_tier(conn, model_name, body.tier)

        await AuditRepo.record_event(
            conn,
            actor_type="admin_cli",
            actor_id="localhost",
            action="tier_activate",
            target_type="model",
            target_id=f"{model_name}:{body.tier}",
            result="success",
            detail={"old_tier": current_tier, "new_tier": body.tier},
        )

    return {
        "model_name": model_name,
        "active_tier": body.tier,
        "previous_tier": current_tier,
        "activated_at": activated["updated_at"].isoformat(),
    }
