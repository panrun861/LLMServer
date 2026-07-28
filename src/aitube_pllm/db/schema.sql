-- AITube-PLLM PostgreSQL Schema
-- 8个表的完整DDL

-- 1. issuers (签发者公钥注册表)
CREATE TABLE IF NOT EXISTS issuers (
    issuer_id VARCHAR(64) PRIMARY KEY,
    key_id VARCHAR(128) NOT NULL,
    public_key TEXT NOT NULL,
    is_active BOOLEAN NOT NULL DEFAULT TRUE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- 2. pllm_tokens (Token台账)
CREATE TABLE IF NOT EXISTS pllm_tokens (
    id BIGSERIAL PRIMARY KEY,
    pllm_token_id UUID NOT NULL DEFAULT gen_random_uuid(),
    issuer_id VARCHAR(64) NOT NULL,
    subject_id VARCHAR(128),
    pllm_token_hash VARCHAR(64) NOT NULL UNIQUE,
    name VARCHAR(255),
    rate_limit_rpm INTEGER,
    token_budget BIGINT,
    token_budget_period VARCHAR(16) CHECK (token_budget_period IN ('daily', 'monthly', 'total')),
    is_active BOOLEAN NOT NULL DEFAULT TRUE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    revoked_at TIMESTAMPTZ,
    last_used_at TIMESTAMPTZ,
    UNIQUE(pllm_token_id),
    UNIQUE(issuer_id, subject_id, pllm_token_hash)
);

CREATE INDEX IF NOT EXISTS idx_pllm_tokens_issuer_subject ON pllm_tokens(issuer_id, subject_id);
CREATE INDEX IF NOT EXISTS idx_pllm_tokens_active ON pllm_tokens(is_active) WHERE is_active = TRUE;

-- 3. used_nonces (已使用nonce，防重放)
CREATE TABLE IF NOT EXISTS used_nonces (
    issuer_id VARCHAR(64) NOT NULL,
    nonce VARCHAR(128) NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    expires_at TIMESTAMPTZ NOT NULL,
    PRIMARY KEY (issuer_id, nonce)
);

CREATE INDEX IF NOT EXISTS idx_used_nonces_expires_at ON used_nonces(expires_at);

-- 4. models (模型登记表)
CREATE TABLE IF NOT EXISTS models (
    id BIGSERIAL PRIMARY KEY,
    model_name VARCHAR(128) NOT NULL,
    tier VARCHAR(32) NOT NULL DEFAULT 'medium',
    model_artifact VARCHAR(255) NOT NULL,
    inference_engine VARCHAR(64) NOT NULL,
    context_length INTEGER NOT NULL,
    api_base VARCHAR(512),
    runtime_params JSONB,
    request_params JSONB,
    is_current BOOLEAN NOT NULL DEFAULT FALSE,
    is_enabled BOOLEAN NOT NULL DEFAULT TRUE,
    sync_status VARCHAR(32) NOT NULL DEFAULT 'pending' CHECK (sync_status IN ('pending', 'synced', 'failed')),
    last_synced_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE(model_name, tier)
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_models_current ON models(model_name) WHERE is_current = TRUE;

-- 5. usage_logs (使用量明细，保留30天)
CREATE TABLE IF NOT EXISTS usage_logs (
    id BIGSERIAL PRIMARY KEY,
    request_id UUID NOT NULL DEFAULT gen_random_uuid(),
    pllm_token_ref_id BIGINT REFERENCES pllm_tokens(id) ON DELETE SET NULL,
    pllm_token_id_snapshot UUID NOT NULL,
    issuer_id_snapshot VARCHAR(64) NOT NULL,
    subject_id_snapshot VARCHAR(128),
    model VARCHAR(128) NOT NULL,
    tier_snapshot VARCHAR(32),
    inference_engine_snapshot VARCHAR(64),
    prompt_tokens INTEGER NOT NULL DEFAULT 0,
    completion_tokens INTEGER NOT NULL DEFAULT 0,
    total_tokens INTEGER NOT NULL DEFAULT 0,
    latency_ms INTEGER NOT NULL,
    status_code INTEGER NOT NULL,
    correlation_id VARCHAR(255),
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_usage_logs_created_at ON usage_logs(created_at);
CREATE INDEX IF NOT EXISTS idx_usage_logs_pllm_token ON usage_logs(pllm_token_ref_id, created_at);
CREATE INDEX IF NOT EXISTS idx_usage_logs_request_id ON usage_logs(request_id);

-- 6. usage_counters (使用量计数器，用于预算判定)
CREATE TABLE IF NOT EXISTS usage_counters (
    id BIGSERIAL PRIMARY KEY,
    pllm_token_id UUID NOT NULL,
    period_type VARCHAR(16) NOT NULL CHECK (period_type IN ('daily', 'monthly', 'total')),
    period_start DATE NOT NULL,
    used_tokens BIGINT NOT NULL DEFAULT 0,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE(pllm_token_id, period_type, period_start)
);

CREATE INDEX IF NOT EXISTS idx_usage_counters_token_period ON usage_counters(pllm_token_id, period_type, period_start);

-- 7. event_logs (业务操作审计日志)
CREATE TABLE IF NOT EXISTS event_logs (
    id BIGSERIAL PRIMARY KEY,
    event_id UUID NOT NULL DEFAULT gen_random_uuid(),
    actor_type VARCHAR(32) NOT NULL,
    actor_id VARCHAR(128),
    action VARCHAR(64) NOT NULL,
    target_type VARCHAR(64),
    target_id VARCHAR(255),
    detail JSONB,
    result VARCHAR(32) NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_event_logs_created_at ON event_logs(created_at);
CREATE INDEX IF NOT EXISTS idx_event_logs_actor ON event_logs(actor_type, actor_id, created_at);
CREATE INDEX IF NOT EXISTS idx_event_logs_action ON event_logs(action, created_at);

-- 8. security_event_logs (安全事件审计日志，append-only)
CREATE TABLE IF NOT EXISTS security_event_logs (
    id BIGSERIAL PRIMARY KEY,
    security_event_id UUID NOT NULL DEFAULT gen_random_uuid(),
    gateway_request_id UUID NOT NULL,
    occurred_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    method VARCHAR(10) NOT NULL,
    endpoint VARCHAR(255) NOT NULL,
    decision VARCHAR(16) NOT NULL CHECK (decision IN ('accepted', 'rejected')),
    issuer_id_claim VARCHAR(64),
    key_id_claim VARCHAR(128),
    source_address VARCHAR(45),
    reason_code VARCHAR(64) NOT NULL,
    response_status INTEGER,
    detail JSONB
);

CREATE INDEX IF NOT EXISTS idx_security_logs_occurred_at ON security_event_logs(occurred_at);
CREATE INDEX IF NOT EXISTS idx_security_logs_issuer ON security_event_logs(issuer_id_claim, occurred_at);
CREATE INDEX IF NOT EXISTS idx_security_logs_decision ON security_event_logs(decision, occurred_at);
