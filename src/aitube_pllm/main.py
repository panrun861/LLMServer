"""AITube-PLLM 主应用程序入口"""

import asyncio
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from .config import settings
from .db.pool import db
from .middleware.signature_verification import SignatureVerificationMiddleware
from .api.token_management import router as token_router
from .api.model_management import router as model_router
from .api.usage_and_audit import router as usage_audit_router
from .api.inference_gateway import router as inference_router
from .api.dashboard import router as dashboard_router
from .tasks.model_sync import sync_models_from_upstream

logger = logging.getLogger(__name__)


async def _periodic_model_sync() -> None:
    """周期同步后台任务：按 model_sync_interval 循环调用同步。"""
    while True:
        await asyncio.sleep(settings.model_sync_interval)
        try:
            await sync_models_from_upstream()
        except Exception:  # noqa: BLE001 - 周期任务需自愈，避免单点异常退出循环
            logger.exception("周期模型同步异常（已忽略，下个周期重试）")


@asynccontextmanager
async def lifespan(app: FastAPI):
    """应用程序生命周期管理"""
    await db.connect()
    sync_task: asyncio.Task | None = None
    if settings.model_sync_interval and settings.model_sync_interval > 0:
        sync_task = asyncio.create_task(_periodic_model_sync())
        logger.info("已启动周期模型同步（间隔 %ss）", settings.model_sync_interval)
    yield
    if sync_task is not None:
        sync_task.cancel()
        try:
            await sync_task
        except asyncio.CancelledError:
            pass
    await db.disconnect()


def create_app() -> FastAPI:
    """创建 FastAPI 应用程序实例"""
    app = FastAPI(
        title="AITube-PLLM",
        description="企业内部私有 LLM 推理基础设施",
        version="1.0.0",
        lifespan=lifespan,
    )
    
    # 添加 CORS 中间件
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )
    
    # 添加签名验证中间件
    app.add_middleware(SignatureVerificationMiddleware)
    
    # 注册路由
    app.include_router(token_router)
    app.include_router(model_router)
    app.include_router(usage_audit_router)
    app.include_router(inference_router)
    app.include_router(dashboard_router)
    
    # 健康检查端点
    @app.get("/health")
    async def health_check():
        return {"status": "healthy", "version": "1.0.0"}
    
    return app


app = create_app()
