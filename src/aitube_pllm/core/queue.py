"""PLLM 三级挡位队列路由核心。

设计（详见 docs/tier-queue-routing-design.md）：

- 每个挡位(high/medium/low)一个 ``asyncio.Queue``（同挡位模型共享，FIFO 无限排队）。
- 每个模型一个 ``asyncio.Semaphore``（并发闸），并发数可不同，存于
  ``models.runtime_params.concurrency``；未设置回退 ``default_model_concurrency``。
- 每挡位一个常驻 worker 协程，从本挡位队列取请求 → 取「最空闲兄弟模型」的并发闸 → 转发。
- **半限软降级**：请求等待时间 ``waited`` 在非最低挡位且
  ``waited > wait_limit × degrade_threshold_ratio``（默认½）仍拿不到槽位 →
  转移到下一挡位队列尾部（保留 enqueued_at，总等待连续累计）。最低挡位仍超
  ``wait_limit``（硬超时）→ 504。

转发动作本身**不在此模块实现**，而是通过 ``queue_router.forward_fn`` 回调注入
（由 ``inference_gateway`` 注册），以避免与网关模块形成循环依赖。
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Any, Awaitable, Callable, Optional

from ..config import settings
from ..db import ModelRepo, db

logger = logging.getLogger(__name__)

TIERS = ("high", "medium", "low")
_NEXT_TIER = {"high": "medium", "medium": "low", "low": None}

# 流式转发时，worker 向 item.stream_q 推送的结束哨兵
STREAM_SENTINEL = object()


class QueueItem:
    """队列工作项：请求在队列 / worker 之间传递的载体。

    非流式结果通过 ``future`` 返回；流式结果逐行推入 ``stream_q``，结束塞入
    :data:`STREAM_SENTINEL`。
    """

    __slots__ = (
        "request_id", "request_body", "headers", "litellm_url",
        "token_record", "model_config", "target_model", "current_tier",
        "enqueued_at", "is_stream", "correlation_id", "start_time",
        "future", "stream_q", "stream_done",
    )

    def __init__(
        self,
        *,
        request_id: str,
        request_body: dict,
        headers: dict,
        litellm_url: str,
        token_record: dict,
        model_config: dict,
        target_model: str,
        current_tier: str,
        is_stream: bool,
        correlation_id: Optional[str],
        start_time: float,
    ) -> None:
        self.request_id = request_id
        self.request_body = request_body
        self.headers = headers
        self.litellm_url = litellm_url
        self.token_record = token_record
        self.model_config = model_config
        self.target_model = target_model
        self.current_tier = current_tier
        self.enqueued_at = time.monotonic()
        self.is_stream = is_stream
        self.correlation_id = correlation_id
        self.start_time = start_time
        self.future: asyncio.Future = asyncio.get_event_loop().create_future()
        self.stream_q: "asyncio.Queue[Any]" = asyncio.Queue()
        self.stream_done = asyncio.Event()


class QueueRouter:
    """三级挡位队列路由器（模块级单例 :data:`queue_router`）。"""

    def __init__(self) -> None:
        self.queues: dict[str, "asyncio.Queue[QueueItem]"] = {
            t: asyncio.Queue() for t in TIERS
        }
        self.sems: dict[str, asyncio.Semaphore] = {}
        self.caps: dict[str, int] = {}  # model_name -> 初始并发数（用于状态展示）
        self.free: dict[str, int] = {}  # model_name -> 当前剩余并发槽位（自维护，避免依赖 Semaphore.value）
        self.tier_models: dict[str, list[str]] = {t: [] for t in TIERS}
        self.active: dict[str, int] = {t: 0 for t in TIERS}
        self.default_model: Optional[str] = None
        self.forward_fn: Optional[Callable[[QueueItem], Awaitable[None]]] = None
        self._workers: list[asyncio.Task] = []
        self._tasks: set[asyncio.Task] = set()  # 持有在跑的转发协程，防 GC
        self._started = False

    # ------------------------------------------------------------------ 模型加载
    @staticmethod
    def _concurrency_of(m: dict) -> int:
        rp = m.get("runtime_params")
        if isinstance(rp, str):
            try:
                rp = json.loads(rp)
            except (ValueError, TypeError):
                rp = None
        try:
            conc = int((rp or {}).get("concurrency") or settings.default_model_concurrency)
        except (ValueError, TypeError):
            conc = settings.default_model_concurrency
        return max(1, conc)

    def reload_models(self, models: list[dict]) -> None:
        """根据模型列表（来自 DB）重建并发闸与挡位→模型映射。"""
        sems: dict[str, asyncio.Semaphore] = {}
        caps: dict[str, int] = {}
        tier_models: dict[str, list[str]] = {t: [] for t in TIERS}
        default_model: Optional[str] = None
        for m in models:
            if not m.get("is_enabled"):
                continue
            name = m["model_name"]
            tier = m.get("tier") or "medium"
            if tier not in TIERS:
                tier = "medium"
            conc = self._concurrency_of(m)
            sems[name] = asyncio.Semaphore(conc)
            caps[name] = conc
            tier_models[tier].append(name)
            if m.get("is_current") and default_model is None:
                default_model = name
        self.sems = sems
        self.caps = caps
        self.free = {name: conc for name, conc in caps.items()}
        self.tier_models = tier_models
        self.default_model = default_model or (models[0]["model_name"] if models else None)
        # 单模型（仅一个 enabled 模型）塌缩：三档都指向同一可用模型，
        # 避免半限降级时因目标空档无模型而直接 504（符合「单模型三档塌缩」设计意图）。
        enabled_names = [m["model_name"] for m in models if m.get("is_enabled")]
        if len(enabled_names) == 1:
            only = enabled_names[0]
            for t in TIERS:
                if not self.tier_models[t]:
                    self.tier_models[t] = [only]
        logger.info(
            "队列路由模型已加载: %s",
            {t: {"models": tier_models[t], "cap": sum(caps[n] for n in tier_models[t])}
             for t in TIERS},
        )

    async def refresh_from_db(self) -> None:
        """从数据库重载模型并发闸（模型增删改后调用）。"""
        async with db.pool.acquire() as conn:
            models = await ModelRepo.list_all(conn)
        self.reload_models(models)

    # ------------------------------------------------------------------ 挡位工具
    def wait_limit(self, tier: str) -> float:
        return float(settings.queue_tier_wait_limit_seconds.get(tier, 60))

    def _pick_model(self, tier: str) -> Optional[str]:
        """本挡位内选择剩余并发槽位最多的模型（最空闲兄弟模型）。"""
        models = self.tier_models.get(tier) or []
        if not models:
            return None
        return max(models, key=lambda n: self.free.get(n, 0))

    # ------------------------------------------------------------------ worker
    def start(self) -> None:
        if self._started:
            return
        for t in TIERS:
            self._workers.append(
                asyncio.create_task(self._worker(t), name=f"queue-worker-{t}")
            )
        self._started = True
        logger.info("三级队列 worker 已启动 (high/medium/low)")

    @property
    def started(self) -> bool:
        return self._started

    async def enqueue(self, item: QueueItem) -> None:
        """把请求工作项投入其当前挡位的队列。"""
        self.queues[item.current_tier].put_nowait(item)

    async def _worker(self, tier: str) -> None:
        while True:
            item = await self.queues[tier].get()
            try:
                await self._dispatch(item, tier)  # 只负责拿槽位 + 派发，不阻塞转发
            except Exception:  # noqa: BLE001 - worker 自愈，避免单点异常退出循环
                logger.exception("队列 worker 异常 request_id=%s", item.request_id)
                self._fail(item, 504, "队列处理异常")
            finally:
                self.queues[tier].task_done()

    async def _dispatch(self, item: QueueItem, tier: str) -> None:
        """并发派发：拿槽位成功后 create_task 后台转发，worker 立刻取下一个。

        这是修复串行化瓶颈的关键——旧版 _process 在 worker 里 await 整条
        forward_fn（httpx 往返），导致每挡位实际并发=1，Semaphore 形同虚设。
        """
        waited = time.monotonic() - item.enqueued_at
        limit = self.wait_limit(tier)
        next_tier = _NEXT_TIER[tier]
        models = self.tier_models.get(tier) or []

        # 1) 本挡位无可用模型 → 降级 / 504
        if not models:
            if next_tier:
                self._requeue(item, next_tier)
                return
            self._fail(item, 504, "无可用模型")
            return

        # 2) 选最空闲兄弟模型，尝试在预算时间内拿到并发槽位
        model = self._pick_model(tier)
        sem = self.sems.get(model) or asyncio.Semaphore(settings.default_model_concurrency)
        # 非最低挡位用「半限」预算，最低挡位用「全限」预算
        budget = (limit if not next_tier else limit * settings.queue_degrade_threshold_ratio) - waited
        if budget <= 0:
            if next_tier:
                self._requeue(item, next_tier)
                return
            self._fail(item, 504, "等待超时")
            return
        try:
            await asyncio.wait_for(sem.acquire(), timeout=budget)
        except asyncio.TimeoutError:
            if next_tier:
                self._requeue(item, next_tier)
                return
            self._fail(item, 504, "等待超时")
            return

        # 3) 拿到槽位 → 登记占用，create_task 后台转发，本方法立刻返回
        item.target_model = model
        self.free[model] = self.free.get(model, 0) - 1
        self.active[tier] += 1
        task = asyncio.create_task(
            self._forward_then_release(item, model, sem, tier),
            name=f"queue-forward-{item.request_id}",
        )
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _forward_then_release(self, item: QueueItem, model: str, sem: asyncio.Semaphore, tier: str) -> None:
        """后台转发协程：执行实际的 httpx 转发 + 最终释放槽位。"""
        try:
            if self.forward_fn is not None:
                await self.forward_fn(item)
            else:
                self._fail(item, 503, "队列转发未初始化")
        finally:
            self.active[tier] -= 1
            self.free[model] = self.free.get(model, 0) + 1
            sem.release()

    def _requeue(self, item: QueueItem, next_tier: str) -> None:
        item.current_tier = next_tier
        picked = self._pick_model(next_tier)
        if picked:
            item.target_model = picked
        self.queues[next_tier].put_nowait(item)
        logger.info(
            "请求 %s 在 %s 等待过久，软降级到 %s (target=%s)",
            item.request_id, item.current_tier, next_tier, item.target_model,
        )

    def _fail(self, item: QueueItem, status_code: int, detail: str) -> None:
        if item.is_stream:
            try:
                item.stream_q.put_nowait(
                    ("error", status_code, detail)
                )
                item.stream_q.put_nowait(STREAM_SENTINEL)
            except Exception:
                pass
            item.stream_done.set()
        else:
            if not item.future.done():
                item.future.set_exception(
                    _HttpException(status_code, detail)
                )

    # ------------------------------------------------------------------ 状态
    def status(self) -> dict:
        tiers: dict[str, dict] = {}
        for t in TIERS:
            models = self.tier_models.get(t) or []
            tiers[t] = {
                "queued": self.queues[t].qsize(),
                "active": self.active[t],
                "limit": sum(self.caps.get(m, 0) for m in models),
                "models": models,
            }
        return {
            "queue_enabled": settings.queue_enabled,
            "default_model": self.default_model,
            "tiers": tiers,
        }


class _HttpException(Exception):
    """轻量 HTTP 异常载体（避免 queue.py 直接依赖 fastapi）。"""

    def __init__(self, status_code: int, detail: str) -> None:
        self.status_code = status_code
        self.detail = detail
        super().__init__(detail)


# 模块级单例
queue_router = QueueRouter()
