"""AITube-PLLM 主应用程序入口"""

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


@asynccontextmanager
async def lifespan(app: FastAPI):
    """应用程序生命周期管理"""
    await db.connect()
    yield
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
    
    # 健康检查端点
    @app.get("/health")
    async def health_check():
        return {"status": "healthy", "version": "1.0.0"}
    
    return app


app = create_app()
