# 项目长期记忆：PLLM 监控大盘 + Prometheus 采集栈

## 监控大盘架构（2026-07-30 定稿）
- 代码：`src/aitube_pllm/api/dashboard.py`
  - 接口：`/admin/dashboard/snapshot`(REST, 需 `X-Dashboard-Token` 或 `Authorization` 头；**不接受 ?token=**)、`/admin/dashboard/stream`(SSE, 接受 ?token=)、`/admin/dashboard`(HTML, 接受 ?token=)
  - token：`PLLM_DASHBOARD_TOKEN`（服务器 .env），当前值见部署记录
  - 看板地址：`http://35.76.187.215:8080/admin/dashboard?token=<TOKEN>`
- 采集策略（双层）：
  - **主路径（Prometheus）**：主机→node_exporter、GPU→nvidia-gpu-exporter(mindprince, NVML)、推理→vLLM 原生指标（经 Prometheus 查询）。Prometheus 地址 `_PROM_URL`，默认 `http://host.docker.internal:9090`（app 容器有 host-gateway，可直达宿主机 9090）
  - **兜底**：Prometheus 不可达时自动回退手搓采集（/proc + nvidia-smi + docker stats 并发 + vLLM /metrics 直连），看板不中断（已宕机演练验证）
  - 容器指标：因**宿主 docker 存储驱动为 overlayfs**，cadvisor 无法解析容器名（cadvisor 只支持 overlay2 层元数据），故容器 CPU/内存/状态仍由并发 docker-stats 采集（ThreadPool 并发，~2s）

## 监控栈部署（独立 compose，不干扰 app）
- 文件：`monitoring/docker-compose.monitoring.yml` + `monitoring/prometheus.yml`（含 `__PGPASS__` 占位符，部署时由服务器 .env 的 litellm 密码替换）
- 组件：prometheus(9090) + node-exporter(9100) + nvidia-gpu-exporter(9445) + postgres-exporter(9187)
- **未采用**：cadvisor（overlayfs 不兼容）、dcgm-exporter（nvcr.io 需登录）、`nvidia/dcgm-exporter`(docker.io 已下架)
- 部署命令：`docker compose -f monitoring/docker-compose.monitoring.yml up -d --remove-orphans`
- 配置热更新：`curl -XPOST http://127.0.0.1:9090/-/reload`（已启用 --web.enable-lifecycle）
- 网络：监控容器在 `monitoring` 网络互访；prometheus/postgres-exporter 用 `extra_hosts: host.docker.internal:host-gateway` 访问宿主机的 vLLM(8000)/Postgres(5432)

## 关键环境事实（L20s 服务器 35.76.187.215）
- GPU：NVIDIA L40S（驱动 595.84）；nvidia-gpu-exporter 指标前缀 `nvidia_gpu_*`（duty_cycle/memory_*/temperature_celsius/power_usage_milliwatts）
- vLLM：v0.25.1，/metrics 含 121 项（running/waiting/kv_cache_usage_perc/prefix_cache_*/各类延迟），推理并发 ground truth；**但 /metrics 不含 max_model_len 指标**。context_length 真实值由 **vLLM 原生 /v1/models**（每个模型对象带 `max_model_len`，实测 `qwen3.6-27b=310000`）提供，PLLM `model_sync.py` 的 `fetch_vllm_models()` 直连该端点抓取，并经 `model_artifact`（去 provider 前缀）桥接 `qwen3.6-local`(PLLM 别名)↔`qwen3.6-27b`(vLLM served) 命名空间；配置项 `PLLM_MODEL_SYNC_VLLM_MODELS_URL`（默认 `http://host.docker.internal:8000/v1/models`）。2026-08-12 已接通，手动 sync 后 context_length 实测由 8192→310000。
- 网络栈：aitube-pllm_default(app+一个 restarting 的 postgres 冗余容器) 与 stack_default(vllm/litellm/postgres 真实推理栈) 分离；app 经 host.docker.internal 访问 vLLM:8000 / LiteLLM:4000 / Postgres:5432
- litellm /metrics 返回 401（需鉴权），不纳入 Prometheus 抓取，看板经 API(master key) 探活

## 部署代码现状（2026-08-12 部署完成）
- 远程 git 已同步到 origin/main（`f72d550`，含 PLLM_FORCE_MODEL 实现）；此前落后 6 commit 的 `ac4e68f` 已通过 ff-only pull 更新。
- 远程原脏工作区（手工改动 + root 误建 `monitoring/monitoring` 嵌套）已 `git stash` 存档（stash@{0}/stash@{1}，未丢）+ `sudo rm` 清理残留；容器重建后代码来源可追溯。
- 容器 aitube-pllm-app-1 重建于 **2026-08-12 01:46 UTC**（`docker compose up -d --build app`），运行 f72d550 + `PLLM_FORCE_MODEL=qwen3.6-local`。
- **PLLM_FORCE_MODEL 全局强制模型机制已实现并部署**（config.py `force_model` 字段 + inference_gateway.py `effective_model` 覆盖查询/404/计费/审计/`request_body["model"]`）。取值 `qwen3.6-local`（PLLM `models` 表唯一 `is_current&is_enabled`）。效果：客户端乱传不存在 `model` 名**不再 404**，统一路由 `qwen3.6-local`（litellm 后端已验证 200 可用）。
- LiteLLM 可用别名：`qwen3.6-local` / `qwen3.6-27b` / `hunyuanocr-1.5`；若要强制到 `qwen3.6-27b` 须先在 PLLM `models` 表登记其为 `is_current&is_enabled`（否则全量 404）。
- token 表实为 `pllm_tokens`（仅存 `pllm_token_hash`，明文不入库），故无法从 DB 取明文 token 自动化端到端测试。

## ⚠️ 数据库技术债 / 部署坑（2026-08-12 验证）
- **运行库 schema 漂移**：`src/aitube_pllm/db/schema.sql` 里 `models.runtime_params`/`request_params` 声明 **JSONB**，但 `schema.sql` 仅在**全新库 init**（`docker-entrypoint-initdb.d`）执行；**现有运行库这两列实际是 TEXT**（历史建表更早）。任何向这两列写 Python dict 的操作都会触发 `asyncpg DataError: expected str, got dict`（$7/$8 参数类型被推断为 text）。
- **修复范式（已落地 model_management.py）**：① 写库前 `json.dumps(...)` 序列化成字符串（兼容 text/jsonb 两种列）；② 读回用 `_extract_upstream_type()` 兼容 dict / JSON 字符串 / None；③ 若确需真 JSONB，手动 `ALTER TABLE models ALTER COLUMN <col> TYPE JSONB USING <col>::jsonb;`（现有库两列已转 JSONB，实测全 NULL 零风险）；④ **改列类型后必须 `docker compose restart app`** 刷新 asyncpg 连接池的类型缓存，否则仍报旧类型。
- 新增模型/字段优先复用现有 JSON 列（如 `runtime_params`）或加 `runtime_params` 子键，**避免新增 DB 列**（否则需手写 ALTER 迁移 + 重启）。
- **`models` 表已新增 `api_key_encrypted TEXT` 列**（2026-08-12 手动 ALTER），用于 AES 加密存储外部 API key。服务器 .env 已配置 `PLLM_ENCRYPTION_KEY`（**此密钥需备份，丢失则所有 api_key 不可恢复**）。
