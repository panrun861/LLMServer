"""单独签发 Token 工具 (运维验证专用)

用法: python -m aitube_pllm.tools.issue_token --subject-id <id> --issuer-id <id>
"""

import argparse
import asyncio
import sys

from ..db.pool import DatabasePool
from ..db.repos import TokenRepo


async def issue_token(
    issuer_id: str,
    subject_id: str,
    name: str | None = None,
    token_budget: int | None = None,
    token_budget_period: str | None = None,
):
    """签发新 Token (管理员运维验证专用)"""
    db_pool = DatabasePool()
    await db_pool.connect()
    
    try:
        async with db_pool.pool.acquire() as conn:
            record, plaintext = await TokenRepo.issue(
                conn,
                issuer_id=issuer_id,
                subject_id=subject_id,
                name=name,
                token_budget=token_budget,
                token_budget_period=token_budget_period,
            )
            
            print(f"✓ Token 签发成功")
            print(f"  Token ID: {record['pllm_token_id']}")
            print(f"  Subject:  {subject_id}")
            print(f"  Issuer:   {issuer_id}")
            if name:
                print(f"  Name:     {name}")
            if token_budget:
                print(f"  Budget:   {token_budget} tokens / {token_budget_period}")
            print()
            print(f"⚠  明文 Token (仅显示一次，请妥善保管):")
            print(f"   {plaintext}")
            
    finally:
        await db_pool.disconnect()


def main():
    """CLI 入口"""
    parser = argparse.ArgumentParser(
        description="单独签发 Token (运维验证专用)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
示例:
  # 基本签发
  python -m aitube_pllm.tools.issue_token --issuer-id admin --subject-id test-user
  
  # 带预算限制
  python -m aitube_pllm.tools.issue_token \\
    --issuer-id admin \\
    --subject-id test-user \\
    --name "测试Token" \\
    --token-budget 100000 \\
    --token-budget-period daily

注意:
  - 此工具仅供管理员运维验证使用
  - issuer_id 应为 'pllm_admin_cli' (由本地管理CLI自动设置)
  - 签发的 Token 明文仅显示一次，请妥善保管
  - 验证完毕后请及时吊销
        """
    )
    
    parser.add_argument(
        "--issuer-id",
        default="pllm_admin_cli",
        help="签发者 ID (默认: pllm_admin_cli)"
    )
    
    parser.add_argument(
        "--subject-id",
        required=True,
        help="主体 ID (例如: test-user, debug-session)"
    )
    
    parser.add_argument(
        "--name",
        help="Token 名称 (可选)"
    )
    
    parser.add_argument(
        "--token-budget",
        type=int,
        help="Token 预算 (可选)"
    )
    
    parser.add_argument(
        "--token-budget-period",
        choices=["daily", "monthly", "total"],
        help="预算周期 (可选, 需要与 --token-budget 一起使用)"
    )
    
    args = parser.parse_args()
    
    # 验证预算参数
    if args.token_budget and not args.token_budget_period:
        print("错误: 指定 --token-budget 时必须同时指定 --token-budget-period", file=sys.stderr)
        sys.exit(1)
    
    if args.token_budget_period and not args.token_budget:
        print("错误: 指定 --token-budget-period 时必须同时指定 --token-budget", file=sys.stderr)
        sys.exit(1)
    
    # 执行签发
    asyncio.run(issue_token(
        issuer_id=args.issuer_id,
        subject_id=args.subject_id,
        name=args.name,
        token_budget=args.token_budget,
        token_budget_period=args.token_budget_period,
    ))


if __name__ == "__main__":
    main()
