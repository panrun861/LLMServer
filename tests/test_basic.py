"""AITube-PLLM 测试套件"""

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession
from sqlalchemy.orm import sessionmaker

from aitube_pllm.main import app
from aitube_pllm.db.pool import DatabasePool


@pytest.fixture
def client():
    """创建测试客户端"""
    return TestClient(app)


@pytest.fixture
async def db_pool():
    """创建测试数据库连接池"""
    # 使用内存 SQLite 进行测试
    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        echo=False,
    )
    
    async_session = sessionmaker(
        engine, class_=AsyncSession, expire_on_commit=False
    )
    
    pool = DatabasePool(engine, async_session)
    await pool.connect()
    
    yield pool
    
    await pool.disconnect()


def test_health_check(client):
    """测试健康检查端点"""
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "healthy", "version": "1.0.0"}


def test_root(client):
    """测试根路径"""
    response = client.get("/")
    assert response.status_code == 200
    assert "AITube-PLLM" in response.json()["message"]


@pytest.mark.asyncio
async def test_token_lifecycle(db_pool):
    """测试 Token 生命周期"""
    from aitube_pllm.db.repos import TokenRepo
    
    async with db_pool.acquire() as conn:
        # 签发 Token
        record = await TokenRepo.issue(
            conn,
            issuer_id="test-issuer",
            subject_id="test-subject",
            plaintext="pllm_test_token_123",
            name="Test Token",
        )
        
        assert record["pllm_token_id"] is not None
        assert record["issuer_id"] == "test-issuer"
        assert record["subject_id"] == "test-subject"
        
        # 查询 Token
        retrieved = await TokenRepo.get_by_id(conn, record["pllm_token_id"])
        assert retrieved is not None
        assert retrieved["pllm_token_id"] == record["pllm_token_id"]
        
        # 吊销 Token
        revoked = await TokenRepo.revoke(conn, record["pllm_token_id"])
        assert revoked is True
        
        # 验证已吊销
        after_revoke = await TokenRepo.get_by_id(conn, record["pllm_token_id"])
        assert after_revoke["is_active"] is False


@pytest.mark.asyncio
async def test_issuer_registration(db_pool):
    """测试签发者注册"""
    from aitube_pllm.db.repos import IssuerRepo
    
    async with db_pool.acquire() as conn:
        # 注册签发者
        await IssuerRepo.upsert(conn, "test-issuer", "test-public-key")
        
        # 查询签发者
        issuer = await IssuerRepo.get_active(conn, "test-issuer")
        assert issuer is not None
        assert issuer["issuer_id"] == "test-issuer"
        assert issuer["public_key"] == "test-public-key"


@pytest.mark.asyncio
async def test_model_registration(db_pool):
    """测试模型登记"""
    from aitube_pllm.db.repos import ModelRepo
    
    async with db_pool.acquire() as conn:
        # 注册模型
        record = await ModelRepo.register(
            conn,
            model_name="qwen-chat",
            tier="medium",
            model_artifact="Qwen/Qwen2.5-72B",
            inference_engine="vllm",
            context_length=32768,
            api_base="http://localhost:8000/v1",
        )
        
        assert record["id"] is not None
        assert record["model_name"] == "qwen-chat"
        assert record["tier"] == "medium"
        
        # 查询模型
        retrieved = await ModelRepo.get_by_name_and_tier(conn, "qwen-chat", "medium")
        assert retrieved is not None
        assert retrieved["model_name"] == "qwen-chat"


def test_inference_gateway_no_auth(client):
    """测试推理网关未认证"""
    response = client.post(
        "/v1/chat/completions",
        json={
            "model": "test-model",
            "messages": [{"role": "user", "content": "hello"}],
        }
    )
    # 应该返回 401 未认证
    assert response.status_code == 401


def test_admin_api_no_signature(client):
    """测试管理 API 无签名"""
    response = client.post(
        "/admin/tokens",
        json={"issuer_id": "test", "subject_id": "test"}
    )
    # 应该返回 401 无签名
    assert response.status_code == 401
