"""监控大盘 API - 对外暴露只读聚合接口 + 内置看板页面

- GET /admin/dashboard/snapshot : 返回全量监控数据 JSON（外部前端对接用，按需筛选）
- GET /admin/dashboard          : 返回自包含 HTML 看板（自动注入 token）

认证：使用专用只读 token（PLLM_DASHBOARD_TOKEN），通过
`Authorization: Bearer <token>` 或 `X-Dashboard-Token: <token>` 头传递。
该路径已在签名中间件中豁免 Ed25519 校验。

设计原则：后端返回尽量全的明细数据（时间序列、按模型/调用方的延迟与错误、
最近用量样本、最近安全事件、主机/容器/GPU 系统资源），前端只负责按需筛选/聚合。

系统资源采集依赖容器挂载：
  /host/proc   -> 主机 /proc（只读，取 CPU/内存/负载/网络/进程）
  /hostfs      -> 主机根文件系统（只读，取磁盘用量）
  /var/run/docker.sock -> Docker API（取各容器 CPU/内存）
  nvidia 设备预留 -> 容器内可执行 nvidia-smi（取 GPU 利用率/显存）
若上述挂载缺失，对应字段会优雅降级（available=false），不影响业务数据。
"""

from __future__ import annotations

import asyncio
import json as _json
import os
import socket as _socket
import subprocess
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from typing import Optional

import http.client as _http
from fastapi import APIRouter, Query, Request, HTTPException, status
from fastapi.responses import HTMLResponse

from ..config import settings
from ..db import db

router = APIRouter(prefix="/admin/dashboard", tags=["Dashboard"])


# ========== token 校验 ==========

def _extract_token(request: Request) -> Optional[str]:
    auth = request.headers.get("authorization")
    if auth and auth.lower().startswith("bearer "):
        return auth[7:].strip()
    return request.headers.get("x-dashboard-token")


# ========== LiteLLM 连通性探测 ==========

async def _check_litellm() -> bool:
    url = settings.litellm_api_base.rstrip("/")

    def _probe() -> bool:
        try:
            with urllib.request.urlopen(url, timeout=2):
                return True
        except urllib.error.HTTPError:
            return True
        except Exception:
            return False

    try:
        return await asyncio.to_thread(_probe)
    except Exception:
        return False


# ========== 推理引擎实时并发采集（运行中 + 排队中请求数） ==========
# 注意：这里的“请求数量”特指推理引擎当前 正在运行 + 排队等待 的请求，
# 与“API Key / Token 数量”是两个完全不同的概念，切勿混用。
_VLLM_METRICS_URL = "http://host.docker.internal:8000/metrics"
_LITELLM_METRICS_URL = "http://host.docker.internal:4000/metrics"


def _http_get_text(url: str, timeout: float = 3.0) -> Optional[str]:
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "pllm-dashboard"})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.read().decode("utf-8", "replace")
    except Exception:
        return None


def _metric_label(line: str, name: str) -> Optional[str]:
    import re

    m = re.search(name + r'="([^"]*)"', line)
    return m.group(1) if m else None


def _metric_value(line: str) -> float:
    try:
        return float(line.rsplit(" ", 1)[1])
    except Exception:
        return 0.0


def _parse_vllm_metrics(text: str) -> Optional[dict]:
    """解析 vLLM Prometheus 指标，提取：
      - running / waiting 请求数（请求数量语义）
      - KV cache 使用率、prefix cache 命中率、cache 配置
    """
    import re as _re

    running: dict = {}
    waiting: dict = {}
    waiting_by_reason: dict = {}
    kv_usage: dict = {}
    prefix_queries: dict = {}
    prefix_hits: dict = {}
    cache_config: dict = {}
    try:
        for line in text.splitlines():
            if line.startswith("#"):
                continue
            if line.startswith("vllm:num_requests_running{"):
                model = _metric_label(line, "model_name")
                if model is not None:
                    running[model] = int(_metric_value(line))
            elif line.startswith("vllm:num_requests_waiting{"):
                model = _metric_label(line, "model_name")
                if model is not None:
                    waiting[model] = int(_metric_value(line))
            elif line.startswith("vllm:num_requests_waiting_by_reason{"):
                model = _metric_label(line, "model_name")
                reason = _metric_label(line, "reason")
                if model is not None and reason is not None:
                    waiting_by_reason.setdefault(model, {})[reason] = int(_metric_value(line))
            elif line.startswith("vllm:kv_cache_usage_perc{"):
                model = _metric_label(line, "model_name")
                if model is not None:
                    kv_usage[model] = _metric_value(line)
            elif line.startswith("vllm:prefix_cache_queries_total{"):
                model = _metric_label(line, "model_name")
                if model is not None:
                    prefix_queries[model] = int(_metric_value(line))
            elif line.startswith("vllm:prefix_cache_hits_total{"):
                model = _metric_label(line, "model_name")
                if model is not None:
                    prefix_hits[model] = int(_metric_value(line))
            elif line.startswith("vllm:cache_config_info{"):
                for k, v in _re.findall(r'(\w+)="([^"]*)"', line):
                    cache_config[k] = v
    except Exception:
        return None
    if not running and not waiting and not kv_usage:
        return None
    models = []
    for m in sorted(set(list(running) + list(waiting) + list(kv_usage))):
        models.append({
            "model": m,
            "running": running.get(m, 0),
            "waiting": waiting.get(m, 0),
            "total_inflight": running.get(m, 0) + waiting.get(m, 0),
        })
    # KV cache 汇总
    def _to_num(v, cast=float):
        try:
            return cast(v)
        except Exception:
            return None

    total_q = sum(prefix_queries.values())
    total_h = sum(prefix_hits.values())
    kv_by_model = []
    for m in sorted(kv_usage):
        q = prefix_queries.get(m, 0)
        h = prefix_hits.get(m, 0)
        kv_by_model.append({
            "model": m,
            "usage_perc": kv_usage.get(m, 0.0),
            "prefix_queries": q,
            "prefix_hits": h,
            "prefix_hit_rate": (h / q) if q else None,
        })
    kv_cache = {
        "usage_perc": next(iter(kv_usage.values())) if kv_usage else 0.0,
        "by_model": kv_by_model,
        "prefix_cache": {
            "queries": total_q,
            "hits": total_h,
            "hit_rate": (total_h / total_q) if total_q else None,
        },
        "config": {
            "num_gpu_blocks": _to_num(cache_config.get("num_gpu_blocks"), int),
            "kv_cache_size_tokens": _to_num(cache_config.get("kv_cache_size_tokens"), int),
            "gpu_memory_utilization": _to_num(cache_config.get("gpu_memory_utilization")),
            "block_size": _to_num(cache_config.get("block_size"), int),
            "cache_dtype": cache_config.get("cache_dtype"),
            "enable_prefix_caching": cache_config.get("enable_prefix_caching"),
        },
    }
    return {
        "engine": "vllm",
        "running_requests": sum(running.values()),
        "queued_requests": sum(waiting.values()),
        "total_inflight": sum(running.values()) + sum(waiting.values()),
        "waiting_by_reason": waiting_by_reason,
        "models": models,
        "kv_cache": kv_cache,
    }


async def collect_inference_metrics() -> dict:
    """采集推理引擎实时并发：运行中 + 排队中请求数。

    优先取 vLLM（GPU 真实并发的 ground truth，PLLM→LiteLLM→vLLM 链路中
    vLLM 才是真正占用 GPU 的层）。若 vLLM 不可达则优雅降级 available=false。
    """
    result = {
        "available": False,
        "error": None,
        "engine": None,
        "running_requests": 0,
        "queued_requests": 0,
        "total_inflight": 0,
        "waiting_by_reason": {},
        "models": [],
        "kv_cache": {
            "usage_perc": None,
            "by_model": [],
            "prefix_cache": {},
            "config": {},
        },
    }
    try:
        text = await asyncio.to_thread(_http_get_text, _VLLM_METRICS_URL, 3.0)
        if text:
            parsed = _parse_vllm_metrics(text)
            if parsed:
                result.update(parsed)
                result["available"] = True
        if not result["available"]:
            result["error"] = "vLLM metrics 不可达或无数据（host.docker.internal:8000/metrics）"
    except Exception as e:  # noqa: BLE001
        result["error"] = str(e)[:200]
    return result


# ========== 业务聚合查询（全量明细） ==========

async def build_snapshot(window_hours: int = 24) -> dict:
    since = datetime.now(timezone.utc) - timedelta(hours=window_hours)
    month_start = datetime.now(timezone.utc).date().replace(day=1)
    # 窗口 <= 72h 用小时桶，否则用天桶，避免点数过多
    bucket_unit = "hour" if window_hours <= 72 else "day"

    async with db.pool.acquire() as conn:
        totals = await conn.fetchrow(
            """SELECT
                   COUNT(*)                                            AS requests,
                   COUNT(*) FILTER (WHERE status_code < 400)           AS success,
                   COUNT(*) FILTER (WHERE status_code >= 400)          AS failed,
                   COALESCE(SUM(total_tokens), 0)::bigint              AS total_tokens,
                   COALESCE(SUM(prompt_tokens), 0)::bigint             AS prompt_tokens,
                   COALESCE(SUM(completion_tokens), 0)::bigint         AS completion_tokens
               FROM usage_logs WHERE created_at >= $1""",
            since,
        )
        latency = await conn.fetchrow(
            """SELECT
                   AVG(latency_ms)                                          AS avg_ms,
                   percentile_cont(0.95) WITHIN GROUP (ORDER BY latency_ms) AS p95_ms,
                   percentile_cont(0.99) WITHIN GROUP (ORDER BY latency_ms) AS p99_ms
               FROM usage_logs
               WHERE created_at >= $1 AND latency_ms IS NOT NULL""",
            since,
        )
        # 时间序列（前端可做趋势/区间筛选）
        timeseries = await conn.fetch(
            f"""SELECT date_trunc($2, created_at)                    AS bucket,
                       COUNT(*)                                      AS requests,
                       COALESCE(SUM(total_tokens), 0)::bigint        AS tokens,
                       COUNT(*) FILTER (WHERE status_code >= 400)    AS errors,
                       ROUND(AVG(latency_ms), 1)                     AS avg_latency_ms
               FROM usage_logs WHERE created_at >= $1
               GROUP BY bucket ORDER BY bucket""",
            since, bucket_unit,
        )
        # 按模型（含延迟/错误）
        by_model = await conn.fetch(
            """SELECT model,
                      COUNT(*)                                       AS requests,
                      COALESCE(SUM(total_tokens), 0)::bigint          AS tokens,
                      ROUND(AVG(latency_ms), 1)                       AS avg_latency_ms,
                      COUNT(*) FILTER (WHERE status_code >= 400)      AS errors
               FROM usage_logs WHERE created_at >= $1
               GROUP BY model ORDER BY tokens DESC""",
            since,
        )
        # 按调用方（含延迟/错误/错误率）
        by_subject = await conn.fetch(
            """SELECT COALESCE(subject_id_snapshot, 'unknown') AS subject,
                      COUNT(*)                                       AS requests,
                      COALESCE(SUM(total_tokens), 0)::bigint          AS tokens,
                      ROUND(AVG(latency_ms), 1)                       AS avg_latency_ms,
                      COUNT(*) FILTER (WHERE status_code >= 400)      AS errors
               FROM usage_logs WHERE created_at >= $1
               GROUP BY subject_id_snapshot ORDER BY tokens DESC""",
            since,
        )
        # 最近用量样本（前端可任意筛选）
        recent_usage = await conn.fetch(
            """SELECT created_at, model,
                      subject_id_snapshot                            AS subject,
                      tier_snapshot                                  AS tier,
                      total_tokens, latency_ms, status_code
               FROM usage_logs WHERE created_at >= $1
               ORDER BY created_at DESC LIMIT 100""",
            since,
        )
        # 配额预算（本月）
        budgets = await conn.fetch(
            """SELECT t.subject_id, t.name,
                      t.token_budget, t.token_budget_period,
                      COALESCE(c.used_tokens, 0)::bigint AS used
               FROM pllm_tokens t
               LEFT JOIN usage_counters c
                 ON c.pllm_token_id = t.pllm_token_id
                AND c.period_type = 'monthly'
                AND c.period_start = $1
               WHERE t.is_active = TRUE
               ORDER BY t.subject_id NULLS LAST""",
            month_start,
        )
        # 安全统计 + 最近安全事件样本
        security = await conn.fetchrow(
            """SELECT
                   COUNT(*) FILTER (WHERE decision = 'accepted')        AS signature_ok,
                   COUNT(*) FILTER (WHERE decision = 'rejected')        AS signature_fail,
                   COUNT(*) FILTER (WHERE reason_code ILIKE '%replay%') AS replays
               FROM security_event_logs WHERE occurred_at >= $1""",
            since,
        )
        security_recent = await conn.fetch(
            """SELECT occurred_at, endpoint, decision, reason_code, source_address
               FROM security_event_logs WHERE occurred_at >= $1
               ORDER BY occurred_at DESC LIMIT 50""",
            since,
        )
        models = await conn.fetch(
            """SELECT model_name, tier, is_current, is_enabled, sync_status
               FROM models ORDER BY model_name, tier""",
        )
        # 密钥（API Key / Token）数量 —— 与“请求数量”完全无关，单独统计以免混淆
        keys = await conn.fetchrow(
            """SELECT COUNT(*) FILTER (WHERE is_active = TRUE) AS active,
                      COUNT(*)                              AS total
               FROM pllm_tokens"""
        )

    requests = int(totals["requests"]) if totals else 0
    failed = int(totals["failed"]) if totals else 0
    error_rate = round(failed / requests, 4) if requests else 0.0

    budget_list = []
    for b in budgets:
        budget = b["token_budget"]
        used = int(b["used"])
        if budget and budget > 0:
            pct = round(used / budget * 100, 1)
            if used >= budget:
                status_ = "exhausted"
            elif pct >= 90:
                status_ = "warning"
            else:
                status_ = "ok"
            remaining = max(budget - used, 0)
        else:
            pct = None
            status_ = "unlimited"
            remaining = None
        budget_list.append({
            "subject": b["subject_id"] or "—",
            "name": b["name"],
            "token_budget": budget,
            "token_budget_period": b["token_budget_period"],
            "used": used,
            "remaining": remaining,
            "pct": pct,
            "status": status_,
        })

    by_model_list = [
        {
            "model": r["model"],
            "requests": int(r["requests"]),
            "tokens": int(r["tokens"]),
            "avg_latency_ms": float(r["avg_latency_ms"]) if r["avg_latency_ms"] else 0,
            "errors": int(r["errors"]),
        }
        for r in by_model
    ]
    by_subject_list = [
        {
            "subject": r["subject"],
            "requests": int(r["requests"]),
            "tokens": int(r["tokens"]),
            "avg_latency_ms": float(r["avg_latency_ms"]) if r["avg_latency_ms"] else 0,
            "errors": int(r["errors"]),
            "error_rate": round(int(r["errors"]) / int(r["requests"]), 4) if r["requests"] else 0,
        }
        for r in by_subject
    ]
    recent_usage_list = [
        {
            "created_at": r["created_at"],
            "model": r["model"],
            "subject": r["subject"] or "—",
            "tier": r["tier"],
            "total_tokens": int(r["total_tokens"]) if r["total_tokens"] else 0,
            "latency_ms": int(r["latency_ms"]) if r["latency_ms"] else 0,
            "status_code": r["status_code"],
        }
        for r in recent_usage
    ]
    security_recent_list = [
        {
            "occurred_at": r["occurred_at"],
            "endpoint": r["endpoint"],
            "decision": r["decision"],
            "reason_code": r["reason_code"],
            "source_address": r["source_address"],
        }
        for r in security_recent
    ]

    # 系统资源（主机/容器/GPU）—— 独立采集，失败不影响业务数据
    system = await collect_system_metrics()
    # 推理引擎实时并发（运行中 + 排队中请求数）
    inference = await collect_inference_metrics()

    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "window": f"{window_hours}h",
        "bucket_unit": bucket_unit,
        "totals": {
            "requests": requests,
            "success": int(totals["success"]) if totals else 0,
            "failed": failed,
            "error_rate": error_rate,
            "total_tokens": int(totals["total_tokens"]) if totals else 0,
            "prompt_tokens": int(totals["prompt_tokens"]) if totals else 0,
            "completion_tokens": int(totals["completion_tokens"]) if totals else 0,
        },
        "latency": {
            "avg_ms": round(float(latency["avg_ms"]), 1) if latency and latency["avg_ms"] else 0,
            "p95_ms": round(float(latency["p95_ms"]), 1) if latency and latency["p95_ms"] else 0,
            "p99_ms": round(float(latency["p99_ms"]), 1) if latency and latency["p99_ms"] else 0,
        },
        "timeseries": [
            {
                "bucket": r["bucket"],
                "requests": int(r["requests"]),
                "tokens": int(r["tokens"]),
                "errors": int(r["errors"]),
                "avg_latency_ms": float(r["avg_latency_ms"]) if r["avg_latency_ms"] else 0,
            }
            for r in timeseries
        ],
        "by_model": by_model_list,
        "by_subject": by_subject_list,
        "recent_usage": recent_usage_list,
        "budgets": budget_list,
        "security": {
            "signature_ok": int(security["signature_ok"]) if security else 0,
            "signature_fail": int(security["signature_fail"]) if security else 0,
            "replays": int(security["replays"]) if security else 0,
            "recent": security_recent_list,
        },
        "models": [
            {
                "model_name": m["model_name"],
                "tier": m["tier"],
                "is_current": m["is_current"],
                "is_enabled": m["is_enabled"],
                "sync_status": m["sync_status"],
            }
            for m in models
        ],
        "system": system,
        "inference": inference,
        "keys": {
            "active": int(keys["active"]) if keys else 0,
            "total": int(keys["total"]) if keys else 0,
        },
        "health": {
            "db_ok": True,
            "litellm_reachable": await _check_litellm(),
        },
    }


# ========== 系统资源采集（主机 / 容器 / GPU） ==========

_HOST_PROC = "/host/proc"
_HOST_FS = "/hostfs"
_DOCKER_SOCK = "/var/run/docker.sock"
_SYS_STATE: dict = {"cpu_prev": None, "cpu_ts": 0.0, "net_prev": None, "net_ts": 0.0}


def _read_proc(rel: str) -> str:
    try:
        with open(os.path.join(_HOST_PROC, rel)) as f:
            return f.read()
    except Exception:
        return ""


def _parse_meminfo(text: str) -> dict:
    d: dict = {}
    for ln in text.splitlines():
        if ":" in ln:
            k, v = ln.split(":", 1)
            try:
                d[k.strip()] = int(v.split()[0]) * 1024  # kB -> bytes
            except Exception:
                pass
    return d


def _cpu_idle(line: str):
    parts = list(map(int, line.split()[1:]))
    # user nice system idle iowait irq softirq steal guest guest_nice
    idle = parts[3] + (parts[4] if len(parts) > 4 else 0)
    total = sum(parts)
    return idle, total


def _net_dev(text: str):
    rx = tx = 0
    for ln in text.splitlines()[2:]:
        if ":" not in ln:
            continue
        cols = ln.split(":", 1)[1].split()
        if len(cols) >= 9:
            rx += int(cols[0])
            tx += int(cols[8])
    return rx, tx


def _docker_request(method: str, path: str):
    """通过 unix socket 访问 Docker API（仅依赖标准库）"""
    try:
        class _UnixHTTP(_http.HTTPConnection):
            def connect(self):
                self.sock = _socket.socket(_socket.AF_UNIX, _socket.SOCK_STREAM)
                self.sock.settimeout(3)
                self.sock.connect(_DOCKER_SOCK)

        c = _UnixHTTP("localhost", timeout=3)
        c.request(method, path)
        r = c.getresponse()
        data = r.read()
        c.close()
        if r.status >= 400:
            return None
        return _json.loads(data.decode())
    except Exception:
        return None


def _collect_gpu() -> dict:
    try:
        out = subprocess.run(
            ["nvidia-smi",
             "--query-gpu=index,name,utilization.gpu,memory.used,memory.total,temperature.gpu",
             "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=4,
        )
        if out.returncode != 0 or not out.stdout.strip():
            return {"available": False, "error": (out.stderr or "no output").strip()[:200]}
        gpus = []
        for ln in out.stdout.splitlines():
            idx, name, util, memu, memt, temp = [x.strip() for x in ln.split(",")]
            gpus.append({
                "index": int(idx),
                "name": name,
                "utilization_percent": float(util),
                "memory_used_mb": float(memu),
                "memory_total_mb": float(memt),
                "temperature_c": float(temp),
            })
        return {"available": True, "gpus": gpus}
    except FileNotFoundError:
        return {"available": False, "error": "nvidia-smi not found in container"}
    except Exception as e:  # noqa: BLE001
        return {"available": False, "error": str(e)[:200]}


def _collect_containers() -> list:
    cs = _docker_request("GET", "/containers/json?size=false")
    if not cs:
        return []
    out = []
    for c in cs:
        cid = c.get("Id")
        name = (c.get("Names") or ["?"])[0].lstrip("/")
        stats = _docker_request("GET", f"/containers/{cid}/stats?stream=false")
        cpu_pct = mem = None
        if stats:
            try:
                cs0 = stats["cpu_stats"]
                pcs0 = stats.get("precpu_stats", {}) or {}
                cpu_delta = cs0["cpu_usage"]["total_usage"] - pcs0.get("cpu_usage", {}).get("total_usage", 0)
                sys_delta = cs0.get("system_cpu_usage", 0) - pcs0.get("system_cpu_usage", 0)
                online = cs0.get("online_cpus") or len(cs0.get("percpu_usage") or [1])
                cpu_pct = round((cpu_delta / sys_delta) * online * 100, 1) if sys_delta > 0 else 0.0
            except Exception:
                cpu_pct = None
            try:
                mem = stats["memory_stats"]["usage"]
                cache = stats["memory_stats"].get("stats", {}).get("cache", 0)
                if cache:
                    mem -= cache
            except Exception:
                mem = None
        out.append({
            "name": name,
            "image": c.get("Image"),
            "status": c.get("Status"),
            "cpu_percent": cpu_pct,
            "mem_usage_bytes": mem,
        })
    return out


def _collect_system_sync() -> dict:
    now = time.time()
    result: dict = {"available": False, "error": None, "host": {}, "gpu": {}, "containers": []}
    try:
        stat = _read_proc("stat")
        meminfo = _read_proc("meminfo")
        load = _read_proc("loadavg")
        uptime = _read_proc("uptime")
        net = _read_proc("net/dev")
        try:
            procs = sum(1 for p in os.listdir(_HOST_PROC) if p.isdigit())
        except Exception:
            procs = None

        # CPU 使用率（基于两次采样 delta）
        cur_cpu = _cpu_idle(stat.splitlines()[0])
        cpu_pct = None
        st = _SYS_STATE
        if st["cpu_prev"] is not None:
            dt = now - st["cpu_ts"]
            dtotal = cur_cpu[1] - st["cpu_prev"][1]
            didle = cur_cpu[0] - st["cpu_prev"][0]
            if dt > 0 and dtotal > 0:
                cpu_pct = round((1 - didle / dtotal) * 100, 1)
        st["cpu_prev"] = cur_cpu
        st["cpu_ts"] = now

        # 内存
        mi = _parse_meminfo(meminfo)
        mem_total = mi.get("MemTotal")
        mem_avail = mi.get("MemAvailable") or mi.get("MemFree")
        mem_used = (mem_total - mem_avail) if (mem_total and mem_avail) else None

        # 网络速率（基于两次采样 delta）
        rx, tx = _net_dev(net)
        net_rx_kbps = net_tx_kbps = None
        if st["net_prev"] is not None:
            dt = now - st["net_ts"]
            if dt > 0:
                net_rx_kbps = round((rx - st["net_prev"][0]) / dt / 1024, 1)
                net_tx_kbps = round((tx - st["net_prev"][1]) / dt / 1024, 1)
        st["net_prev"] = (rx, tx)
        st["net_ts"] = now

        load_parts = load.split()
        load1 = float(load_parts[0]) if load_parts else None
        up = float(uptime.split()[0]) if uptime else None

        # 磁盘（根文件系统）
        try:
            fs = os.statvfs(_HOST_FS)
            disk_total = fs.f_blocks * fs.f_frsize
            disk_free = fs.f_bfree * fs.f_frsize
            disk_used = disk_total - disk_free
        except Exception:
            disk_total = disk_free = disk_used = None

        result["host"] = {
            "cpu_percent": cpu_pct,
            "mem_total_bytes": mem_total,
            "mem_used_bytes": mem_used,
            "mem_available_bytes": mem_avail,
            "disk_total_bytes": disk_total,
            "disk_used_bytes": disk_used,
            "disk_free_bytes": disk_free,
            "net_rx_bytes": rx,
            "net_tx_bytes": tx,
            "net_rx_kbps": net_rx_kbps,
            "net_tx_kbps": net_tx_kbps,
            "load_1m": load1,
            "uptime_seconds": up,
            "process_count": procs,
        }
        result["gpu"] = _collect_gpu()
        result["containers"] = _collect_containers()
        result["available"] = True
    except Exception as e:  # noqa: BLE001
        result["error"] = str(e)
    return result


async def collect_system_metrics() -> dict:
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, _collect_system_sync)


# ========== 路由 ==========

@router.get("/snapshot")
async def dashboard_snapshot(
    request: Request,
    window: int = Query(24, ge=1, le=720, description="统计窗口(小时)"),
):
    """只读监控聚合接口（供外部前端拉取）"""
    expected = settings.dashboard_token
    token = _extract_token(request)
    if not expected or token != expected:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Invalid or missing dashboard token",
        )
    return await build_snapshot(window)


# ========== 内置 HTML 看板 ==========

_DASHBOARD_HTML = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8" />
<meta name="viewport" content="width=device-width, initial-scale=1.0" />
<title>PLLM 监控大盘</title>
<script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.1/dist/chart.umd.min.js"></script>
<style>
  :root { --bg:#0f1420; --card:#1a2233; --txt:#e6ecf5; --muted:#8b97ad; --line:#27324a;
          --ok:#3ecf8e; --warn:#f2c14e; --bad:#ff6b6b; --accent:#5b8cff; }
  * { box-sizing:border-box; }
  body { margin:0; background:var(--bg); color:var(--txt);
         font-family:-apple-system,"Segoe UI",Roboto,"PingFang SC",sans-serif; }
  header { padding:18px 24px; display:flex; align-items:center; justify-content:space-between;
           border-bottom:1px solid var(--line); flex-wrap:wrap; gap:10px; }
  header h1 { font-size:18px; margin:0; }
  .meta { color:var(--muted); font-size:13px; display:flex; gap:12px; align-items:center; }
  .winbtns button { background:transparent; border:1px solid var(--line); color:var(--muted); margin-left:6px; }
  .winbtns button.active { background:var(--accent); color:#fff; border-color:var(--accent); }
  .wrap { padding:20px 24px; }
  .grid { display:grid; gap:14px; grid-template-columns:repeat(auto-fit,minmax(150px,1fr)); margin-bottom:18px; }
  .kpi { background:var(--card); border:1px solid var(--line); border-radius:12px; padding:16px; }
  .kpi .label { color:var(--muted); font-size:12px; }
  .kpi .value { font-size:24px; font-weight:700; margin-top:6px; }
  .row { display:grid; gap:14px; grid-template-columns:1fr 1fr; margin-bottom:18px; }
  @media (max-width:900px){ .row { grid-template-columns:1fr; } }
  .panel { background:var(--card); border:1px solid var(--line); border-radius:12px; padding:16px; overflow:auto; }
  .panel h2 { font-size:14px; margin:0 0 12px; color:var(--muted); font-weight:600; }
  table { width:100%; border-collapse:collapse; font-size:13px; }
  th,td { text-align:left; padding:7px 10px; border-bottom:1px solid var(--line); white-space:nowrap; }
  th { color:var(--muted); font-weight:600; position:sticky; top:0; background:var(--card); }
  .bar { height:8px; background:var(--line); border-radius:4px; overflow:hidden; min-width:80px; }
  .bar > span { display:block; height:100%; }
  .tag { padding:2px 8px; border-radius:999px; font-size:12px; }
  .tag.ok { background:rgba(62,207,142,.15); color:var(--ok); }
  .tag.warning { background:rgba(242,193,78,.15); color:var(--warn); }
  .tag.exhausted { background:rgba(255,107,107,.15); color:var(--bad); }
  .tag.unlimited { background:rgba(91,140,255,.15); color:var(--accent); }
  .dot { display:inline-block; width:9px; height:9px; border-radius:50%; margin-right:6px; }
  .on { background:var(--ok); } .off { background:var(--bad); }
  .err { color:var(--bad); padding:10px 0; }
  .tblscroll { max-height:320px; overflow:auto; }
</style>
</head>
<body>
<header>
  <h1>PLLM 监控大盘</h1>
  <div class="meta">
    <span id="updated">—</span>
    <span class="winbtns" id="winbtns">
      <button data-w="1">1h</button>
      <button data-w="6">6h</button>
      <button data-w="24" class="active">24h</button>
      <button data-w="168">7d</button>
      <button data-w="720">30d</button>
    </span>
    <button onclick="load()">刷新</button>
  </div>
</header>
<div class="wrap">
  <div id="err" class="err"></div>
  <div class="grid" id="kpis"></div>
  <div class="row">
    <div class="panel"><h2>用量时间序列 (tokens / 请求)</h2><canvas id="chartTs" height="120"></canvas></div>
    <div class="panel"><h2>按模型 Token 分布</h2><canvas id="chartModel" height="120"></canvas></div>
  </div>
  <div class="row">
    <div class="panel"><h2>按调用方(subject) Token 分布</h2><canvas id="chartSubject" height="120"></canvas></div>
    <div class="panel"><h2>配额预算 (本月)</h2><div id="budgets"></div></div>
  </div>
  <div class="row">
    <div class="panel"><h2>按模型明细</h2><div class="tblscroll"><table id="tblModel"></table></div></div>
    <div class="panel"><h2>按调用方明细</h2><div class="tblscroll"><table id="tblSubject"></table></div></div>
  </div>
  <div class="row">
    <div class="panel"><h2>最近用量样本 (最多100)</h2><div class="tblscroll"><table id="tblRecent"></table></div></div>
    <div class="panel"><h2>安全 / 审计 + 最近事件</h2><div id="security"></div><div class="tblscroll"><table id="tblSec"></table></div></div>
  </div>
  <div class="row">
    <div class="panel"><h2>系统资源 (主机)</h2><div id="system"></div></div>
    <div class="panel"><h2>GPU</h2><div id="gpu"></div></div>
  </div>
  <div class="row">
    <div class="panel"><h2>推理实时并发 (运行中 + 排队中) — 即“请求数量”</h2><div id="inference"></div></div>
    <div class="panel"><h2>依赖健康</h2><div id="health"></div></div>
  </div>
  <div class="row">
    <div class="panel"><h2>KV Cache 缓存 (vLLM)</h2><div id="kvcache"></div></div>
  </div>
  <div class="panel"><h2>容器资源</h2><div class="tblscroll"><table id="tblContainers"></table></div></div>
  <div class="panel"><h2>模型登记</h2><div id="models"></div></div>
</div>
<script>
const TOKEN = "__TOKEN__";
const ENDPOINT = "/admin/dashboard/snapshot";
let curWindow = 24;
let chartTs, chartModel, chartSubject;

document.querySelectorAll("#winbtns button").forEach(b => {
  b.onclick = () => {
    document.querySelectorAll("#winbtns button").forEach(x => x.classList.remove("active"));
    b.classList.add("active");
    curWindow = Number(b.dataset.w);
    load();
  };
});

async function load(){
  const errEl = document.getElementById("err");
  errEl.textContent = "";
  try {
    const r = await fetch(ENDPOINT + "?window=" + curWindow, { headers: { "Authorization": "Bearer " + TOKEN } });
    if (!r.ok) { errEl.textContent = "接口返回 " + r.status + "：" + (await r.text()).slice(0,200); return; }
    const d = await r.json();
    render(d);
    document.getElementById("updated").textContent = "更新于 " + new Date().toLocaleTimeString();
  } catch(e){ errEl.textContent = "请求失败：" + e.message; }
}

function fmt(n){ return (n==null?0:n).toLocaleString(); }
function pct(n){ return (n*100).toFixed(2) + "%"; }
function fmtUptime(s){ if(s==null) return "–"; s=Math.floor(s); const d=Math.floor(s/86400), h=Math.floor(s%86400/3600), m=Math.floor(s%3600/60); return (d>0?d+"天 ":"")+h+"时"+m+"分"; }

function render(d){
  const t = d.totals, l = d.latency;
  const inf = d.inference || {};
  const keys = d.keys || {};
  const kpis = [
    ["窗口请求数", fmt(t.requests)], ["Token 总量", fmt(t.total_tokens)],
    ["错误率", pct(t.error_rate)], ["平均延迟", l.avg_ms + " ms"],
    ["P95 延迟", l.p95_ms + " ms"], ["P99 延迟", l.p99_ms + " ms"],
    ["运行中请求", fmt(inf.running_requests||0)], ["排队中请求", fmt(inf.queued_requests||0)],
    ["在途请求合计", fmt(inf.total_inflight||0)], ["活跃密钥数", fmt(keys.active||0) + " / " + fmt(keys.total||0)],
  ];
  if (d.system && d.system.available && d.system.host){
    const h = d.system.host;
    const mp = h.mem_total_bytes ? (h.mem_used_bytes/h.mem_total_bytes*100).toFixed(1) : "–";
    kpis.push(["主机CPU", (h.cpu_percent!=null?h.cpu_percent:"–") + "%"]);
    kpis.push(["主机内存", mp + "%"]);
  }
  document.getElementById("kpis").innerHTML = kpis.map(k =>
    `<div class="kpi"><div class="label">${k[0]}</div><div class="value">${k[1]}</div></div>`).join("");

  // timeseries
  drawLine("chartTs", chartTs,
    (d.timeseries||[]).map(x => x.bucket),
    [
      { label:"tokens", data:(d.timeseries||[]).map(x=>x.tokens), yAxis:'y' },
      { label:"requests", data:(d.timeseries||[]).map(x=>x.requests), yAxis:'y1' },
    ], c => chartTs=c);

  // by model / subject bars
  drawBar("chartModel", chartModel, (d.by_model||[]).map(x=>({label:x.model, v:x.tokens})), c=>chartModel=c);
  drawBar("chartSubject", chartSubject, (d.by_subject||[]).map(x=>({label:x.subject, v:x.tokens})), c=>chartSubject=c);

  // budgets
  document.getElementById("budgets").innerHTML = (d.budgets||[]).length
    ? `<table><tr><th>调用方</th><th>预算</th><th>已用</th><th>剩余</th><th></th><th>状态</th></tr>` +
      (d.budgets||[]).map(b => {
        const p = b.pct==null ? 0 : b.pct;
        const color = b.status==="exhausted" ? "var(--bad)" : b.status==="warning" ? "var(--warn)" : "var(--ok)";
        const remain = b.remaining==null ? "∞" : fmt(b.remaining);
        const budget = b.token_budget==null ? "∞" : fmt(b.token_budget);
        return `<tr><td>${b.subject}</td><td>${budget}</td><td>${fmt(b.used)}</td><td>${remain}</td>
                <td><div class="bar"><span style="width:${p}%;background:${color}"></span></div></td>
                <td><span class="tag ${b.status}">${b.status}</span></td></tr>`;
      }).join("") + `</table>`
    : `<div class="meta">无激活 Token</div>`;

  // by_model table
  document.getElementById("tblModel").innerHTML =
    `<tr><th>模型</th><th>请求</th><th>Tokens</th><th>平均延迟</th><th>错误</th></tr>` +
    (d.by_model||[]).map(m=>`<tr><td>${m.model}</td><td>${fmt(m.requests)}</td><td>${fmt(m.tokens)}</td>
      <td>${m.avg_latency_ms} ms</td><td>${fmt(m.errors)}</td></tr>`).join("");

  // by_subject table
  document.getElementById("tblSubject").innerHTML =
    `<tr><th>调用方</th><th>请求</th><th>Tokens</th><th>平均延迟</th><th>错误率</th></tr>` +
    (d.by_subject||[]).map(s=>`<tr><td>${s.subject}</td><td>${fmt(s.requests)}</td><td>${fmt(s.tokens)}</td>
      <td>${s.avg_latency_ms} ms</td><td>${pct(s.error_rate)}</td></tr>`).join("");

  // recent usage
  document.getElementById("tblRecent").innerHTML =
    `<tr><th>时间</th><th>模型</th><th>调用方</th><th>档位</th><th>Tokens</th><th>延迟</th><th>状态</th></tr>` +
    (d.recent_usage||[]).map(r=>`<tr><td>${new Date(r.created_at).toLocaleString()}</td><td>${r.model}</td>
      <td>${r.subject}</td><td>${r.tier||"—"}</td><td>${fmt(r.total_tokens)}</td><td>${r.latency_ms} ms</td>
      <td>${r.status_code}</td></tr>`).join("");

  // security
  const s = d.security||{};
  document.getElementById("security").innerHTML = `<table>
    <tr><th>签名通过</th><td style="color:var(--ok)">${fmt(s.signature_ok)}</td></tr>
    <tr><th>签名失败</th><td style="color:var(--bad)">${fmt(s.signature_fail)}</td></tr>
    <tr><th>疑似重放</th><td>${fmt(s.replays)}</td></tr></table>`;
  document.getElementById("tblSec").innerHTML =
    `<tr><th>时间</th><th>端点</th><th>判定</th><th>原因</th></tr>` +
    (s.recent||[]).map(e=>`<tr><td>${new Date(e.occurred_at).toLocaleString()}</td><td>${e.endpoint}</td>
      <td>${e.decision}</td><td>${e.reason_code}</td></tr>`).join("");

  // system resources
  const sys = d.system || {};
  if (sys.available){
    const h = sys.host || {};
    const memPct = h.mem_total_bytes ? (h.mem_used_bytes/h.mem_total_bytes*100).toFixed(1) : "–";
    const diskPct = h.disk_total_bytes ? (h.disk_used_bytes/h.disk_total_bytes*100).toFixed(1) : "–";
    document.getElementById("system").innerHTML = `<table>
      <tr><th>CPU 使用率</th><td>${h.cpu_percent!=null?h.cpu_percent:"–"}%</td><th>负载(1m)</th><td>${h.load_1m!=null?h.load_1m:"–"}</td></tr>
      <tr><th>内存</th><td>${fmt(h.mem_used_bytes)} / ${fmt(h.mem_total_bytes)} (${memPct}%)</td><th>进程数</th><td>${h.process_count!=null?h.process_count:"–"}</td></tr>
      <tr><th>磁盘</th><td>${fmt(h.disk_used_bytes)} / ${fmt(h.disk_total_bytes)} (${diskPct}%)</td><th>运行时长</th><td>${h.uptime_seconds!=null?fmtUptime(h.uptime_seconds):"–"}</td></tr>
      <tr><th>网络 ↓/↑</th><td colspan="3">${h.net_rx_kbps!=null?h.net_rx_kbps:"0"} / ${h.net_tx_kbps!=null?h.net_tx_kbps:"0"} KB/s （累计 ↓${fmt(h.net_rx_bytes)} ↑${fmt(h.net_tx_bytes)}）</td></tr>
    </table>`;
    const gpu = sys.gpu || {};
    if (gpu.available){
      document.getElementById("gpu").innerHTML = `<table><tr><th>GPU</th><th>型号</th><th>利用率</th><th>显存</th><th>温度</th></tr>` +
        (gpu.gpus||[]).map(g=>`<tr><td>${g.index}</td><td>${g.name}</td><td>${g.utilization_percent}%</td>
          <td>${fmt(Math.round(g.memory_used_mb*1048576))} / ${fmt(Math.round(g.memory_total_mb*1048576))}</td><td>${g.temperature_c}°C</td></tr>`).join("") + `</table>`;
    } else {
      document.getElementById("gpu").innerHTML = `<div class="meta">GPU 监控不可用${gpu.error?"："+gpu.error:""}</div>`;
    }
  } else {
    document.getElementById("system").innerHTML = `<div class="meta">系统资源采集失败${sys.error?"："+sys.error:""}</div>`;
    document.getElementById("gpu").innerHTML = "";
  }

  // containers
  document.getElementById("tblContainers").innerHTML =
    `<tr><th>容器</th><th>状态</th><th>CPU%</th><th>内存</th></tr>` +
    ((sys.containers) || []).map(c=>`<tr><td>${c.name}</td><td>${c.status}</td>
      <td>${c.cpu_percent!=null?c.cpu_percent:"–"}</td><td>${c.mem_usage_bytes?fmt(c.mem_usage_bytes):"–"}</td></tr>`).join("");

  // models
  document.getElementById("models").innerHTML = `<table><tr><th>模型</th><th>档位</th><th>当前</th><th>启用</th><th>同步</th></tr>` +
    (d.models||[]).map(m => `<tr><td>${m.model_name}</td><td>${m.tier}</td>
      <td>${m.is_current ? "✅" : "—"}</td><td>${m.is_enabled ? "✅" : "❌"}</td><td>${m.sync_status||"—"}</td></tr>`).join("") + `</table>`;

  // inference realtime concurrency (running + queued requests)
  const infEl = document.getElementById("inference");
  if (inf.available){
    infEl.innerHTML = `<table>
      <tr><th>引擎</th><td>${inf.engine}</td><th>运行中</th><td>${fmt(inf.running_requests)}</td></tr>
      <tr><th>排队中</th><td>${fmt(inf.queued_requests)}</td><th>在途合计</th><td><b>${fmt(inf.total_inflight)}</b></td></tr>
      ${(inf.models||[]).map(m=>`<tr><td colspan="4" style="color:var(--muted)">${m.model}：运行 ${m.running} / 排队 ${m.waiting}</td></tr>`).join("")}
    </table>
    <div class="meta" style="margin-top:8px">注：这是推理引擎“正在运行 + 排队”的实时请求数，<b>与 API Key 数量无关</b>。</div>`;
  } else {
    infEl.innerHTML = `<div class="meta">推理并发监控不可用${inf.error?"："+inf.error:""}</div>`;
  }

  // KV cache monitoring (vLLM)
  const kvEl = document.getElementById("kvcache");
  const kv = inf.kv_cache || {};
  if (kv && (kv.usage_perc != null || (kv.by_model && kv.by_model.length))){
    const pc = kv.prefix_cache || {};
    const cfg = kv.config || {};
    const pct2 = (x) => x != null ? (x*100).toFixed(1)+"%" : "–";
    const num = (x) => x == null ? "—" : x;
    kvEl.innerHTML = `<table>
      <tr><th>KV 占用率</th><td><b>${(kv.usage_perc!=null?kv.usage_perc.toFixed(1)+"%":"–")}</b></td>
          <th>Prefix 命中率</th><td>${pct2(pc.hit_rate)}</td></tr>
      <tr><th>Prefix 查询</th><td>${fmt(pc.queries)}</td>
          <th>Prefix 命中</th><td>${fmt(pc.hits)}</td></tr>
      <tr><th>GPU blocks</th><td>${num(cfg.num_gpu_blocks)}</td>
          <th>KV 容量(令牌)</th><td>${fmt(cfg.kv_cache_size_tokens)}</td></tr>
      <tr><th>Block 大小</th><td>${num(cfg.block_size)}</td>
          <th>显存利用率</th><td>${pct2(cfg.gpu_memory_utilization)}</td></tr>
      <tr><th>前缀缓存</th><td>${num(cfg.enable_prefix_caching)}</td>
          <th>KV 类型</th><td>${num(cfg.cache_dtype)}</td></tr>
    </table>
    ${(kv.by_model||[]).map(m=>`<div class="meta" style="margin-top:6px">${m.model}：KV ${m.usage_perc!=null?m.usage_perc.toFixed(1)+"%":"–"} / Prefix命中 ${m.prefix_hit_rate!=null?(m.prefix_hit_rate*100).toFixed(1)+"%":"–"}</div>`).join("")}`;
  } else {
    kvEl.innerHTML = `<div class="meta">KV Cache 监控不可用（vLLM 未暴露或无数据）</div>`;
  }

  // health
  const h = d.health||{};
  document.getElementById("health").innerHTML =
    `<div><span class="dot ${h.db_ok?"on":"off"}"></span>数据库 ${h.db_ok?"正常":"异常"}</div>
     <div><span class="dot ${h.litellm_reachable?"on":"off"}"></span>LiteLLM ${h.litellm_reachable?"可达":"不可达"}</div>`;
}

function drawBar(canvasId, chartRef, data, setRef){
  const ctx = document.getElementById(canvasId).getContext("2d");
  const labels = data.map(x => x.label), vals = data.map(x => x.v);
  if (chartRef){ chartRef.data.labels=labels; chartRef.data.datasets[0].data=vals; chartRef.update(); return; }
  setRef(new Chart(ctx, { type:"bar",
    data:{ labels, datasets:[{ label:"tokens", data:vals, backgroundColor:"#5b8cff" }] },
    options:{ plugins:{ legend:{display:false} },
      scales:{ x:{ ticks:{color:"#8b97ad"}, grid:{color:"#27324a"} },
               y:{ ticks:{color:"#8b97ad"}, grid:{color:"#27324a"} } } } }));
}

function drawLine(canvasId, chartRef, labels, datasets, setRef){
  const ctx = document.getElementById(canvasId).getContext("2d");
  if (chartRef){ chartRef.data.labels=labels; chartRef.data.datasets.forEach((ds,i)=>ds.data=datasets[i].data); chartRef.update(); return; }
  setRef(new Chart(ctx, { type:"line",
    data:{ labels, datasets: datasets.map((ds,i)=>({ label:ds.label, data:ds.data, borderColor:i?"#f2c14e":"#5b8cff",
      backgroundColor:"transparent", yAxisID:ds.yAxis, tension:.3, pointRadius:0 })) },
    options:{ plugins:{ legend:{labels:{color:"#8b97ad"}} },
      scales:{ x:{ ticks:{color:"#8b97ad", maxTicksLimit:10}, grid:{color:"#27324a"} },
               y:{ ticks:{color:"#8b97ad"}, grid:{color:"#27324a"} },
               y1:{ position:"right", ticks:{color:"#8b97ad"}, grid:{display:false} } } } }));
}

load();
setInterval(load, 30000);
</script>
</body>
</html>"""


@router.get("", response_class=HTMLResponse)
async def dashboard_page(request: Request):
    """内置看板页面（token 保护：支持 Authorization 头或 ?token= 查询参数）"""
    expected = settings.dashboard_token
    token = _extract_token(request) or request.query_params.get("token")
    if not expected or token != expected:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Invalid or missing dashboard token",
        )
    html = _DASHBOARD_HTML.replace("__TOKEN__", expected)
    return HTMLResponse(html)
