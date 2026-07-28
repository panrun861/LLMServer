"""AITube-PLLM CLI 工具模块"""

from .register_issuer import main as register_issuer
from .set_token_rate import main as set_token_rate
from .issue_token import main as issue_token

__all__ = ["register_issuer", "set_token_rate", "issue_token"]
