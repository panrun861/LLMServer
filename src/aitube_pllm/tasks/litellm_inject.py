"""LiteLLM 模型注入任务

PLLM 启动时从 models 表读取 upstream_type=external_api 的模型行，
解密 api_key_encrypted，构造 LiteLLM Proxy config 并通过
PUT /v1/config/save API 动态注入。

注入内容包括：model_name（LiteLLM 别名）、provider、api_base、api_key。
本地 vLLM 模型不注入（其配置由 LiteLLM 侧静态维护）。
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


async def fetch_external_models() -> list[dict]:
    """从 models 表读取外部 API 模型列表，并解密 api_key。

    返回每条记录包含 model_name, model_artifact, api_base, api_key。
    解密失败的模型跳过并记录警告。
    """
    async with db.pool.acquire() as conn:
        all_models = await ModelRepo.list_all(conn)

    external_models: list[dict[str, Any]] = []
    for m in all_models:
        # 从 runtime_params 中提取 upstream_type（兼容 dict/str/None）
        rp = m.get("runtime_params")
        if isinstance(rp, str):
            try:
                import json
                rp = json.loads(rp)
            except (ValueError, TypeError):
                continue
        entry_type = (rp or {}).get("upstream_type")
        if entry_type != "external_api":
            continue

        # 提取 api_key
        api_key_enc = m.get("api_key_encrypted")
        api_key = decrypt(api_key_enc) if api_key_enc else None
        if not api_key and m.get("api_base"):
            logger.warning(
                "外部模型 %s 无可用 api_key（未配置或未解密），跳过注入",
                m["model_name"],
            )
            continue

        external_models.append({
            "model_name": m["model_name"],
            "model_artifact": m["model_artifact"],
            "api_base": m.get("api_base") or "",
            "api_key": api_key,
            "tier": m["tier"],
            "context_length": m["context_length"],
            "is_enabled": m["is_enabled"],
        })

    return external_models


def _build_litellm_config(models: list[dict]) -> dict[str, Any]:
    """将外部模型列表转换为 LiteLLM Proxy config.yaml 格式的 model_list 结构。

    每个模型的 litellm_params 根据 api_base/model_artifact 推断 provider。
    若 model_artifact 以 openai/ 开头，provider=openai；否则 default 用 openai。
    """
    model_list_entries = []
    for m in models:
        if not m.get("is_enabled"):
            continue

        artifact = m.get("model_artifact", m["model_name"])
        api_base = m.get("api_base") or ""

        # 推断 provider
        provider = "openai"  # default fallback
        if "/" in artifact:
            provider = artifact.split("/")[0]

        litellm_params: dict[str, Any] = {
            "model": f"{provider}/{artifact}" if provider != artifact else artifact,
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
    """启动注入：读取外部模型 → 解密 → 调用 LiteLLM admin API 注入。

    返回注入结果摘要。
    """
    external = await fetch_external_models()
    if not external:
        logger.info("无外部 API 模型需要注入 LiteLLM")
        return {"injected": 0, "skipped": 0, "errors": []}

    config = _build_litellm_config(external)
    admin_url = _resolve_litellm_admin_url()
    save_url = f"{admin_url}/v1/config/save"

    summary = {"injected": 0, "skipped": len(external) - len(config.get("model_list", [])), "errors": []}

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
                    "LiteLLM 注入完成: injected=%d config=%s",
                    summary["injected"],
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
    """将单个外部模型注入 LiteLLM（用于注册接口回调）。

    Args:
        model_data: 包含 model_name, model_artifact, api_base, api_key 的 dict
    """
    config = _build_litellm_config([model_data])
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
                logger.info("LiteLLM 单模型注入成功: %s", model_data["model_name"])
                return {"success": True}
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