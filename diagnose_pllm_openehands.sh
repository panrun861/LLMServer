#!/usr/bin/env bash
# diagnose_pllm_openehands.sh
# 一键诊断 OpenHands 接入 PLLM 失败的原因
# 用法: bash diagnose_pllm_openehands.sh <pllm_base_url> <pllm_sk_token> [model_name]
# 示例: bash diagnose_pllm_openehands.sh http://35.76.187.215:8080 pllm_sk_xxxxx qwen3.6-local

set -u

BASE_URL="${1:-http://35.76.187.215:8080}"
TOKEN="${2:-}"
MODEL="${3:-qwen3.6-local}"

c_red()    { printf '\033[31m%s\033[0m\n' "$1"; }
c_green()  { printf '\033[32m%s\033[0m\n' "$1"; }
c_yellow() { printf '\033[33m%s\033[0m\n' "$1"; }
c_blue()   { printf '\033[34m%s\033[0m\n' "$1"; }
hr()       { printf -- '--- %.0s' {1..60}; printf '\n'; }

hr
c_blue "PLLM ↔ OpenHands 接入诊断"
hr
printf "BASE_URL = %s\n" "$BASE_URL"
printf "TOKEN    = %s\n" "${TOKEN:0:12}…(masked)"
printf "MODEL    = %s\n" "$MODEL"
hr

# ---------- Step 1: 网络/端口连通性 ----------
c_yellow "[1/4] 健康检查  GET /health"
RESP=$(curl -sS -m 8 -w '\n__HTTP_CODE__%{http_code}' "$BASE_URL/health" 2>&1) || {
  c_red "  ✗ 无法连接 $BASE_URL —— 端口不通 / 防火墙拦截 / 服务未启动"
  c_red "  排查: docker ps | grep app ; 安全组放行 8080 ; 主机 firewall-cmd --list-ports"
  exit 1
}
CODE=$(printf '%s' "$RESP" | sed -n 's/.*__HTTP_CODE__//p')
BODY=$(printf '%s' "$RESP" | sed 's/__HTTP_CODE__.*//')
if [ "$CODE" = "200" ]; then
  c_green "  ✓ 200 OK  $BODY"
else
  c_red "  ✗ HTTP $CODE  $BODY"
  exit 1
fi

# ---------- Step 2: Token 鉴权 + 推理连通 ----------
c_yellow "[2/4] Token 鉴权 + 推理连通  POST /v1/chat/completions"
if [ -z "$TOKEN" ]; then
  c_red "  ✗ 未提供 TOKEN 参数。请先 POST /admin/pllm-tokens 签发一个 pllm_sk_… 再跑此脚本。"
  exit 1
fi
RESP=$(curl -sS -m 30 -w '\n__HTTP_CODE__%{http_code}' \
       -H "Authorization: Bearer $TOKEN" \
       -H "Content-Type: application/json" \
       -d "{\"model\":\"$MODEL\",\"messages\":[{\"role\":\"user\",\"content\":\"ping\"}],\"max_tokens\":16}" \
       "$BASE_URL/v1/chat/completions" 2>&1)
CODE=$(printf '%s' "$RESP" | sed -n 's/.*__HTTP_CODE__//p')
BODY=$(printf '%s' "$RESP" | sed 's/__HTTP_CODE__.*//')
case "$CODE" in
  200)
    c_green "  ✓ 200  Token 有效 + 模型 '$MODEL' 存在 + 推理链路通"
    printf "  响应片段: %s\n" "$(printf '%s' "$BODY" | head -c 200)"
    ;;
  401)
    c_red "  ✗ 401 Invalid or expired token"
    c_red "  → 你填的不是 pllm_sk_… 签发 token (可能是 LITELLM_MASTER_KEY)。"
    c_red "    修复: 通过 POST /admin/pllm-tokens (Ed25519 签名) 签发新 token，填到 OpenHands UI 的 API 密钥。"
    exit 2
    ;;
  404)
    c_red "  ✗ 404 Model not found —— 模型名仍不对"
    c_red "  → 你在 OpenHands UI 填的 'openai/qwen3.6-local' 多了 provider 前缀。"
    c_red "    PLLM 直接按 body.model 精确匹配 DB，不带 openai/ 前缀。"
    c_red "    修复: OpenHands UI → 自定义模型 改成裸名 (如 qwen3.6-local)。"
    exit 3
    ;;
  429)
    c_yellow "  ⚠ 429 Rate limit / 预算超限"
    printf "  %s\n" "$BODY"
    exit 4
    ;;
  *)
    c_red "  ✗ HTTP $CODE  $BODY"
    exit 2
    ;;
esac

# ---------- Step 3: 模型名匹配 ----------
c_yellow "[3/4] 模型名匹配  校验裸模型名 '$MODEL'"
if [ "$CODE" = "200" ]; then
  c_green "  ✓ PLLM 接受裸模型名 '$MODEL'（Step 2 已端到端验证）"
elif [ "$CODE" = "404" ]; then
  c_red "  ✗ 模型名 '$MODEL' 未通过校验（见 Step 2 的 404 提示）"
  exit 3
else
  c_red "  ✗ Step 2 未通过，模型名匹配无法继续"
  exit 2
fi

# ---------- Step 4: 端到端推理 ----------
c_yellow "[4/4] 端到端推理  POST /v1/chat/completions"
RESP=$(curl -sS -m 30 -w '\n__HTTP_CODE__%{http_code}' \
       -H "Authorization: Bearer $TOKEN" \
       -H "Content-Type: application/json" \
       -d "{\"model\":\"$MODEL\",\"messages\":[{\"role\":\"user\",\"content\":\"ping\"}],\"max_tokens\":16}" \
       "$BASE_URL/v1/chat/completions" 2>&1)
CODE=$(printf '%s' "$RESP" | sed -n 's/.*__HTTP_CODE__//p')
BODY=$(printf '%s' "$RESP" | sed 's/__HTTP_CODE__.*//')
case "$CODE" in
  200)
    c_green "  ✓ 200 推理链路通"
    printf "  响应片段: %s\n" "$(printf '%s' "$BODY" | head -c 200)"
    hr
    c_green "全部通过。如果 OpenHands 仍报 'did not include a function call or a message',"
    c_green "检查 vLLM 的 --enable-auto-tool-choice / --tool-call-parser qwen3_coder 是否生效,"
    c_green "以及 OpenHands 是否开了流式 (PLLM 流式只转发 data: 前缀的行)。"
    ;;
  404)
    c_red "  ✗ 404 Model not found —— 模型名仍不对 (回到 Step 3)"
    ;;
  429)
    c_yellow "  ⚠ 429 Rate limit / 预算超限"
    printf "  %s\n" "$BODY"
    ;;
  502|504)
    c_red "  ✗ $CODE 上游 LiteLLM/vLLM 不可达 —— 检查 LITELLM_API_BASE / vllm 容器状态"
    printf "  %s\n" "$BODY"
    ;;
  *)
    c_red "  ✗ HTTP $CODE"
    printf "  %s\n" "$BODY"
    ;;
esac
hr
