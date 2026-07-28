"""设置 Token 速率限制工具

用法: python -m aitube_pllm.tools.set_token_rate --token-id <uuid> --rate <rpm>
"""

import argparse
import asyncio
import sys

from ..db.pool import DatabasePool
from ..db.repos import TokenRepo


async def set_token_rate(token_id: str, rate: int):
    """设置 Token 的速率限制"""
    db_pool = DatabasePool()
    await db_pool.connect()
    
    try:
        async with db_pool.pool.acquire() as conn:
            result = await TokenRepo.update_rate(conn, token_id, rate)
            if result:
                print(f"✓ Token '{token_id}' 速率限制已设置为 {rate} RPM")
            else:
                print(f"✗ Token '{token_id}' 不存在", file=sys.stderr)
                sys.exit(1)
    finally:
        await db_pool.disconnect()


def main():
    """CLI 入口"""
    parser = argparse.ArgumentParser(
        description="设置 Token 速率限制",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
示例:
  python -m aitube_pllm.tools.set_token_rate --token-id "550e8400-e29b-41d4-a716-446655440000" --rate 100
        """
    )
    
    parser.add_argument(
        "--token-id",
        required=True,
        help="Token ID (UUID 格式)"
    )
    
    parser.add_argument(
        "--rate",
        type=int,
        required=True,
        help="速率限制 (RPM - 每分钟请求数)"
    )
    
    args = parser.parse_args()
    
    # 验证速率
    if args.rate < 0:
        print("错误: 速率限制不能为负数", file=sys.stderr)
        sys.exit(1)
    
    # 执行设置
    asyncio.run(set_token_rate(args.token_id, args.rate))


if __name__ == "__main__":
    main()
