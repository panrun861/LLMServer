# PLLM 监控大盘对接文档

对外暴露一个**只读聚合接口**，供任意外部前端拉取监控数据。零额外基础设施，仅复用现有 Postgres。

## 1. 接口：聚合快照

```
GET /admin/dashboard/snapshot
```

### 认证
任选其一（专用只读 token，与写权限的 `x-local-admin` / Ed25519 签名无关）：
- `Authorization: Bearer <DASHBOARD_TOKEN>`
- `X-Dashboard-Token: <DASHBOARD_TOKEN>`

当前部署 token：
```
pllm_dash_sTq2jdQERFiZF9PWZgVIMxCenXW6jiCI
```

### 查询参数
| 参数 | 类型 | 说明 |
|---|---|---|
| `window` | int (1–720) | 统计窗口，单位小时。默认 24。≤72h 时间序列按「小时」聚合，否则按「天」。 |

### 返回字段（全量明细，前端按需筛选）
```jsonc
{
  "generated_at": "ISO8601",
  "window": "24h",
  "bucket_unit": "hour",            // timeseries 的桶粒度
  "totals": {                       // 窗口内汇总
    "requests", "success", "failed", "error_rate",
    "total_tokens", "prompt_tokens", "completion_tokens"
  },
  "latency": { "avg_ms", "p95_ms", "p99_ms" },
  "timeseries": [                   // 时间序列，可画趋势/区间
    { "bucket", "requests", "tokens", "errors", "avg_latency_ms" }
  ],
  "by_model": [                     // 按模型
    { "model", "requests", "tokens", "avg_latency_ms", "errors" }
  ],
  "by_subject": [                   // 按调用方(subject_id)
    { "subject", "requests", "tokens", "avg_latency_ms", "errors", "error_rate" }
  ],
  "recent_usage": [                 // 最近用量样本(最多100)，可任意筛选
    { "created_at", "model", "subject", "tier", "total_tokens", "latency_ms", "status_code" }
  ],
  "budgets": [                      // 配额预算(本月)
    { "subject", "name", "token_budget", "token_budget_period",
      "used", "remaining", "pct", "status" }   // status: ok|warning|exhausted|unlimited
  ],
  "security": {
    "signature_ok", "signature_fail", "replays",
    "recent": [ { "occurred_at", "endpoint", "decision", "reason_code", "source_address" } ]
  },
  "models": [ { "model_name", "tier", "is_current", "is_enabled", "sync_status" } ],
  "inference": {                   // 推理引擎实时并发 —— 即「请求数量」的正确含义
    "available", "engine",         // 当前 engine=vllm
    "running_requests",            // 正在运行的请求数（≠ API Key 数量）
    "queued_requests",             // 排队等待的请求数
    "total_inflight",              // running + queued
    "waiting_by_reason": { "<model>": { "capacity": n, "deferred": n } },
    "models": [ { "model", "running", "waiting", "total_inflight" } ],
    "kv_cache": {                  // ← vLLM KV Cache 监控
      "usage_perc",                // KV cache 占用率 %（全局/首个模型）
      "by_model": [ { "model", "usage_perc", "prefix_queries", "prefix_hits", "prefix_hit_rate" } ],
      "prefix_cache": { "queries", "hits", "hit_rate" },   // 前缀缓存命中率
      "config": {                  // 来自 vllm:cache_config_info
        "num_gpu_blocks", "kv_cache_size_tokens", "gpu_memory_utilization",
        "block_size", "cache_dtype", "enable_prefix_caching"
      }
    }
  },
  "keys": { "active", "total" },   // API Key / Token 数量（与请求数量无关，单独统计）
  "health": { "db_ok", "litellm_reachable" }
}
```

> ⚠️ **「请求数量」语义澄清**：本接口的「请求数量」指推理引擎当前
> **正在运行 + 排队中** 的请求数（`inference.running_requests` / `inference.queued_requests`），
> 来自 vLLM 的 `vllm:num_requests_running` / `vllm:num_requests_waiting` 指标。
> 这与 `keys.active`/`keys.total`（API Key 数量）是两个完全不同的概念，请勿混用。
> `totals.requests` 则是「统计窗口内已完成的请求数」，亦不同于实时在途并发。
>
> **KV Cache 监控**：`inference.kv_cache` 来自 vLLM 的 `vllm:kv_cache_usage_perc`（KV 占用率）、
> `vllm:prefix_cache_*`（`prefix cache` 命中率）、`vllm:cache_config_info`（GPU blocks / 容量令牌数 /
> 显存利用率 / block 大小 / dtype / 前缀缓存开关）。KV 占用率接近 100% 说明 GPU 显存中缓存的
> K/V 块接近上限，新请求将被迫等待或抢占；前缀命中率高说明大量重复 prompt 前缀被复用，省 token 与延迟。

## 2. 内置看板页面（浏览器直接看）

```
GET /admin/dashboard?token=<DASHBOARD_TOKEN>
```

自包含 HTML（Chart.js CDN），含 KPI 卡片、时间序列折线、按模型/调用方分布、配额/安全/模型表，
支持 1h/6h/24h/7d/30d 窗口切换，每 30s 自动刷新。

## 3. 外部前端对接示例

CORS 已开放（`allow_origins=["*"]`），浏览器可直接 `fetch`：

```js
const TOKEN = "pllm_dash_sTq2jdQERFiZF9PWZgVIMxCenXW6jiCI";
const res = await fetch(
  "https://<host>:8080/admin/dashboard/snapshot?window=24",
  { headers: { Authorization: "Bearer " + TOKEN } }
);
const data = await res.json();
// 前端按需要筛选 data.by_model / data.timeseries / data.recent_usage ...
```

## 4. 部署说明
- 代码：`src/aitube_pllm/api/dashboard.py`（路由 `/admin/dashboard`）。
- 中间件已对 `/admin/dashboard*` 豁免 Ed25519 签名，改由本 token 校验。
- token 来自环境变量 `PLLM_DASHBOARD_TOKEN`（服务端 `.env`）。
- 容器以 bridge 网络运行，数据库连接需用 `host.docker.internal` 而非 `127.0.0.1`：
  `PLLM_DATABASE_URL` 与 `PLLM_LITELLM_API_BASE` 均指向 `host.docker.internal`。
