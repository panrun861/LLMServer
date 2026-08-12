#!/usr/bin/env python3
"""
Ed25519 签名测试客户端 —— 验证 PLLM /admin/* 控制平面认证。

原理（必须与 signature_verification.py:164-202 完全一致）:
    签名串 = f"{METHOD}\\n{request_target}\\n{audience}\\n{timestamp}\\n{nonce}\\n{sha256(body)}"
    其中 request_target = path + ("?" + query)  (无 query 则只有 path)
    用私钥 SigningKey.sign(签名串) 取 64 字节签名，hex 后放入 x-signature 头。

用法:
    python scripts/test_ed25519_admin.py                 # 默认 GET /admin/pllm-tokens
    python scripts/test_ed25519_admin.py GET  /admin/pllm-tokens
    python scripts/test_ed25519_admin.py POST /admin/pllm-tokens '{"issuer_id":"aitube-admin",...}'
"""
import sys
import os
import json
import uuid
import hashlib
from datetime import datetime, timezone

from nacl.signing import SigningKey
import httpx

KEYS_PATH = os.path.join(os.path.dirname(__file__), "..", "keys.json")
BASE_URL = "http://35.76.187.215:8080"
AUDIENCE = "aitube-pllm"


def load_keys(path):
    with open(path) as f:
        return json.load(f)


def build_signature(keys, method, target, body_bytes, timestamp_str, nonce):
    # request_target: 服务端用 request.url.path + (?query)
    request_target = target
    body_hash = hashlib.sha256(body_bytes).hexdigest()
    sign_content = f"{method}\n{request_target}\n{AUDIENCE}\n{timestamp_str}\n{nonce}\n{body_hash}"
    signing_key = SigningKey(bytes.fromhex(keys["private_key"]))
    signature = signing_key.sign(sign_content.encode("utf-8")).signature  # 64 bytes
    return signature.hex(), sign_content


def main():
    method = (sys.argv[1] if len(sys.argv) > 1 else "GET").upper()
    target = sys.argv[2] if len(sys.argv) > 2 else "/admin/pllm-tokens"
    body_str = sys.argv[3] if len(sys.argv) > 3 else None
    body_bytes = body_str.encode("utf-8") if body_str else b""

    keys = load_keys(KEYS_PATH)
    timestamp_str = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S+00:00")
    nonce = uuid.uuid4().hex

    signature_hex, sign_content = build_signature(
        keys, method, target, body_bytes, timestamp_str, nonce
    )

    headers = {
        "x-issuer-id": keys["issuer_id"],
        "x-key-id": keys["key_id"],
        "x-audience": AUDIENCE,
        "x-timestamp": timestamp_str,
        "x-nonce": nonce,
        "x-signature": signature_hex,
        "Content-Type": "application/json",
    }

    url = BASE_URL + target
    print("=== SIGN CONTENT (server 将据此验签) ===")
    print(sign_content)
    print("=== HEADERS ===")
    for k, v in headers.items():
        print(f"  {k}: {v}")
    print(f"\n=== REQUEST {method} {url} ===")

    with httpx.Client(timeout=15) as client:
        if method == "GET":
            resp = client.get(url, headers=headers)
        elif method == "POST":
            resp = client.post(url, headers=headers, content=body_bytes)
        elif method == "DELETE":
            resp = client.request("DELETE", url, headers=headers)
        else:
            resp = client.request(method, url, headers=headers, content=body_bytes)

    print(f"STATUS: {resp.status_code}")
    print("BODY:", resp.text[:2000])


if __name__ == "__main__":
    main()
