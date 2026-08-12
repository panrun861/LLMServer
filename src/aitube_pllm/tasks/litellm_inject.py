"""LiteLLM 模型注入任务

PLLM 启动时从 models 表读取所有已登记模型（local_vllm + external_api），
解密 api_key_encrypted，通过 LiteLLM 单模型管理端点 ``POST /model/new`` 逐模型注册。

为何不用 /config/update：
    LiteLLM 1.92 的 ``auth_utils.is_request_body_safe()`` 硬编码禁止请求体中包含
    ``model_list``（无论 master 还是 admin key），因此无法用 /config/update 整体下发
    model_list。改用 ``/model/new`` 单模型注册，并依赖 ``STORE_MODEL_IN_DB=True``
    将模型持久化到 LiteLLM 自己的数据库（重启后仍生效）。

幂等：每个模型注册前先 ``POST /model/delete`` 删除同名 DB 记录，再 ``/model/new``。

不再依赖 LiteLLM 静态 config.yaml 的 model_list —— 静态 model_list 已清空，
所有模型路由由 PLLM 数据库统一管理，启动时一次性注入。
"""

from __future__ import annotations

import logging
from typing import Any

import httpx

from ..config import settings
from ..db import db, ModelRepo
from ..utils.crypto import decrypt

logger = logging.getLogger(__name__)


def _resolve_litellm_admin_url() -> str:
    """获取 LiteLLM admin API 地址。"""
    if settings.litellm_admin_url:
        return settings.litellm_admin_url
    return settings.litellm_api_base.rstrip("/")


def _auth_header() -> dict[str, str]:
    """LiteLLM 调用鉴权头（master key 即可调用 /model/new）。"""
    return {"Authorization": f"Bearer {settings.litellm_master_key}"}


async def fetch_all_registered_models() -> list[dict]:
    """从 models 表读取所有已登记模型，并解密 api_key。

    包括 local_vllm 和 external_api 两种类型——所有模型均由 PLLM 注入 LiteLLM，
    不再依赖 LiteLLM 静态 config.yaml。

    返回每条记录包含 model_name, model_artifact, api_base, api_key, upstream_type。
    解密失败的模型跳过并记录警告。
    """
    async with db.pool.acquire() as conn:
        all_models = await ModelRepo.list_all(conn)

    injectable: list[dict[str, Any]] = []
    for m in all_models:
        # 从 runtime_params 中提取 upstream_type（兼容 dict/str/None）
        rp = m.get("runtime_params")
        if isinstance(rp, str):
            try:
                import json
                rp = json.loads(rp)
            except (ValueError, TypeError):
                rp = None
        entry_type = (rp or {}).get("upstream_type", "local_vllm")

        # 两类都注入
        if entry_type not in ("local_vllm", "external_api"):
            continue

        # 提取 api_key（外部 API 模型解密；本地 vLLM 通常为 None）
        api_key_enc = m.get("api_key_encrypted")
        api_key = decrypt(api_key_enc) if api_key_enc else None

        if entry_type == "external_api" and not api_key and not m.get("api_base"):
            logger.warning(
                "外部模型 %s 缺少 api_key 和 api_base，跳过注入",
                m["model_name"],
            )
            continue

        injectable.append({
            "model_name": m["model_name"],
            "model_artifact": m["model_artifact"],
            "api_base": m.get("api_base") or "",
            "api_key": api_key,
            "upstream_type": entry_type,
            "tier": m["tier"],
            "context_length": m["context_length"],
            "is_enabled": m["is_enabled"],
        })

    return injectable


def _model_new_payload(m: dict) -> dict[str, Any]:
    """构造单个模型的 POST /model/new 请求体。

    provider 推断规则：
    - local_vllm → ``hosted_vllm/{artifact}``（LiteLLM 对 vLLM 的原生 provider）
    - external_api + artifact 含 ``/`` → ``{provider}/{artifact}``（如 ``openai/gpt-4o``）
    - external_api + artifact 不含 ``/`` → default ``openai/{artifact}``
    """
    artifact = m.get("model_artifact", m["model_name"])
    api_base = m.get("api_base") or ""
    upstream_type = m.get("upstream_type", "local_vllm")

    if upstream_type == "local_vllm":
        litellm_model = f"hosted_vllm/{artifact}"
    elif "/" in artifact:
        provider = artifact.split("/")[0]
        litellm_model = f"{provider}/{artifact}"
    else:
        # 外部 API 但 artifact 不带前缀，默认 openai
        litellm_model = f"openai/{artifact}"

    litellm_params: dict[str, Any] = {"model": litellm_model}
    if api_base:
        litellm_params["api_base"] = api_base
    if m.get("api_key"):
        litellm_params["api_key"] = m["api_key"]

    return {
        "model_name": m["model_name"],
        "litellm_params": litellm_params,
    }


async def _delete_model(client: httpx.AsyncClient, admin_url: str, model_name: str) -> None:
    """删除 LiteLLM DB 中同名模型（幂等前置清理，忽略不存在的错误）。"""
    try:
        resp = await client.post(
            f"{admin_url}/model/delete",
            json={"model_name": model_name},
            headers=_auth_header(),
        )
        if resp.status_code not in (200, 201, 404):
            logger.warning("删除同名模型 %s 返回 %d", model_name, resp.status_code)
    except Exception as exc:  # noqa: BLE001
        logger.warning("删除同名模型 %s 异常: %s", model_name, exc)


async def inject_models_to_litellm() -> dict[str, Any]:
    """启动注入：读取所有已登记模型 → 解密 → 逐模型 POST /model/new 注册。

    LiteLLM 1.92 的 /config/update 硬编码禁止 model_list，因此改用单模型端点
    /model/new（需 STORE_MODEL_IN_DB=True 持久化到 LiteLLM DB）。每个模型注册前
    先 DELETE 同名 DB 记录以保证幂等。

    返回注入结果摘要。
    """
    all_models = await fetch_all_registered_models()
    if not all_models:
        logger.info("无已登记模型需要注入 LiteLLM")
        return {"injected": 0, "skipped": 0, "errors": [], "total": 0}

    admin_url = _resolve_litellm_admin_url()
    summary: dict[str, Any] = {
        "injected": 0,
        "skipped": 0,
        "total": len(all_models),
        "errors": [],
    }

    async with httpx.AsyncClient(timeout=30.0) as client:
        for m in all_models:
            if not m.get("is_enabled"):
                summary["skipped"] += 1
                continue
            payload = _model_new_payload(m)
            try:
                # 幂等：先删除同名（忽略不存在）
                await _delete_model(client, admin_url, m["model_name"])
                # 注册
                resp = await client.post(
                    f"{admin_url}/model/new",
                    json=payload,
                    headers=_auth_header(),
                )
                if resp.status_code in (200, 201):
                    summary["injected"] += 1
                    logger.info(
                        "LiteLLM 注册模型成功: %s -> %s api_base=%s",
                        m["model_name"], payload["litellm_params"]["model"],
                        payload["litellm_params"].get("api_base", ""),
                    )
                else:
                    msg = f"注册 {m['model_name']} 返回 {resp.status_code}: {resp.text[:300]}"
                    logger.error(msg)
                    summary["errors"].append(msg)
            except Exception as exc:  # noqa: BLE001
                msg = f"注册 {m['model_name']} 异常: {exc}"
                logger.error(msg)
                summary["errors"].append(msg)

    return summary


async def inject_single_model(model_data: dict) -> dict[str, Any]:
    """注册新模型后触发单模型重注入（确保 LiteLLM 与 PLLM 数据库一致）。"""
    all_models = await fetch_all_registered_models()
    target = next(
        (m for m in all_models if m["model_name"] == model_data.get("model_name")),
        None,
    )
    if not target:
        return {"success": True, "note": "model not in injectable list"}

    admin_url = _resolve_litellm_admin_url()
    payload = _model_new_payload(target)
    async with httpx.AsyncClient(timeout=15.0) as client:
        try:
            await _delete_model(client, admin_url, target["model_name"])
            resp = await client.post(
                f"{admin_url}/model/new",
                json=payload,
                headers=_auth_header(),
            )
            if resp.status_code in (200, 201):
                return {"success": True, "injected": target["model_name"]}
            return {"success": False, "error": f"返回 {resp.status_code}: {resp.text[:300]}"}
        except Exception as exc:  # noqa: BLE001
            return {"success": False, "error": str(exc)}


async def remove_model_from_litellm(model_name: str) -> dict[str, Any]:
    """从 LiteLLM 删除指定模型（DB 中的记录）。"""
    admin_url = _resolve_litellm_admin_url()
    async with httpx.AsyncClient(timeout=15.0) as client:
        try:
            resp = await client.post(
                f"{admin_url}/model/delete",
                json={"model_name": model_name},
                headers=_auth_header(),
            )
            if resp.status_code in (200, 201, 404):
                return {"success": True}
            return {"success": False, "error": f"返回 {resp.status_code}: {resp.text[:300]}"}
        except Exception as exc:  # noqa: BLE001
            return {"success": False, "error": str(exc)}
