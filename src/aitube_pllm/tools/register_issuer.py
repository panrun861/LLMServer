"""注册签发者公钥工具

用法: python -m aitube_pllm.tools.register_issuer --issuer-id <id> --public-key <key>
"""

import argparse
import asyncio
import sys
from pathlib import Path

from ..db.pool import DatabasePool
from ..db.repos import IssuerRepo


async def register_issuer(issuer_id: str, key_id: str, public_key: str):
    """注册签发者公钥到数据库"""
    db_pool = DatabasePool()
    await db_pool.connect()
    
    try:
        async with db_pool.pool.acquire() as conn:
            await IssuerRepo.upsert(conn, issuer_id, key_id, public_key)
            print(f"✓ 签发者 '{issuer_id}' (key: {key_id}) 公钥注册成功")
    finally:
        await db_pool.disconnect()


def main():
    """CLI 入口"""
    parser = argparse.ArgumentParser(
        description="注册签发者公钥",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
示例:
  python -m aitube_pllm.tools.register_issuer --issuer-id rag --public-key-file ./rag_public.pem
  
  或直接传入公钥:
  python -m aitube_pllm.tools.register_issuer --issuer-id rag --public-key "LS0tLS1CRUdJTi..."
        """
    )
    
    parser.add_argument(
        "--issuer-id",
        required=True,
        help="签发者 ID (例如: rag, admin)"
    )
    
    parser.add_argument(
        "--key-id",
        required=True,
        help="密钥 ID (例如: key-2024, prod-key)"
    )
    
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument(
        "--public-key",
        help="Base64 编码的公钥字符串"
    )
    group.add_argument(
        "--public-key-file",
        type=Path,
        help="公钥文件路径 (PEM 格式)"
    )
    
    args = parser.parse_args()
    
    # 获取公钥
    if args.public_key_file:
        if not args.public_key_file.exists():
            print(f"错误: 公钥文件不存在: {args.public_key_file}", file=sys.stderr)
            sys.exit(1)
        
        public_key = args.public_key_file.read_text().strip()
    else:
        public_key = args.public_key
    
    # 清理公钥格式
    public_key = public_key.replace("-----BEGIN PUBLIC KEY-----", "")
    public_key = public_key.replace("-----END PUBLIC KEY-----", "")
    public_key = "".join(public_key.split())
    
    # 执行注册
    asyncio.run(register_issuer(args.issuer_id, args.key_id, public_key))


if __name__ == "__main__":
    main()
