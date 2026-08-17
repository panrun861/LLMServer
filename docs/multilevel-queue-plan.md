# 多级队列转发方案（PLLM 并发治理）

> 状态：**已废弃（superseded）**。本方案按「请求难度」分级，已被 `docs/tier-queue-routing-design.md`（按「模型等级」分级）取代，不再实现。保留作为历史参考。
> 关联任务：#41 难度分类器 / #42 队列+并发闸 / #43 接入转发 / #44 SSE 心跳 / #45 非流式超时 / #46 提交部署验证

## 1. 背景与现状

当前 `src/aitube_pllm/api/inference_gateway.py` 是**纯透传扇出**，代码实证（`grep Semaphore|Queue|gather|Throttling` 全部为空）：

- 每个 chat 请求自建 `httpx.AsyncClient(timeout=120.0)`，直接 `POST` 到 LiteLLM
- 并发请求之间**无任何协调**：N 个并发 = N 个协程同时 `await client.post(...)`，全部同时砸向 LiteLLM
- 真正的瓶颈在上游：vLLM 的 `--max-num-seqs`、硅基流动云端的 RPM/并发配额
- 超额时上游返回 429 / 排队 / 变慢，PLLM 这边每个请求还挂着 120s 超时干等，资源与体验双输

**结论**：需要一个进程内的**多级队列 + 并发闸**，把并发请求按难度分流、受控地提交给上游，而非无缓冲扇出。

## 2. 设计决策（已与用户确认 2026-08-12）

| 维度 | 决策 |
|---|---|
| 难度判定 | **PLLM 自动启发式**（不依赖客户端传参） |
| 队列级数 | **3 级：high / med / low** |
| 溢出策略 | **无限排队 + 补 SSE 心跳保活**（非流式例外，见 §6） |

> 注：之前给过的「单级 Semaphore」(A/B/C) 方案是升级版的前身；多级队列是更精细的控制，但工作量更大。

## 3. 难度启发式规则（推荐阈值，集中可配）

依据 `ChatCompletionRequest` 字段推断，无需客户端显式声明：

- **high**：`enable_thinking=true`（或请求带 reasoning 类字段） **或** `max_tokens > 2000` **或** prompt 总字符 `> 4000`
- **med**：`max_tokens > 512` **或** prompt 总字符 `> 800`
- **low**：其余（短对话 / 简单生成）

`prompt 总长度 = Σ(len(message.content))`（多轮累加）。
阈值集中放 config（如 `QUEUE_DIFFICULTY_THRESHOLDS`），避免硬编码，便于调参。

## 4. 队列与并发上限（推荐默认，可配）

| 级别 | asyncio.Queue | Semaphore 并发上限 | 适用场景 |
|---|---|---|---|
| high | 独立队列 | 32 | 快任务（短对话、小 max_tokens） |
| med | 独立队列 | 8 | 中等生成 |
| low | 独立队列 | 2 | 慢任务（长生成 / 开启 thinking） |

- 无限排队：`asyncio.Queue()` 不设 `maxsize`，`put` 不阻塞
- 每级一个常驻 worker 协程：`loop { item = await queue.get(); async with sem: forward(item) }`
- 超额请求在队列里等待，而非压垮上游

## 5. 架构数据流

```
客户端请求
   │
   ▼
[接收层] /v1/chat/completions
   │
   ▼
[难度分类器 classify_difficulty()]  →  high / med / low
   ├──────────────┬──────────────┐
   ▼              ▼              ▼
[high 队列]    [med 队列]     [low 队列]
 Sem=32         Sem=8          Sem=2
   │              │              │
   └──────┬───────┴──────────────┘
          ▼
   [Worker 取任务] ──► 复用 httpx 单例 ──► LiteLLM ──► 上游(vLLM / 硅基流动)
          │
          ├─ 成功 → 返回 / 流式逐 token
          ├─ 忙/超时 → (流式) 心跳保活；(非流式) 放宽 httpx 超时或返回 504
          └─ 结果/异常写回 future → 唤醒等待的请求协程
```

## 6. 关键技术约束（必须处理）

- **流式请求**：可无限排队 + SSE 心跳（`: keep-alive` 每 15s），客户端不断连。✅
- **非流式请求**：HTTP 是**单次响应，无法心跳**。无限排队会受 httpx 120s 硬超时限制 → 超时就断连。
  - 方案：把 httpx 总超时放宽到较大值（如 `600s`，由 `settings.litellm_timeout` 控制）容忍排队；
  - 排队等待超过阈值时返回明确 `504 / 502`，而非静默断连（让客户端知道「系统忙，请重试」）。
- **复用 httpx 客户端**：改每请求新建为**模块级单例** `httpx.AsyncClient()`，复用连接池（尤其硅基流动外部 HTTPS，省 TLS 握手开销）。

## 7. 下一步操作（实现步骤）

### Step 1 — 难度分类器（任务 #41）
- 位置：新建 `src/aitube_pllm/core/difficulty.py`（或并入 `inference_gateway.py`）
- 新增 `classify_difficulty(req: ChatCompletionRequest) -> Literal["high","med","low"]`
- 读取 `req.max_tokens` / `req.messages` / `req.extra` 里的 `enable_thinking`
- 阈值从 config 读取（`config.QUEUE_DIFFICULTY_THRESHOLDS`），不硬编码
- `py_compile` 验证

### Step 2 — 队列 + 并发闸（任务 #42）
- 模块级：`QUEUES = {"high": Queue(), "med": Queue(), "low": Queue()}`
- `SEMAPHORES = {"high": Semaphore(32), "med": Semaphore(8), "low": Semaphore(2)}`（值来自 config）
- 应用启动（`lifespan` 或模块 import）spawn 3 个 worker 协程：
  ```python
  async def _worker(level: str):
      while True:
          item = await QUEUES[level].get()
          async with SEMAPHORES[level]:
              await _forward_item(item)   # 转发 + 写回 future
  ```
- 使用模块级 `httpx.AsyncClient()` 单例

### Step 3 — 接入转发路径（任务 #43）
- `chat_completions` 改为：分类 → `future = loop.create_future()` → `await QUEUES[level].put((req, future, meta))` → `await future`
- 流式：`stream_generator` 从 future 拿上游流后逐 token `yield`；排队期间发心跳（见 Step 4）
- 非流式：从 future 拿响应整包 `JSONResponse`

### Step 4 — SSE 心跳（任务 #44）
- `stream_generator` 在排队等待阶段，定时（每 15s）`yield ": keep-alive\n\n"`
- 用 `asyncio.wait_for` 或独立心跳任务，避免阻塞取流

### Step 5 — 非流式超时配套（任务 #45）
- httpx 总超时从 `120.0` 改为 `settings.litellm_timeout`（默认 `600.0`）
- 排队等待超阈值（如 120s 未出队）→ 返回 `504`，body 提示「系统繁忙，请稍后重试」
- `py_compile` 验证

### Step 6 — 提交 / 部署 / 验证（任务 #46）
- `git add` 改动文件 → `git commit`（feat: 多级队列并发治理）→ `git push origin main`
- L20s：`git pull` + `docker compose up -d --build app`
- 验证：容器健康；高/低难度请求分别走对应队列（日志/metrics）；流式心跳不断连；端到端经 `qwen3-8b` 通

## 8. 验证方法

- 临时插测试 token，发 high/med/low 难度请求，确认路由级别与并发受控
- 压测：并发 50 请求，观察 low 队列被限到 2 并发、high 基本不受影响
- 流式长等待：客户端观察心跳包（`": keep-alive"`）不断连

## 9. 风险与回滚

- 单进程 asyncio，worker 协程常驻；队列逻辑 bug 可能导致请求挂起
- 回滚：`git revert <commit>` + `docker compose up -d --build app`
- 建议先在 staging 用压测验证后再上生产

## 10. 待确认项（实现前）

- [ ] 各级 Semaphore 默认值（high=32/med=8/low=2）是否符合服务器规格（L40S / 硅基流动配额）
- [ ] 非流式是否也接受「无限排队 + 放宽 httpx 超时」，还是设一个排队上限（如 120s）→ 504
- [ ] 难度阈值是否需要根据实际流量分布微调
