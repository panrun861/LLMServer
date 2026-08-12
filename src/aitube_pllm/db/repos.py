"""数据库仓库层 - 封装所有表的 CRUD 操作"""

from __future__ import annotations

import hashlib
import json
import secrets
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

import asyncpg

from .pool import db


def _generate_token() -> tuple[str, str, str]:
    """生成 PLLM-Token: (明文, hash, prefix)"""
    raw = "pllm_sk_" + secrets.token_urlsafe(32)
    h = hashlib.sha256(raw.encode()).hexdigest()
    prefix = raw[:12]
    return raw, h, prefix


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class IssuerRepo:
    """签发者仓库"""

    @staticmethod
    async def get_active(issuer_id: str, key_id: str, conn: asyncpg.Connection) -> dict | None:
        return await conn.fetchrow(
            "SELECT * FROM issuers WHERE issuer_id = $1 AND key_id = $2 AND is_active = TRUE",
            issuer_id, key_id,
        )

    @staticmethod
    async def upsert(conn: asyncpg.Connection, issuer_id: str, key_id: str, public_key: str) -> None:
        await conn.execute(
            """INSERT INTO issuers (issuer_id, key_id, public_key)
               VALUES ($1, $2, $3)
               ON CONFLICT (issuer_id) DO UPDATE
               SET key_id = EXCLUDED.key_id, public_key = EXCLUDED.public_key""",
            issuer_id, key_id, public_key,
        )


class NonceRepo:
    """Nonce 仓库"""

    @staticmethod
    async def is_used(issuer_id: str, nonce: str, conn: asyncpg.Connection) -> bool:
        row = await conn.fetchval(
            "SELECT 1 FROM used_nonces WHERE issuer_id = $1 AND nonce = $2",
            issuer_id, nonce,
        )
        return row is not None

    @staticmethod
    async def record(issuer_id: str, nonce: str, expires_at: datetime, conn: asyncpg.Connection) -> None:
        await conn.execute(
            "INSERT INTO used_nonces (issuer_id, nonce, expires_at) VALUES ($1, $2, $3)",
            issuer_id, nonce, expires_at,
        )

    @staticmethod
    async def purge_expired(conn: asyncpg.Connection) -> int:
        result = await conn.execute("DELETE FROM used_nonces WHERE expires_at < NOW()")
        return int(result.split()[-1]) if result else 0


class TokenRepo:
    """PLLM-Token 仓库"""

    @staticmethod
    async def issue(
        conn: asyncpg.Connection,
        *,
        issuer_id: str,
        subject_id: str | None,
        name: str | None,
        token_budget: int | None,
        token_budget_period: str | None,
        rate_limit_rpm: int | None = None,
    ) -> tuple[dict, str]:
        """签发新Token, 返回 (record_dict, plaintext_token)"""
        plaintext, token_hash, prefix = _generate_token()
        row = await conn.fetchrow(
            """INSERT INTO pllm_tokens
               (issuer_id, subject_id, pllm_token_hash, name,
                rate_limit_rpm, token_budget, token_budget_period)
               VALUES ($1, $2, $3, $4, $5, $6, $7)
               RETURNING *""",
            issuer_id, subject_id, token_hash, name,
            rate_limit_rpm, token_budget, token_budget_period,
        )
        return dict(row), plaintext

    @staticmethod
    async def get_active_by_subject(
        conn: asyncpg.Connection, issuer_id: str, subject_id: str,
    ) -> list[dict]:
        rows = await conn.fetch(
            """SELECT * FROM pllm_tokens
               WHERE issuer_id = $1 AND subject_id = $2 AND is_active = TRUE
               ORDER BY created_at DESC""",
            issuer_id, subject_id,
        )
        return [dict(r) for r in rows]

    @staticmethod
    async def get_by_hash(conn: asyncpg.Connection, token_hash: str) -> dict | None:
        return await conn.fetchrow(
            "SELECT * FROM pllm_tokens WHERE pllm_token_hash = $1 AND is_active = TRUE",
            token_hash,
        )

    @staticmethod
    async def get_by_id(conn: asyncpg.Connection, pllm_token_id: uuid.UUID) -> dict | None:
        return await conn.fetchrow(
            "SELECT * FROM pllm_tokens WHERE pllm_token_id = $1",
            pllm_token_id,
        )

    @staticmethod
    async def revoke_by_id(conn: asyncpg.Connection, pllm_token_id: uuid.UUID) -> int:
        result = await conn.execute(
            """UPDATE pllm_tokens SET is_active = FALSE, revoked_at = NOW()
               WHERE pllm_token_id = $1 AND is_active = TRUE""",
            pllm_token_id,
        )
        return int(result.split()[-1]) if result else 0

    @staticmethod
    async def revoke_by_subject(conn: asyncpg.Connection, issuer_id: str, subject_id: str) -> int:
        result = await conn.execute(
            """UPDATE pllm_tokens SET is_active = FALSE, revoked_at = NOW()
               WHERE issuer_id = $1 AND subject_id = $2 AND is_active = TRUE""",
            issuer_id, subject_id,
        )
        return int(result.split()[-1]) if result else 0

    @staticmethod
    async def query(
        conn: asyncpg.Connection,
        *,
        issuer_id: str | None = None,
        subject_id: str | None = None,
        pllm_token_id: uuid.UUID | None = None,
        page: int = 1,
        page_size: int = 20,
    ) -> tuple[list[dict], int]:
        where_parts: list[str] = []
        params: list[Any] = []
        idx = 1
        if issuer_id:
            where_parts.append(f"issuer_id = ${idx}")
            params.append(issuer_id)
            idx += 1
        if subject_id:
            where_parts.append(f"subject_id = ${idx}")
            params.append(subject_id)
            idx += 1
        if pllm_token_id:
            where_parts.append(f"pllm_token_id = ${idx}")
            params.append(pllm_token_id)
            idx += 1

        where = "WHERE " + " AND ".join(where_parts) if where_parts else ""

        total = await conn.fetchval(f"SELECT COUNT(*) FROM pllm_tokens {where}", *params)
        offset = (page - 1) * page_size
        rows = await conn.fetch(
            f"""SELECT * FROM pllm_tokens {where}
                ORDER BY created_at DESC
                LIMIT ${idx} OFFSET ${idx + 1}""",
            *params, page_size, offset,
        )
        return [dict(r) for r in rows], total or 0

    @staticmethod
    async def update_budget(
        conn: asyncpg.Connection,
        pllm_token_id: uuid.UUID,
        token_budget: int | None,
        token_budget_period: str | None,
    ) -> dict | None:
        return await conn.fetchrow(
            """UPDATE pllm_tokens
               SET token_budget = $2, token_budget_period = $3
               WHERE pllm_token_id = $1
               RETURNING *""",
            pllm_token_id, token_budget, token_budget_period,
        )

    @staticmethod
    async def update_rate(
        conn: asyncpg.Connection,
        pllm_token_id: uuid.UUID,
        rate_limit_rpm: int | None,
    ) -> dict | None:
        return await conn.fetchrow(
            """UPDATE pllm_tokens
               SET rate_limit_rpm = $2
               WHERE pllm_token_id = $1 AND is_active = TRUE
               RETURNING *""",
            pllm_token_id, rate_limit_rpm,
        )

    @staticmethod
    async def touch_last_used(conn: asyncpg.Connection, pllm_token_id: uuid.UUID) -> None:
        await conn.execute(
            "UPDATE pllm_tokens SET last_used_at = NOW() WHERE pllm_token_id = $1",
            pllm_token_id,
        )


class ModelRepo:
    """模型登记仓库"""

    @staticmethod
    async def register(conn: asyncpg.Connection, **kwargs) -> dict:
        row = await conn.fetchrow(
            """INSERT INTO models
               (model_name, tier, model_artifact, inference_engine, context_length,
                api_base, runtime_params, request_params, is_current, is_enabled)
               VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10)
               RETURNING *""",
            kwargs["model_name"], kwargs.get("tier", "medium"),
            kwargs["model_artifact"], kwargs["inference_engine"],
            kwargs["context_length"], kwargs.get("api_base"),
            kwargs.get("runtime_params"), kwargs.get("request_params"),
            kwargs.get("is_current", False), kwargs.get("is_enabled", True),
        )
        return dict(row)

    @staticmethod
    async def get_by_name_and_tier(
        conn: asyncpg.Connection, model_name: str, tier: str,
    ) -> dict | None:
        return await conn.fetchrow(
            "SELECT * FROM models WHERE model_name = $1 AND tier = $2",
            model_name, tier,
        )

    @staticmethod
    async def get_current(conn: asyncpg.Connection, model_name: str) -> dict | None:
        return await conn.fetchrow(
            """SELECT * FROM models
               WHERE model_name = $1 AND is_current = TRUE AND is_enabled = TRUE""",
            model_name,
        )

    @staticmethod
    async def get_current_model_name(conn: asyncpg.Connection) -> str | None:
        """返回全局当前模型名：models 表中 is_current 且 is_enabled 的行。
        若多个 model_name 各自持有 current tier，取最近更新（updated_at DESC）的一个。
        无 current 模型时返回 None（此时网关退回客户端 body.model）。"""
        return await conn.fetchval(
            """SELECT model_name FROM models
               WHERE is_current = TRUE AND is_enabled = TRUE
               ORDER BY updated_at DESC, id DESC LIMIT 1"""
        )

    @staticmethod
    async def list_all(
        conn: asyncpg.Connection,
        model_name: str | None = None,
        tier: str | None = None,
        is_current: bool | None = None,
    ) -> list[dict]:
        query = "SELECT * FROM models WHERE 1=1"
        params: list[Any] = []
        idx = 1
        if model_name:
            query += f" AND model_name = ${idx}"
            params.append(model_name)
            idx += 1
        if tier:
            query += f" AND tier = ${idx}"
            params.append(tier)
            idx += 1
        if is_current is not None:
            query += f" AND is_current = ${idx}"
            params.append(is_current)
            idx += 1
        query += " ORDER BY model_name, tier"
        rows = await conn.fetch(query, *params)
        return [dict(r) for r in rows]

    @staticmethod
    async def list_tiers(conn: asyncpg.Connection, model_name: str) -> list[dict]:
        rows = await conn.fetch(
            "SELECT * FROM models WHERE model_name = $1 ORDER BY tier",
            model_name,
        )
        return [dict(r) for r in rows]

    @staticmethod
    async def update_row(
        conn: asyncpg.Connection, model_name: str, tier: str, **kwargs
    ) -> dict | None:
        sets: list[str] = ["updated_at = NOW()"]
        params: list[Any] = [model_name, tier]
        idx = 3
        for key in (
            "api_base", "runtime_params", "request_params",
            "is_enabled", "sync_status", "context_length", "last_synced_at",
        ):
            if key in kwargs:
                sets.append(f"{key} = ${idx}")
                params.append(kwargs[key])
                idx += 1
        return await conn.fetchrow(
            f"""UPDATE models SET {', '.join(sets)}
                WHERE model_name = $1 AND tier = $2 RETURNING *""",
            *params,
        )

    @staticmethod
    async def delete_row(conn: asyncpg.Connection, model_name: str, tier: str) -> bool:
        result = await conn.execute(
            "DELETE FROM models WHERE model_name = $1 AND tier = $2",
            model_name, tier,
        )
        return "DELETE 1" in (result or "")

    # Aliases for API compatibility
    update = update_row
    delete = delete_row

    @staticmethod
    async def activate_tier(conn: asyncpg.Connection, model_name: str, tier: str) -> dict | None:
        async with conn.transaction():
            await conn.execute(
                "UPDATE models SET is_current = FALSE WHERE model_name = $1",
                model_name,
            )
            return await conn.fetchrow(
                """UPDATE models SET is_current = TRUE, updated_at = NOW()
                   WHERE model_name = $1 AND tier = $2 RETURNING *""",
                model_name, tier,
            )

    @staticmethod
    async def exists(conn: asyncpg.Connection, model_name: str, tier: str) -> bool:
        return await conn.fetchval(
            "SELECT 1 FROM models WHERE model_name = $1 AND tier = $2",
            model_name, tier,
        ) is not None


class UsageRepo:
    """使用量仓库"""

    @staticmethod
    async def record(conn: asyncpg.Connection, **kwargs) -> None:
        await conn.execute(
            """INSERT INTO usage_logs
               (request_id, pllm_token_ref_id, pllm_token_id_snapshot,
                issuer_id_snapshot, subject_id_snapshot,
                model, tier_snapshot, inference_engine_snapshot,
                prompt_tokens, completion_tokens, total_tokens,
                latency_ms, status_code, correlation_id)
               VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14)""",
            kwargs["request_id"], kwargs.get("pllm_token_ref_id"),
            kwargs["pllm_token_id_snapshot"],
            kwargs["issuer_id_snapshot"], kwargs.get("subject_id_snapshot"),
            kwargs["model"], kwargs.get("tier_snapshot"),
            kwargs.get("inference_engine_snapshot"),
            kwargs.get("prompt_tokens", 0), kwargs.get("completion_tokens", 0),
            kwargs.get("total_tokens", 0),
            kwargs["latency_ms"], kwargs["status_code"],
            kwargs.get("correlation_id"),
        )

    @staticmethod
    async def increment_counters(
        conn: asyncpg.Connection,
        pllm_token_id: uuid.UUID,
        total_tokens: int,
    ) -> None:
        """原子更新 daily/monthly/total 三个计数器"""
        now = _utcnow()
        day_start = now.date()
        month_start = now.replace(day=1).date()
        epoch = datetime(1970, 1, 1, tzinfo=timezone.utc).date()

        for period_type, period_start in [
            ("daily", day_start), ("monthly", month_start), ("total", epoch),
        ]:
            await conn.execute(
                """INSERT INTO usage_counters (pllm_token_id, period_type, period_start, used_tokens)
                   VALUES ($1, $2, $3, $4)
                   ON CONFLICT (pllm_token_id, period_type, period_start)
                   DO UPDATE SET used_tokens = usage_counters.used_tokens + $4,
                                 updated_at = NOW()""",
                pllm_token_id, period_type, period_start, total_tokens,
            )

    @staticmethod
    async def get_used_tokens(
        conn: asyncpg.Connection,
        pllm_token_id: uuid.UUID,
        period_type: str,
    ) -> int:
        now = _utcnow()
        if period_type == "daily":
            period_start = now.date()
        elif period_type == "monthly":
            period_start = now.replace(day=1).date()
        else:
            period_start = datetime(1970, 1, 1, tzinfo=timezone.utc).date()

        val = await conn.fetchval(
            """SELECT used_tokens FROM usage_counters
               WHERE pllm_token_id = $1 AND period_type = $2 AND period_start = $3""",
            pllm_token_id, period_type, period_start,
        )
        return val or 0

    @staticmethod
    async def get_usage_summary(
        conn: asyncpg.Connection,
        pllm_token_id: uuid.UUID,
        days: int,
    ) -> dict:
        since = _utcnow() - timedelta(days=days)
        row = await conn.fetchrow(
            """SELECT COUNT(*) as total_requests, COALESCE(SUM(total_tokens), 0) as total_tokens
               FROM usage_logs WHERE pllm_token_id_snapshot = $1 AND created_at >= $2""",
            pllm_token_id, since,
        )
        breakdown_rows = await conn.fetch(
            """SELECT DATE(created_at) as date, model,
                      COUNT(*) as requests, COALESCE(SUM(total_tokens), 0) as total_tokens
               FROM usage_logs
               WHERE pllm_token_id_snapshot = $1 AND created_at >= $2
               GROUP BY DATE(created_at), model ORDER BY date""",
            pllm_token_id, since,
        )
        return {
            "days": days,
            "total_requests": row["total_requests"] if row else 0,
            "total_tokens": row["total_tokens"] if row else 0,
            "breakdown": [dict(r) for r in breakdown_rows],
        }

    @staticmethod
    async def query_records(
        conn: asyncpg.Connection,
        *,
        page: int = 1,
        page_size: int = 20,
        **filters,
    ) -> tuple[list[dict], int]:
        where_parts: list[str] = []
        params: list[Any] = []
        idx = 1
        for key in ("request_id", "pllm_token_id_snapshot", "issuer_id_snapshot",
                     "subject_id_snapshot", "model", "tier_snapshot", "correlation_id"):
            if filters.get(key):
                where_parts.append(f"{key} = ${idx}")
                params.append(filters[key])
                idx += 1
        if filters.get("from_time"):
            where_parts.append(f"created_at >= ${idx}")
            params.append(filters["from_time"])
            idx += 1
        if filters.get("to_time"):
            where_parts.append(f"created_at < ${idx}")
            params.append(filters["to_time"])
            idx += 1

        where = "WHERE " + " AND ".join(where_parts) if where_parts else ""
        total = await conn.fetchval(f"SELECT COUNT(*) FROM usage_logs {where}", *params)
        offset = (page - 1) * page_size
        rows = await conn.fetch(
            f"""SELECT * FROM usage_logs {where}
                ORDER BY created_at DESC LIMIT ${idx} OFFSET ${idx+1}""",
            *params, page_size, offset,
        )
        return [dict(r) for r in rows], total or 0


class AuditRepo:
    """审计日志仓库"""

    @staticmethod
    async def record_security_event(conn: asyncpg.Connection, **kwargs) -> uuid.UUID:
        event_id = uuid.uuid4()
        detail = kwargs.get("detail")
        if detail is not None and not isinstance(detail, str):
            detail = json.dumps(detail, default=str)
        await conn.execute(
            """INSERT INTO security_event_logs
               (security_event_id, gateway_request_id, method, endpoint,
                decision, issuer_id_claim, key_id_claim, source_address,
                reason_code, response_status, detail)
               VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11)""",
            event_id, kwargs["gateway_request_id"],
            kwargs["method"], kwargs["endpoint"],
            kwargs["decision"], kwargs.get("issuer_id_claim"),
            kwargs.get("key_id_claim"), kwargs.get("source_address"),
            kwargs["reason_code"], kwargs.get("response_status"),
            detail,
        )
        return event_id

    @staticmethod
    async def record_event(conn: asyncpg.Connection, **kwargs) -> uuid.UUID:
        event_id = uuid.uuid4()
        detail = kwargs.get("detail")
        if detail is not None and not isinstance(detail, str):
            detail = json.dumps(detail, default=str)
        await conn.execute(
            """INSERT INTO event_logs
               (event_id, actor_type, actor_id, action,
                target_type, target_id, detail, result)
               VALUES ($1,$2,$3,$4,$5,$6,$7,$8)""",
            event_id, kwargs["actor_type"], kwargs.get("actor_id"),
            kwargs["action"], kwargs.get("target_type"),
            kwargs.get("target_id"), detail,
            kwargs["result"],
        )
        return event_id

    @staticmethod
    async def query_audit_events(
        conn: asyncpg.Connection,
        *,
        page: int = 1,
        page_size: int = 20,
        event_type: str | None = None,
        from_time: datetime | None = None,
        to_time: datetime | None = None,
        issuer_id: str | None = None,
        decision: str | None = None,
        result: str | None = None,
    ) -> tuple[list[dict], int]:
        """UNION ALL 查询两个审计表"""
        from_time = from_time or (_utcnow() - timedelta(hours=24))
        to_time = to_time or _utcnow()

        items: list[dict] = []

        if event_type in (None, "security_decision"):
            sec_where = ["occurred_at >= $1", "occurred_at < $2"]
            sec_params: list[Any] = [from_time, to_time]
            idx = 3
            if issuer_id:
                sec_where.append(f"issuer_id_claim = ${idx}")
                sec_params.append(issuer_id)
                idx += 1
            if decision:
                sec_where.append(f"decision = ${idx}")
                sec_params.append(decision)
                idx += 1
            rows = await conn.fetch(
                f"""SELECT * FROM security_event_logs
                    WHERE {' AND '.join(sec_where)}
                    ORDER BY occurred_at DESC LIMIT ${idx}""",
                *sec_params, page_size,
            )
            for r in rows:
                d = dict(r)
                items.append({
                    "audit_event_id": f"security:{d['security_event_id']}",
                    "event_type": "security_decision",
                    "source": "security_event_logs",
                    "occurred_at": d["occurred_at"],
                    "issuer_id": d.get("issuer_id_claim"),
                    "subject_id": None,
                    "gateway_request_id": d["gateway_request_id"],
                    "method": d["method"],
                    "endpoint": d["endpoint"],
                    "decision": d["decision"],
                    "reason_code": d["reason_code"],
                    "response_status": d.get("response_status"),
                    "action": None,
                    "target": None,
                    "result": None,
                    "detail": d.get("detail") or {},
                })

        if event_type in (None, "business_result"):
            biz_where = ["created_at >= $1", "created_at < $2"]
            biz_params: list[Any] = [from_time, to_time]
            idx = 3
            if issuer_id:
                biz_where.append(f"actor_id = ${idx}")
                biz_params.append(issuer_id)
                idx += 1
            if result:
                biz_where.append(f"result = ${idx}")
                biz_params.append(result)
                idx += 1
            rows = await conn.fetch(
                f"""SELECT * FROM event_logs
                    WHERE {' AND '.join(biz_where)}
                    ORDER BY created_at DESC LIMIT ${idx}""",
                *biz_params, page_size,
            )
            for r in rows:
                d = dict(r)
                items.append({
                    "audit_event_id": f"business:{d['event_id']}",
                    "event_type": "business_result",
                    "source": "event_logs",
                    "occurred_at": d["created_at"],
                    "issuer_id": d.get("actor_id"),
                    "subject_id": None,
                    "gateway_request_id": None,
                    "method": None,
                    "endpoint": None,
                    "decision": None,
                    "reason_code": None,
                    "response_status": None,
                    "action": d["action"],
                    "target": d.get("target_id"),
                    "result": d["result"],
                    "detail": d.get("detail") or {},
                })

        items.sort(key=lambda x: x["occurred_at"], reverse=True)
        total = len(items)
        offset = (page - 1) * page_size
        return items[offset:offset + page_size], total
