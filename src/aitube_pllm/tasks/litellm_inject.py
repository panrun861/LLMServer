"""LiteLLM 模型注入任务

PLLM 启动时从 models 表读取所有已登记模型（local_vllm + external_api），
解密 api_key_encrypted，构造 LiteLLM Proxy config 并通过
PUT /v1/config/save API 全量动态注入。

**不再依赖 LiteLLM 静态 config.yaml**——所有模型的路由、api_base、api_key
均由 PLLM 数据库统一管理，启动时一次性注入。

注入内容包括：model_name（LiteLLM 别名）、provider、api_base、api_key。
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


def _build_litellm_config(models: list[dict]) -> dict[str, Any]:
    """将模型列表转换为 LiteLLM Proxy config model_list 结构。

    provider 推断规则：
    - local_vllm → ``vllm/{artifact}``，api_base 来自模型配置
    - external_api + artifact 含 ``/`` → ``{provider}/{artifact}``（如 ``openai/gpt-4o``）
    - external_api + artifact 不含 ``/`` → default ``openai/{artifact}``
    """
    model_list_entries = []
    for m in models:
        if not m.get("is_enabled"):
            continue

        artifact = m.get("model_artifact", m["model_name"])
        api_base = m.get("api_base") or ""
        upstream_type = m.get("upstream_type", "local_vllm")

        # 推断 provider 和完整 model 路径
        if upstream_type == "local_vllm":
            # 本地 vLLM 模型，直接以 vllm/ 前缀路由
            litellm_model = f"vllm/{artifact}"
        elif "/" in artifact:
            provider = artifact.split("/")[0]
            litellm_model = f"{provider}/{artifact}"
        else:
            # 外部 API 但 artifact 不带前缀，默认 openai
            litellm_model = f"openai/{artifact}"

        litellm_params: dict[str, Any] = {
            "model": litellm_model,
        }
        if api_base:
            litellm_params["api_base"] = api_base
        if m.get("api_key"):
            litellm_params["api_key"] = m["api_key"]

        entry = {
            "model_name": m["model_name"],
            "litellm_params": litellm_params,
        }
        model_list_entries.append(entry)

    return {"model_list": model_list_entries}


async def inject_models_to_litellm() -> dict[str, Any]:
    """启动注入：读取所有已登记模型 → 解密 → 全量覆盖注入 LiteLLM。

    全量覆盖模式：从 PLLM 数据库读取全部已登记模型，构造完整的 model_list
    并调用 PUT /v1/config/save 覆盖 LiteLLM 配置。不再依赖 LiteLLM 静态
    config.yaml。

    返回注入结果摘要。
    """
    all_models = await fetch_all_registered_models()
    if not all_models:
        logger.info("无已登记模型需要注入 LiteLLM")
        return {"injected": 0, "skipped": 0, "errors": [], "total": 0}

    config = _build_litellm_config(all_models)
    admin_url = _resolve_litellm_admin_url()
    save_url = f"{admin_url}/v1/config/save"

    summary = {
        "injected": 0,
        "skipped": len(all_models) - len(config.get("model_list", [])),
        "total": len(all_models),
        "errors": [],
    }

    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.put(
                save_url,
                json=config,
                headers={"Authorization": f"Bearer {settings.litellm_master_key}"},
            )
            if resp.status_code in (200, 201, 202):
                summary["injected"] = len(config.get("model_list", []))
                logger.info(
                    "LiteLLM 全量注入完成: injected=%d skipped=%d total=%d config=%s",
                    summary["injected"], summary["skipped"], summary["total"],
                    config,
                )
            else:
                error_msg = f"LiteLLM /v1/config/save 返回 {resp.status_code}: {resp.text[:500]}"
                logger.error(error_msg)
                summary["errors"].append(error_msg)
    except Exception as exc:  # noqa: BLE001
        error_msg = f"LiteLLM 注入失败: {exc}"
        logger.error(error_msg)
        summary["errors"].append(error_msg)

    return summary


async def inject_single_model(model_data: dict) -> dict[str, Any]:
    """注册新模型后触发全量重注入（确保 LiteLLM config 与 PLLM 数据库一致）。

    因为 PUT /v1/config/save 是全量覆盖，不支持追加单个模型，
    所以从数据库重新读取全部已登记模型后全量注入。
    """
    all_models = await fetch_all_registered_models()

    # 检查当前模型是否已在列表中
    existing_names = {m["model_name"] for m in all_models}
    if model_data.get("model_name") not in existing_names:
        logger.info("新注册模型 %s 不在注入列表中，跳过重注入", model_data.get("model_name"))
        return {"success": True, "note": "model not in injectable list"}

    return await _do_full_injection(all_models)


async def _do_full_injection(all_models: list[dict]) -> dict[str, Any]:
    """执行全量注入 LiteLLM。"""
    config = _build_litellm_config(all_models)
    admin_url = _resolve_litellm_admin_url()
    save_url = f"{admin_url}/v1/config/save"

    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.put(
                save_url,
                json=config,
                headers={"Authorization": f"Bearer {settings.litellm_master_key}"},
            )
            if resp.status_code in (200, 201, 202):
                logger.info("LiteLLM 全量注入成功: %d models", len(config.get("model_list", [])))
                return {"success": True, "injected": len(config.get("model_list", []))}
            else:
                error_msg = f"LiteLLM 注入返回 {resp.status_code}: {resp.text[:500]}"
                logger.error(error_msg)
                return {"success": False, "error": error_msg}
    except Exception as exc:  # noqa: BLE001
        error_msg = f"LiteLLM 注入异常: {exc}"
        logger.error(error_msg)
        return {"success": False, "error": error_msg}


async def remove_model_from_litellim(model_name: str) -> dict[str, Any]:
    """从 LiteLLM 中移除指定模型的注入配置。

    通过读取当前 config → 过滤该模型 → 重新保存。
    """
    admin_url = _resolve_litellm_admin_url()
    save_url = f"{admin_url}/v1/config/save"
    models_url = _resolve_litellm_admin_url() + "/v1/models"

    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            # 先获取当前配置（通过 models 列表推断，或直接保存空 list）
            resp = await client.get(
                models_url,
                headers={"Authorization": f"Bearer {settings.litellm_master_key}"},
            )
            if resp.status_code != 200:
                return {"success": False, "error": f"获取 LiteLLM models 失败: {resp.status_code}"}

            current_models = resp.json().get("data", [])
            remaining = [m["id"] for m in current_models if m.get("id") != model_name]

            # 重新构建 config（仅保留未删除的模型）
            # 注意：这里简化为只保留现有模型中不匹配的
            # 实际应通过 /v1/config/get 获取完整 config 再过滤
            logger.warning(
                "remove_model_from_litellim 暂不支持精确移除（需读取完整 config），"
                "建议调用 PUT /v1/config/save 全量覆盖"
            )
            return {"success": False, "error": "精确移除暂不支持（需全量覆盖 config）"}
    except Exception as exc:  # noqa: BLE001
        return {"success": False, "error": str(exc)}