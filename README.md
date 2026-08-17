# AITube-PLLM

OpenAI-compatible private LLM gateway with PLLM token authentication, model tiers,
usage accounting, audit logs, multimodal messages, and function calling passthrough.

## Architecture

```
client ──► /v1/chat/completions (Bearer PLLM-Token)
                │
                ▼
        inference_gateway  ──(PLLM_QUEUE_ENABLED)──►  QueueRouter (high/medium/low tiers)
                │                                         │  per-model Semaphore(N)
                │                                         ▼
                └──────────────────────────────►  LiteLLM proxy ──► vLLM (local L40S)
admin APIs (/admin/*)      : Ed25519 signature (external) / x-local-admin (localhost CLI)
monitoring (/admin/dashboard/*) : Dashboard Token
```

- **Gateway** — `src/aitube_pllm/api/inference_gateway.py`: token auth, RPM / token-budget
  limits, tier routing, usage + audit recording.
- **Queue** (optional, `PLLM_QUEUE_ENABLED=true`) — `src/aitube_pllm/core/queue.py`: three
  tier queues plus a per-model concurrency gate. Design doc: `docs/tier-queue-routing-design.md`.
- **Admin APIs** — model / token management behind Ed25519 signature (external caller =
  AITube-RAG) or `x-local-admin` (localhost CLI).
- **Monitoring** — Prometheus + node-exporter + nvidia-gpu-exporter + postgres-exporter
  (separate compose), plus an in-app dashboard at `/admin/dashboard?token=<TOKEN>`.

## Model tiers & concurrency

Each model carries a `tier` (high / medium / low). Requests are routed to that tier's queue;
each tier has its own `asyncio.Queue` and every model has its own `asyncio.Semaphore`
concurrency gate (`models.runtime_params.concurrency`). Waiting past half of a tier's
`wait_limit` triggers a soft-degrade to the next tier; the lowest tier hard-times-out at
`wait_limit` → 504. See `docs/tier-queue-routing-design.md`.

Enable with `PLLM_QUEUE_ENABLED=true`. After any model add/remove/change, call
`POST /admin/queue/reload` so the router rebuilds its gates.

## Authentication (layered)

| Surface | Auth |
|---|---|
| Admin APIs — external (`/admin/models`, `/admin/tokens`, ...) | Ed25519 signature (timestamp window + nonce replay protection) |
| Inference `/v1/*` | Bearer PLLM-Token (SHA-256 hashed at rest) + RPM limit + token budget |
| Monitoring `/admin/dashboard/*` | Dashboard Token (`X-Dashboard-Token` / `?token=` / `Authorization: Bearer`) |
| Localhost admin CLI | `x-local-admin: true` (weak, localhost-only) |

## Local Docker setup

1. Copy `.env.example` to `.env`.
2. Put local database and LiteLLM credentials in `.env`.
3. Start the service:

```bash
docker compose up -d --build
```

The API listens on `http://localhost:8080` by default. Swagger documentation is
available at `http://localhost:8080/docs`.

`.env`, local compose overrides, private keys, logs, generated API test files, and
virtual environments are intentionally excluded from Git.

## Deploy to the L40S host

The app database is **`aitube_pllm`** (not `litellm`) — set
`PLLM_DATABASE_URL=.../aitube_pllm`. After pushing:

```bash
ssh ubuntu@<host>
cd /path/to/aitube-pllm
git pull
docker compose up -d --build app
# if the queue is enabled:
curl -X POST -H "x-dashboard-token: $DASH_TOKEN" http://localhost:8080/admin/queue/reload
```

## Monitoring stack

Independent of the app: `monitoring/docker-compose.monitoring.yml`
(Prometheus + exporters). Dashboard:
`http://<host>:8080/admin/dashboard?token=<PLLM_DASHBOARD_TOKEN>`.
