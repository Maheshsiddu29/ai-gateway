"""Authenticated, bounded cost and budget reads."""

from datetime import UTC, datetime, timedelta
from typing import Any

from fastapi import APIRouter, Query, Request
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError

from gateway.api.dependencies import PrincipalDep
from gateway.cost.config import CostConfig
from gateway.cost.store import CostStore, period_start
from gateway.errors import AdmissionUnavailableError, InvalidRequestError

router = APIRouter(prefix="/v1")


def _scope(request: Request, principal_id: str, team_id: str | None) -> tuple[CostStore, str, str]:
    store: CostStore | None = request.app.state.cost_store
    if store is None:
        raise AdmissionUnavailableError("Cost accounting is disabled.")
    config: CostConfig = request.app.state.cost_config
    if team_id is None:
        return store, "key", principal_id
    if principal_id not in config.team_readers.get(team_id, []):
        raise InvalidRequestError(
            "Team spending access is not authorized.", status_code=403, code="team_usage_forbidden"
        )
    return store, "team", team_id


def _range(start: datetime | None, end: datetime | None) -> tuple[datetime, datetime]:
    end = end or datetime.now(UTC)
    start = start or end - timedelta(days=7)
    if start.tzinfo is None or end.tzinfo is None:
        raise InvalidRequestError("Time range must include a timezone.", param="start")
    if end <= start or end - start > timedelta(days=31):
        raise InvalidRequestError("Time range must be positive and at most 31 days.", param="start")
    return start.astimezone(UTC), end.astimezone(UTC)


async def _rows(store: CostStore, sql: str, params: dict[str, Any]) -> list[dict[str, Any]]:
    try:
        async with store.engine.connect() as connection:
            result = await connection.execute(text(sql), params)
            return [dict(row) for row in result.mappings()]
    except SQLAlchemyError as exc:
        raise AdmissionUnavailableError("Cost analytics is unavailable.") from exc


def _params(
    scope: str,
    subject: str,
    start: datetime,
    end: datetime,
    provider: str | None,
    model: str | None,
) -> dict[str, Any]:
    return {
        "scope": scope,
        "subject": subject,
        "start": start,
        "end": end,
        "provider": provider,
        "model": model,
    }


@router.get("/usage")
async def usage(
    request: Request,
    principal: PrincipalDep,
    start: datetime | None = None,
    end: datetime | None = None,
    provider: str | None = None,
    model: str | None = None,
    team_id: str | None = None,
    limit: int = Query(default=100, ge=1, le=100),
    offset: int = Query(default=0, ge=0, le=10000),
) -> dict[str, Any]:
    store, scope, subject = _scope(request, principal.id, team_id)
    start, end = _range(start, end)
    sql = """SELECT attempt_id, request_id, provider, model, pricing_version,
        created_at, state, classification, input_tokens, output_tokens,
        estimated_input_tokens, maximum_output_tokens, reserved_micro_usd,
        charge_micro_usd FROM cost_attempts
        WHERE ((:scope='key' AND principal_id=:subject)
        OR (:scope='team' AND team_id=:subject))
        AND created_at>=:start AND created_at<:end
        AND (CAST(:provider AS text) IS NULL OR provider=CAST(:provider AS text))
        AND (CAST(:model AS text) IS NULL OR model=CAST(:model AS text))
        ORDER BY created_at DESC, attempt_id DESC LIMIT :limit OFFSET :offset"""
    rows = await _rows(
        store,
        sql,
        {
            **_params(scope, subject, start, end, provider, model),
            "limit": limit,
            "offset": offset,
        },
    )
    for row in rows:
        row["attempt_id"] = str(row["attempt_id"])
        row["request_id"] = str(row["request_id"])
        row["created_at"] = row["created_at"].isoformat()
    return {"items": rows, "limit": limit, "offset": offset}


@router.get("/usage/summary")
async def usage_summary(
    request: Request,
    principal: PrincipalDep,
    start: datetime | None = None,
    end: datetime | None = None,
    provider: str | None = None,
    model: str | None = None,
    team_id: str | None = None,
) -> dict[str, Any]:
    store, scope, subject = _scope(request, principal.id, team_id)
    start, end = _range(start, end)
    sql = """SELECT provider, model,
        COUNT(DISTINCT request_id) AS request_count, COUNT(*) AS attempt_count,
        COALESCE(SUM(input_tokens), 0) AS input_tokens,
        COALESCE(SUM(output_tokens), 0) AS output_tokens,
        COALESCE(SUM(reserved_micro_usd) FILTER
            (WHERE state IN ('reserved','dispatched')), 0) AS reserved_micro_usd,
        COALESCE(SUM(charge_micro_usd) FILTER
            (WHERE state='settled'), 0) AS settled_micro_usd,
        COALESCE(SUM(charge_micro_usd) FILTER
            (WHERE state='unresolved'), 0) AS unresolved_micro_usd
        FROM cost_attempts
        WHERE ((:scope='key' AND principal_id=:subject)
        OR (:scope='team' AND team_id=:subject))
        AND created_at>=:start AND created_at<:end
        AND (CAST(:provider AS text) IS NULL OR provider=CAST(:provider AS text))
        AND (CAST(:model AS text) IS NULL OR model=CAST(:model AS text))
        GROUP BY provider, model ORDER BY provider, model"""
    rows = await _rows(store, sql, _params(scope, subject, start, end, provider, model))
    for row in rows:
        for key, value in row.items():
            if key not in ("provider", "model"):
                row[key] = int(value)
    return {"start": start.isoformat(), "end": end.isoformat(), "by_provider_model": rows}


@router.get("/budgets")
async def budgets(
    request: Request,
    principal: PrincipalDep,
    team_id: str | None = None,
) -> dict[str, Any]:
    store, scope, subject = _scope(request, principal.id, team_id)
    config: CostConfig = request.app.state.cost_config
    limits = (
        config.team_limits[subject]
        if scope == "team"
        else config.key_limits.get(subject, config.default_key_limits)
    )
    now = datetime.now(UTC)
    items: list[dict[str, Any]] = []
    for kind, limit in (("daily", limits.daily_micro_usd), ("monthly", limits.monthly_micro_usd)):
        if limit is None:
            continue
        start = period_start(now, kind)
        rows = await _rows(
            store,
            """SELECT reserved_micro_usd, spent_micro_usd
            FROM budget_periods WHERE scope=:scope AND subject_id=:subject
            AND period_kind=:kind AND period_start=:start""",
            {
                "scope": scope,
                "subject": subject,
                "kind": kind,
                "start": start,
            },
        )
        reserved = rows[0]["reserved_micro_usd"] if rows else 0
        spent = rows[0]["spent_micro_usd"] if rows else 0
        items.append(
            {
                "period": kind,
                "period_start": start.isoformat(),
                "limit_micro_usd": limit,
                "reserved_micro_usd": reserved,
                "spent_micro_usd": spent,
                "available_micro_usd": max(0, limit - reserved - spent),
                "debt_micro_usd": max(0, reserved + spent - limit),
            }
        )
    return {"scope": scope, "subject_id": subject, "items": items}
