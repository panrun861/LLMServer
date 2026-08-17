# PLLM 全局模型强制路由 — 方案书

> 目标：新增“全局强制模型”参数。设置后，**不论 OpenAI 接口客户端传入什么 `model` 名称，统一路由到唯一指定的当前可用模型**。
> 状态：**已落地（2026-08-12）**。适用场景：单模型部署 / 客户机器。

> **实现说明（已变更）**：最终未采用 `PLLM_FORCE_MODEL` 环境变量方案，而是直接复用 `models` 表的 `is_current` 标记作为全局当前模型——
> 网关在每次请求时读取 `WHERE is_current=TRUE AND is_enabled=TRUE` 的 `model_name` 作为强制目标（见 `ModelRepo.get_current_model_name`）。
> 切换全局模型只需改 `models.is_current`（或调 `/admin/models/{name}/active-tier`），**无需改 `.env`、无需重建容器**。原 `config.py` 的 `force_model` 字段已删除。

---

## 1. 现有机制梳理（已取证）

路由入口 `src/aitube_pllm/api/inference_gateway.py`：

| 行号 | 代码 | 含义 |
|---|---|---|
| L251 | `ModelRepo.get_current(conn, body.model)` | 按 `model_name=body.model AND is_current=TRUE AND is_enabled=TRUE` 查 `models` 表 |
| L289 | `request_body = body.model_dump(exclude_none=True)` | 转发给 LiteLLM 的 `request_body["model"]` **直接等于客户端原值** |
| L362/373/399/422/433/449/461 | `model_name=body.model` | 计费 `usage_logs` 与审计 `event_logs` 均记客户端原值 |

**关键发现**：DB 字段 `model_artifact` 在 `inference_gateway.py` 中**无任何引用**，当前并未参与转发。实际路由依赖「客户端 `model` 名 == LiteLLM 中已配置的模型别名」。

**现有“模型切换”接口**（用户所指的全局切换，实为 per-model 切换）：
- `PUT /admin/models/{model_name}/active-tier`（`model_management.py:222` → `ModelRepo.activate_tier` `repos.py:320`）
- 作用：**针对某个 `model_name` 切换其 active tier（`is_current`）**，仍要求客户端传对 `model_name`。
- 它解决“同模型不同档位切换”，**不是**“忽略客户端模型名”。新增机制应叠在它之上、与之正交。

配置入口：`src/aitube_pllm/config.py` `Settings`，`env_prefix="PLLM_"`。

---

## 2. 设计决策（已与用户确认）

- **D1 强制模型必须是“当前可用模型”**：即已在 `models` 表中登记、且 `is_current=TRUE`、`is_enabled=TRUE`。未满足则请求返回 404（沿用现有白名单语义，不绕过）。
- **D2 `/v1/models` 端点已移除**（模型列表改由管理端点 `/admin/models` 提供）；仅 `chat/completions` 请求被强制到单一模型。
- **D3 与 `activate_tier` 关系**：新增的全局覆盖是**叠在 `is_current` 之上的一层 client-model-agnostic 覆盖**，二者正交。建议强制模型值取自“当前可用模型”之一。

---

## 3. 实现路线

### 路线 A（推荐·第一版）：环境变量静态钉选
适合客户机器 / 单模型部署，设置一次、重启生效。

- `config.py` 增加字段：
  ```python
  # 全局强制模型（单模型部署）：设置后忽略客户端 model，统一路由到此模型。
  # 值须为 LiteLLM 中已配置的模型别名，且须在 PLLM models 表中 is_current & is_enabled。
  force_model: str | None = Field(default=None, description="全局强制模型")
  ```
  对应环境变量 `PLLM_FORCE_MODEL`（`env_prefix="PLLM_"`）。
- 网关 `chat_completions` 改动（详见第 5 节）。
- 部署：`.env` + `docker-compose.yml` 的 app 服务加 `PLLM_FORCE_MODEL=<LiteLLM别名>` → 重启 app 容器。

### 路线 B（增强·可选）：运行时管理员接口动态切换
适合需要**不重启即可切换**全局模型的场景，复用现有 admin 鉴权与审计模式（与用户提到的“rag server 端全局模型切换接口”一致）。

- 新增 `system_config(key TEXT PK, value JSONB)` 表；key = `global_force_model`。
- 新增 admin 接口：
  - `PUT /admin/global-model`：设置，值须为当前可用模型（复用 `_require_local_admin` + `AuditRepo.record_event`）。
  - `DELETE /admin/global-model`：清除。
- 网关解析优先级：`DB.global_force_model` → `env PLLM_FORCE_MODEL` → `body.model`。
- PLLM 侧提供对等的管理员切换接口，可与外部 rag server 的全局模型开关在部署时保持同步。

---

## 4. 行为对比

| 场景 | 当前行为 | 路线 A 生效后 |
|---|---|---|
| 客户端传 `model:"gpt-4"`，`PLLM_FORCE_MODEL="qwen3.6-27b"` | 查 `gpt-4` 白名单，命中则转 LiteLLM 的 `gpt-4` | 忽略 `gpt-4`，统一转 LiteLLM 的 `qwen3.6-27b`（须为当前可用模型） |
| 客户端传不存在的模型名 | 404 | 仍走 `qwen3.6-27b`（不再 404） |
| `/admin/models` | 列出已启用模型 | 管理端点（Ed25519/x-local-admin），替代原 `/v1/models` |
| 计费/审计记录 | `body.model` | `effective_model`（真实服务的模型） |

---

## 5. 改动点清单（以路线 A 为例）

| 文件 | 位置 | 改动 |
|---|---|---|
| `src/aitube_pllm/config.py` | L53 后 | 新增 `force_model: str \| None` 字段 |
| `src/aitube_pllm/api/inference_gateway.py` | L247 后 | 插入 `effective_model = settings.force_model or body.model` |
| 同上 | L251 / L255 / L262 | `get_current` 与 404 信息改用 `effective_model` |
| 同上 | L289-293 | merge 完成后 `request_body["model"] = effective_model`（最后一步强制覆盖，避免被 request_params 覆盖） |
| 同上 | L362 / 373 / 399 / 422 / 433 / 449 / 461 | `model_name=body.model` → `model_name=effective_model` |

`body.model` 的 pydantic 定义（L123，必填 `str`）保持不变；强制模式下其取值被忽略，仅校验“非空字符串”。

---

## 6. 部署与验证

1. 在服务器确认 LiteLLM 当前可用模型别名（见第 8 节待确认项）。
2. 设置 `PLLM_FORCE_MODEL=<当前可用模型的 model_name>`（须等于 LiteLLM 已配置别名）。
3. 重启 app 容器：`docker compose up -d --build`（仅 app 容器，不影响 vLLM / LiteLLM / Postgres）。
4. 验证：
   - 用错模型名调用 `POST /v1/chat/completions` `{"model":"whatever",...}` → 正常返回，实际走 `PLLM_FORCE_MODEL` 指定模型。
   - `usage_logs.model` 与 `event_logs.target_id` 应为 `effective_model`（真实服务模型）。
   - 模型列表改由 `/admin/models`（管理端点）提供；原 `/v1/models` 已移除。

---

## 7. 风险与注意事项

- **R1 值必须为 LiteLLM 已知别名 + PLLM 已登记启用模型**，否则全量请求 404。
- **R2 容量集中**：强制后所有请求打到同一模型，需关注该模型的速率 / 预算 / 显存容量（多 token 共享一个模型额度）。
- **R3 计费归属**：计费 / 审计记入 `effective_model`，与客户端感知的 `model` 不同，需在文档 / 对账中说明。
- **R4 技术债**：`model_artifact` 当前未参与转发，强制模型值以 `model_name`（= LiteLLM 别名）为准，而非 `model_artifact`。建议后续单独清理该字段用途。

---

## 8. 待确认项

- [ ] 采用路线 A，还是 A + B？
- [ ] `PLLM_FORCE_MODEL` 具体取值（需先在服务器确认 LiteLLM 当前可用模型列表）。
- [ ] 是否需要我 SSH 上服务器实测 LiteLLM 可用模型别名后再定稿取值？
