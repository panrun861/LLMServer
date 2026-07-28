"""数据库连接池管理"""

import asyncpg
from typing import AsyncGenerator
from ..config import settings


class DatabasePool:
    """异步数据库连接池"""

    def __init__(self):
        self.pool: asyncpg.Pool | None = None

    async def connect(self):
        """创建连接池"""
        self.pool = await asyncpg.create_pool(
            dsn=settings.database_url,
            min_size=settings.database_pool_min,
            max_size=settings.database_pool_max,
        )

    async def disconnect(self):
        """关闭连接池"""
        if self.pool:
            await self.pool.close()

    async def get_connection(self) -> AsyncGenerator[asyncpg.Connection, None]:
        """获取数据库连接"""
        if not self.pool:
            raise RuntimeError("Database pool not initialized")
        async with self.pool.acquire() as conn:
            yield conn


db = DatabasePool()
