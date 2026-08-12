"""模型同步任务

将上游（LiteLLM / vLLM）真实可用状态与上下文长度回写到 PLLM 的 ``models`` 表：

* 上游列表里存在的已登记模型  -> sync_status='synced'，并按配置刷新 is_enabled / context_length
* 上游列表里不存在的已登记模型 -> sync_status='failed'，并按配置置 is_enabled=False
* （可选）上游存在但 PLLM 未登记的模型 -> 自动登记

同步源默认走 LiteLLM 的 ``/v1/models``（带 master key），因为所有模型都经 LiteLLM
单网关扇出到不同 vLLM 后端；额外可配置 ``model_sync_vllm_metrics_url`` 抓取 vLLM
Prometheus 指标中的真实 ``max_model_len`` 回填 ``context_length``。

安全约束：仅当「成功拉到上游列表、但列表中没有该模型」时才按配置禁用；若上游整体
不可达（网络/HTTP 错误），本次同步直接报错退回，不做批量禁用，避免 LiteLLM 抖动
误杀全部模型。
"""

from __future__ import annotations

import logging
import re
from datetime import datetime, timezone
from typing import Any

import httpx

from ..config import settings
from ..db import db, ModelRepo

logger = logging.getLogger(__name__)

# 简单的 Prometheus 文本指标解析：匹配 vllm:max_model_len / vllm_max_model_len
_MAX_MODEL_LEN_RE = re.compile(
    r"^(?:vllm:?max_model_len|vllm_max_model_len)\s*"
    r'\{model_name="([^"]+)"\}[^0-9]*([0-9]+)',
    re.MULTILINE,
)


async def _resolve_upstream_url() -> str:
    """解析上游 /v1/models 地址。"""
    if settings.model_sync_upstream_url:
        return settings.model_sync_upstream_url
    base = settings.litellm_api_base.rstrip("/")
    return f"{base}/v1/models"


async def fetch_upstream_models() -> list[dict]:
    """拉取上游 /v1/models，返回 [{id, max_model_len?}, ...]。

    使用 LiteLLM master key 鉴权（vLLM 对该头无感，可忽略）。
    """
    url = await _resolve_upstream_url()
    headers = {"Authorization": f"Bearer {settings.litellm_master_key}"}
    async with httpx.AsyncClient(timeout=15.0) as client:
        resp = await client.get(url, headers=headers)
        resp.raise_for_status()
        payload = resp.json()

    items: list[dict] = []
    for m in payload.get("data", []):
        entry: dict[str, Any] = {"id": m.get("id")}
        # vLLM 较新版本会在模型对象里直接带 max_model_len
        if m.get("max_model_len") is not None:
            entry["max_model_len"] = int(m["max_model_len"])
        items.append(entry)
    return items


async def fetch_vllm_max_model_len() -> dict[str, int]:
    """可选：从 vLLM Prometheus /metrics 抓取真实 max_model_len。

    返回 {served_model_name: max_model_len}。抓取失败返回空 dict（best-effort）。
    """
    if not settings.model_sync_vllm_metrics_url:
        return {}
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.get(settings.model_sync_vllm_metrics_url)
            resp.raise_for_status()
            text = resp.text
    except Exception as exc:  # noqa: BLE001 - best-effort，失败不影响主流程
        logger.warning("抓取 vLLM metrics 失败，context_length 将不自动更新: %s", exc)
        return {}

    result: dict[str, int] = {}
    for match in _MAX_MODEL_LEN_RE.finditer(text):
        served_name, val = match.group(1), int(match.group(2))
        result[served_name] = val
    return result


async def fetch_vllm_models() -> dict[str, int]:
    """从 vLLM 原生 /v1/models 抓取真实 max_model_len。

    vLLM（含 0.25.1）的 /v1/models 每个模型对象直接带 ``max_model_len`` 字段，
    这是真实上下文窗口长度（如 310000），比 LiteLLM 透传的 /v1/models 更权威
    （LiteLLM 透传会丢失该字段）。

    注意：该端点的 model id 是 vLLM 的 served name（如 ``qwen3.6-27b``），与 PLLM
    的 ``model_name``（LiteLLM 别名，如 ``qwen3.6-local``）可能不同，匹配时需用
    ``model_artifact`` 去前缀桥接（见 ``_resolve_context_length``）。

    返回 {served_model_name: max_model_len}。抓取失败返回空 dict（best-effort）。
    """
    url = settings.model_sync_vllm_models_url
    if not url:
        return {}
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.get(url)
            resp.raise_for_status()
            payload = resp.json()
    except Exception as exc:  # noqa: BLE001
        logger.warning("抓取 vLLM /v1/models 失败，context_length 将不自动更新: %s", exc)
        return {}

    result: dict[str, int] = {}
    for m in payload.get("data", []):
        served = m.get("id")
        ml = m.get("max_model_len")
        if served and ml is not None:
            result[served] = int(ml)
    return result


def _match_upstream(model: dict, upstream_ids: set[str]) -> str | None:
    """判断已登记模型是否在上游列表中，返回命中的上游 id（未命中返回 None）。

    匹配键优先级：model_name（即客户端传给 LiteLLM 的 served name）>
    model_artifact（HF 路径，作为兜底）。
    """
    name = model.get("model_name")
    artifact = model.get("model_artifact")
    if name and name in upstream_ids:
        return name
    if artifact and artifact in upstream_ids:
        return artifact
    return None


def _resolve_context_length(model: dict, max_len_map: dict[str, int]) -> int | None:
    """从 max_len_map 中按候选 key 解析真实上下文长度。

    候选 key 优先级：model_name（LiteLLM 别名）> model_artifact（可能带 provider
    前缀，如 ``openai/qwen3.6-27b``）> model_artifact 去前缀
    （``qwen3.6-27b``，匹配 vLLM served name）。首个命中即返回；均未命中返回 None。
    """
    name = model.get("model_name")
    artifact = model.get("model_artifact")
    candidates: list[str] = []
    if name:
        candidates.append(name)
    if artifact:
        candidates.append(artifact)
        candidates.append(artifact.split("/")[-1])
    for key in candidates:
        if key and key in max_len_map:
            return max_len_map[key]
    return None


async def sync_models_from_upstream() -> dict:
    """执行一次同步，回写 models 表。返回摘要。"""
    upstream = await fetch_upstream_models()
    upstream_ids = {m["id"] for m in upstream if m.get("id")}
    # 真实 max_model_len：优先上游 /v1/models 自带，否则用 metrics 抓取
    max_len_map: dict[str, int] = {}
    for m in upstream:
        if m.get("max_model_len"):
            max_len_map[m["id"]] = int(m["max_model_len"])
    # vLLM 原生 /v1/models 是真实 max_model_len 的权威来源（优先级高）
    max_len_map.update(await fetch_vllm_models())
    # vLLM Prometheus metrics（0.25.1 无此指标，保留兼容 best-effort）
    max_len_map.update(await fetch_vllm_max_model_len())

    summary = {
        "synced": 0,
        "failed": 0,
        "created": 0,
        "upstream_models": sorted(upstream_ids),
        "errors": [],
    }

    async with db.pool.acquire() as conn:
        registered = await ModelRepo.list_all(conn)
        for model in registered:
            matched_id = _match_upstream(model, upstream_ids)
            if matched_id:
                update: dict[str, Any] = {"sync_status": "synced"}
                if settings.model_sync_disable_missing:
                    update["is_enabled"] = True
                ctx = _resolve_context_length(model, max_len_map)
                if ctx:
                    update["context_length"] = ctx
                update["last_synced_at"] = datetime.now(timezone.utc)
                await ModelRepo.update_row(
                    conn, model["model_name"], model["tier"], **update
                )
                summary["synced"] += 1
            else:
                update = {
                    "sync_status": "failed",
                    "last_synced_at": datetime.now(timezone.utc),
                }
                if settings.model_sync_disable_missing:
                    update["is_enabled"] = False
                await ModelRepo.update_row(
                    conn, model["model_name"], model["tier"], **update
                )
                summary["failed"] += 1

        # 可选：自动登记上游有、PLLM 没有的模型
        if settings.model_sync_auto_create:
            registered_names = {
                (m.get("model_name"), m.get("tier")) for m in registered
            }
            for m in upstream:
                uid = m.get("id")
                if not uid or uid in {n for n, _ in registered_names}:
                    continue
                try:
                    await ModelRepo.register(
                        conn,
                        model_name=uid,
                        tier="medium",
                        model_artifact=uid,
                        inference_engine="vllm",
                        context_length=max_len_map.get(uid, 0) or 32768,
                        api_base=None,
                        runtime_params=None,
                        request_params=None,
                        is_current=False,
                        is_enabled=True,
                    )
                    summary["created"] += 1
                except Exception as exc:  # noqa: BLE001
                    summary["errors"].append(f"auto_create {uid}: {exc}")

    logger.info(
        "模型同步完成: synced=%d failed=%d created=%d",
        summary["synced"], summary["failed"], summary["created"],
    )
    return summary
