# PLLM 模型分级队列路由设计（高/中/低三级）

> 状态：设计稿 v1，待确认后实现
> 关联：旧方案 `docs/multilevel-queue-plan.md`（按「请求难度」分级）与本方案（按「模型等级」分级）是两个不同维度，本方案取代前者作为主路径。

## 1. 需求一句话

客户端请求一个模型名，系统按**模型自身的等级（tier：high / medium / low）**分流到三个独立等待队列 + 线程池；某等级不可用或排队过久时**自动降级**到下一等级；最低等级也没有就落到**默认模型**兜底。同时提供**队列与可用模型的可视化查看**。

## 2. 核心概念

| 概念 | 定义 |
|---|---|
| 等级 tier | 复用 `models.tier`，约定三档：`high` / `medium` / `low` |
| 默认模型 | `models.is_current = true` 的那个（现有机制，`get_current_model_name`） |
| 等待序列 | 每个等级一个 `asyncio.Queue`（不设上限，无限排队）；**同等级的模型共享一个队列** |
| 并发闸 | **每个模型一个 `asyncio.Semaphore`**，并发数 `concurrency` 在注册模型时单独设置（模型之间可不同） |
| 降级 | 从当前等级转移到下一等级继续排队/转发 |

## 3. 路由决策流程（核心）

```
请求 model = M
  │
  ├─ M 不认识（models 表无 / is_enabled=false）
  │      → 用「默认模型」，按默认模型自己的等级进队列
  │
  └─ M 认识
        取 M.tier = L（high / medium / low）
        │
        ├─ L 等级有可用模型（该等级存在 enabled 模型）
        │      → 进入 L 等级队列，由 L 线程池转发
        │
        └─ L 等级无可用模型（异常：M 刚被禁用等）
               → 降级到下一等级（high→medium→low）
               → low 也没有 → 默认模型
```

### 降级触发（两条路径）

1. **等级无模型**：当前等级没有任何 `is_enabled=true` 的模型 → 立即降级下一等级。
2. **排队超时**：请求在队列里等待超过 **10 分钟**（可配）→ 自动转移到下一等级的队列尾部重新排队。

### 降级后用什么模型

- 降到某等级后，用该等级的「**默认模型**」：优先该等级内 `is_current=true` 的模型，否则取该等级最近更新（`updated_at DESC`）的 enabled 模型。
- 兜底：`low` 也没有 → 用全局默认模型（`is_current`）。

## 4. 队列与并发闸（两层正交）

**队列按等级分（3 个，同等级共享）：**

| 等级 | 队列 | 定位 |
|---|---|---|
| high | `QUEUES["high"]` | 主路 |
| medium | `QUEUES["medium"]` | 降级缓冲 |
| low | `QUEUES["low"]` | 兜底 |

**并发按模型分（每模型一个，数值注册时设置）：**

```python
QUEUES = {"high": Queue(), "medium": Queue(), "low": Queue()}   # 等级维度
SEMS = {}  # 模型维度：{"模型名": Semaphore(该模型 concurrency)}

async def worker(level):
    while True:
        req = await QUEUES[level].get()   # 从等级队列取（FIFO）
        sem = SEMS[req.model]             # 按请求的模型取它自己的并发闸
        async with sem:
            await forward(req)            # 转发 LiteLLM
```

- 每个模型注册时填 `concurrency`（并发数），如模型 A=32、模型 B=16、模型 C=2。
- 同等级的模型共享一个队列（先到先服务），但转发时各用各的并发闸，互不干扰。
- 每级一个常驻 worker 协程。

## 5. 队列状态查询（新增接口）

`GET /admin/queue/status`（`x-local-admin` 鉴权，与模型管理同款弱鉴权）：

```json
{
  "default_model": "qwen3",
  "tiers": {
    "high":   { "queued": 0, "active": 0, "limit": 32, "models": ["..."] },
    "medium": { "queued": 0, "active": 0, "limit": 8,  "models": ["qwen3", "qwen3.6-local"] },
    "low":    { "queued": 0, "active": 0, "limit": 2,  "models": ["..."] }
  }
}
```

- `queued`：排队等待数；`active`：正在转发数；`limit`：并发上限；`models`：该等级可用（enabled）模型名列表。

## 6. 配置项（config.py 新增）

| 配置 | 默认 | 说明 |
|---|---|---|
| `default_model_concurrency` | `8` | 模型未填 `concurrency` 时的默认并发数 |
| `queue_downgrade_timeout_seconds` | `600` | 排队超时降级阈值（10 分钟） |
| `queue_enabled` | `true` | 总开关（可一键回退到当前直连模式） |

> 注：并发数不再用全局 `queue_tier_concurrency`，改为**每个模型的 `concurrency` 字段**（存 `models` 表，注册/编辑时设置）。

## 7. 前端展示

在「状态总览」Tab 新增「队列与可用模型」面板：
- 三张卡片分别展示 high/medium/low 的「排队数 / 占用并发 / 上限 / 该等级模型列表」。
- 顶部显示当前「默认模型」。
- 轮询 `GET /admin/queue/status`（复用 dashboard token 或 x-local-admin）。

## 8. 数据流

```
客户端 /v1/chat/completions (model=M)
        │
        ▼
  路由决策（查 M 的 tier）
        │
   ┌────┼────┬────────┐
   ▼    ▼    ▼        ▼
[high] [med] [low]   默认模型(不认识时)
 Sem32 Sem8  Sem2
   │    │    │
   └────┴────┴─► worker ──► LiteLLM ──► 上游
                     │
                     ├─ 排队超 10min ──► 下一等级队列
                     └─ 结果/异常写回 future ──► 唤醒请求
```

## 9. 关键技术约束

- **流式请求**：可无限排队，排队期间每 15s 发 SSE 心跳 `: keep-alive`，防客户端断连。
- **非流式请求**：HTTP 单次响应无法心跳，需把 httpx 总超时放宽（如 600s），排队超阈值时返回 504「系统繁忙请重试」。
- **复用 httpx 单例**：改每请求新建为模块级单例，复用连接池。
- **单进程内存队列**：asyncio.Queue 只在单个进程内有效。当前 app 单副本没问题；若将来多副本部署，队列状态需外置（Redis），本方案先按单副本设计。

## 10. 与旧方案（按难度分级）的关系

- 旧 `multilevel-queue-plan.md`：按请求难度（max_tokens/prompt 长度）分 high/med/low。
- 本方案：按模型 tier 分 high/medium/low。
- 两者**正交**，可后续叠加（等级内再按难度细分），但第一版只做「按模型等级」，避免过度设计。

## 11. 实现步骤（确认后执行）

1. `config.py` 加 3 个配置项。
2. 新建 `src/aitube_pllm/core/queue.py`：三级 `Queue` + `Semaphore` + worker 协程 + 队列状态快照函数。
3. `inference_gateway.py`：改写 `chat_completions` 路由——不认识→默认模型；认识→按 tier 进队列；接入超时降级。
4. 新增 `GET /admin/queue/status` 接口。
5. 前端「状态总览」加队列面板。
6. 提交 / 部署 / 端到端验证。

## 12. 待确认项（请拍板）

1. **等级映射**：现有模型 tier 有 `medium/premium/test`，是否统一为 `high/medium/low`？`premium`→high、`test`→low 是否 OK？
2. **超时降级后模型选择**：降级用下一等级的「current 或最近更新」模型，是否符合预期？
3. **线程池默认值**：high=32 / medium=8 / low=2 是否合适（L40S + 硅基流动配额）？
4. **是否保留旧「按难度」方案**：建议先不叠加，只做按等级，确认一下。
