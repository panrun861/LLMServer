"""模型登记管理 API - 仅 localhost CLI 可用"""

from typing import Literal, Optional

from fastapi import APIRouter, HTTPException, Request, status
from pydantic import BaseModel, Field

from ..db import db, ModelRepo, AuditRepo
from ..tasks.model_sync import sync_models_from_upstream

router = APIRouter(prefix="/admin/models", tags=["Model Management"])


def _require_local_admin(request: Request):
    """验证 localhost CLI 认证"""
    local_admin = request.headers.get("x-local-admin")
    if local_admin != "true":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="This endpoint is only available from localhost CLI",
        )


class ModelRegisterRequest(BaseModel):
    model_name: str = Field(..., max_length=255)
    tier: str = Field("medium", max_length=32)
    model_artifact: str = Field(
        ...,
        max_length=255,
        description=(
            "LiteLLM 中登记的模型别名。本地 vLLM 与外部 API 统一经 LiteLLM 网关："
            "外部 API 需先在 LiteLLM 侧注册并配置真实 api_base/key，这里只填其别名"
        ),
    )
    inference_engine: Literal["litellm"] = Field(
        "litellm",
        description=(
            "统一经 LiteLLM 网关路由（本地 vLLM 与外部 API 均如此，真正的上游地址"
            "由 LiteLLM 持有）。预留扩展位，未来可加入 direct 引擎"
        ),
    )
    upstream_type: Optional[Literal["local_vllm", "external_api"]] = Field(
        None,
        description=(
            "声明上游来源：local_vllm=本机 vLLM 服务；external_api=第三方 API"
            "（经 LiteLLM 代理）。仅作运营标记，不改变路由（均走 LiteLLM）"
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
        description=(
            "仅作信息记录，不参与路由（PLLM 始终经 LiteLLM）。外部 API 的真实"
            " api_base 应配置在 LiteLLM 侧"
        ),
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

    本地 vLLM 与外部 API 统一经 LiteLLM 网关：外部 API 需在 LiteLLM 侧先注册
    （由其持有真实 api_base/key），此处 model_artifact 填 LiteLLM 别名即可。
    upstream_type 仅作运营标记，不改变路由。
    """
    _require_local_admin(request)

    # 将 upstream_type 标记并入 runtime_params（已有 JSON 列，不参与推理路由），
    # 避免新增 DB 列带来的迁移成本
    payload = body.model_dump()
    if body.upstream_type:
        rp = dict(payload.get("runtime_params") or {})
        rp["upstream_type"] = body.upstream_type
        payload["runtime_params"] = rp

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
            detail=payload,
        )

    return {
        "id": model["id"],
        "model_name": model["model_name"],
        "tier": model["tier"],
        "model_artifact": model["model_artifact"],
        "inference_engine": model["inference_engine"],
        "upstream_type": (model.get("runtime_params") or {}).get("upstream_type"),
        "context_length": model["context_length"],
        "is_current": model["is_current"],
        "is_enabled": model["is_enabled"],
        "sync_status": model["sync_status"],
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
            "upstream_type": (m.get("runtime_params") or {}).get("upstream_type"),
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
