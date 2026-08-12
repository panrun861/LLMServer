"""AES-CBC 加解密工具（用于加密存储外部 API key）

使用环境变量 PLLM_ENCRYPTION_KEY 作为 AES 密钥（Base64 编码的 32 字节密钥）。
加密格式：AES-CBC + PKCS7 padding，IV 前 16 字节随机生成并拼接到密文前。
最终存储格式：base64(IV + ciphertext)，便于存入 PostgreSQL TEXT 列。

警告：
- ENCRYPTION_KEY 丢失 = 所有已加密的 api_key 不可恢复
- 建议定期备份 .env 中的 ENCRYPTION_KEY
- 更换密钥需要全量重新加密所有模型
"""

from __future__ import annotations

import base64
import os
import logging
from typing import Optional

from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from cryptography.hazmat.primitives import padding
from cryptography.hazmat.backends import default_backend

logger = logging.getLogger(__name__)

# AES block size
_BLOCK_SIZE = 128  # bits

# AES key length
_KEY_SIZE = 32  # bytes = 256 bits


def _get_aes_key(env_var: str = "PLLM_ENCRYPTION_KEY") -> bytes | None:
    """从环境变量获取 AES 密钥。

    期望格式：Base64 编码的 32 字节密钥。
    若环境变量缺失或长度不对，返回 None。
    """
    raw = os.environ.get(env_var)
    if not raw:
        return None
    try:
        key = base64.b64decode(raw)
        if len(key) not in (16, 24, 32):
            logger.error(
                "ENCRYPTION_KEY 长度 %d 不符合 AES 要求（需 16/24/32 字节）", len(key)
            )
            return None
        return key
    except Exception as exc:
        logger.error("ENCRYPTION_KEY 解码失败: %s", exc)
        return None


def _generate_iv() -> bytes:
    """生成随机 IV（16 字节）"""
    return os.urandom(16)


def encrypt(plaintext: str, key_bytes: bytes | None = None) -> str | None:
    """加密字符串，返回 base64(IV + ciphertext)。

    Args:
        plaintext: 明文（如 api_key）
        key_bytes: AES 密钥（默认从 ENV 读取）

    Returns:
        Base64 编码的密文字符串，或 None（密钥未配置时）
    """
    if plaintext is None:
        return None
    key = key_bytes or _get_aes_key()
    if not key:
        logger.warning("加密失败：ENCRYPTION_KEY 未配置，将不加密")
        return None

    iv = _generate_iv()
    padder = padding.PKCS7(_BLOCK_SIZE).padder()
    padded_data = padder.update(plaintext.encode("utf-8")) + padder.finalize()

    cipher = Cipher(algorithms.AES(key), modes.CBC(iv), backend=default_backend())
    encryptor = cipher.encryptor()
    ciphertext = encryptor.update(padded_data) + encryptor.finalize()

    # 存储格式：IV + ciphertext，再 base64
    encrypted = base64.b64encode(iv + ciphertext).decode("ascii")
    return encrypted


def decrypt(encrypted: str | None, key_bytes: bytes | None = None) -> Optional[str]:
    """解密 base64(IV + ciphertext) 字符串。

    Args:
        encrypted: 加密后的字符串
        key_bytes: AES 密钥（默认从 ENV 读取）

    Returns:
        解密的明文字符串，失败或输入为 None 时返回 None
    """
    if not encrypted:
        return None
    key = key_bytes or _get_aes_key()
    if not key:
        logger.warning("解密失败：ENCRYPTION_KEY 未配置")
        return None

    try:
        raw = base64.b64decode(encrypted)
        if len(raw) < 17:
            logger.error("密文过短: %d bytes", len(raw))
            return None
        iv = raw[:16]
        ciphertext = raw[16:]

        cipher = Cipher(algorithms.AES(key), modes.CBC(iv), backend=default_backend())
        decryptor = cipher.decryptor()
        padded_plaintext = decryptor.update(ciphertext) + decryptor.finalize()

        unpadder = padding.PKCS7(_BLOCK_SIZE).unpadder()
        plaintext = unpadder.update(padded_plaintext) + unpadder.finalize()
        return plaintext.decode("utf-8")
    except Exception as exc:
        logger.error("解密异常: %s", exc)
        return None


def generate_encryption_key() -> str:
    """生成一个随机的 AES-256 密钥（Base64 编码），供写入 .env。

    使用示例：
        $ python -c "from aitube_pllm.utils.crypto import generate_encryption_key; print(generate_encryption_key())"
        $ export PLLM_ENCRYPTION_KEY=<上面的输出>
    """
    key = os.urandom(_KEY_SIZE)
    return base64.b64encode(key).decode("ascii")