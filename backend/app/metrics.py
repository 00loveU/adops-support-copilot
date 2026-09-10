from __future__ import annotations

import sqlite3
from datetime import date
from decimal import Decimal, ROUND_HALF_UP


SIX_PLACES = Decimal("0.000001")


def _validate_date_range(start_date: str, end_date: str) -> None:
    start = date.fromisoformat(start_date)
    end = date.fromisoformat(end_date)
    if start > end:
        raise ValueError("start_date 不能晚于 end_date")


def _ratio(numerator: int, denominator: int) -> float:
    value = Decimal(numerator) / Decimal(denominator)
    return float(value.quantize(SIX_PLACES, rounding=ROUND_HALF_UP))


def _money_per_count(amount_cents: int, count: int) -> int:
    value = Decimal(amount_cents) / Decimal(count)
    return int(value.quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def get_campaign_metrics(
    connection: sqlite3.Connection,
    campaign_code: str,
    start_date: str,
    end_date: str,
) -> dict[str, object]:
    _validate_date_range(start_date, end_date)
    campaign = connection.execute(
        "SELECT id, campaign_code, name, status FROM campaigns WHERE campaign_code = ?",
        (campaign_code,),
    ).fetchone()
    if campaign is None:
        raise LookupError(f"广告计划不存在: {campaign_code}")

    totals = connection.execute(
        "SELECT COALESCE(SUM(impressions), 0) AS impressions, "
        "COALESCE(SUM(clicks), 0) AS clicks, "
        "COALESCE(SUM(conversions), 0) AS conversions, "
        "COALESCE(SUM(spend_cents), 0) AS spend_cents, "
        "COALESCE(SUM(revenue_cents), 0) AS revenue_cents "
        "FROM daily_metrics WHERE campaign_id = ? AND metric_date BETWEEN ? AND ?",
        (campaign["id"], start_date, end_date),
    ).fetchone()
    raw = dict(totals)
    unavailable = []

    calculated: dict[str, float | int | None] = {
        "ctr": _ratio(raw["clicks"], raw["impressions"]) if raw["impressions"] else None,
        "cvr": _ratio(raw["conversions"], raw["clicks"]) if raw["clicks"] else None,
        "cpc_cents": _money_per_count(raw["spend_cents"], raw["clicks"])
        if raw["clicks"]
        else None,
        "cpa_cents": _money_per_count(raw["spend_cents"], raw["conversions"])
        if raw["conversions"]
        else None,
        "roas": _ratio(raw["revenue_cents"], raw["spend_cents"])
        if raw["spend_cents"]
        else None,
    }

    zero_denominators = (
        ("ctr", "曝光量", raw["impressions"], "CTR"),
        ("cvr", "点击量", raw["clicks"], "CVR"),
        ("cpc", "点击量", raw["clicks"], "CPC"),
        ("cpa", "转化量", raw["conversions"], "CPA"),
        ("roas", "广告消耗", raw["spend_cents"], "ROAS"),
    )
    for metric, denominator_name, denominator, label in zero_denominators:
        if denominator == 0:
            unavailable.append(
                {"metric": metric, "reason": f"{denominator_name}为 0，无法计算 {label}。"}
            )

    return {
        "campaign": dict(campaign),
        "date_range": {"start_date": start_date, "end_date": end_date},
        "raw": {**raw, "currency": "CNY"},
        "calculated": calculated,
        "unavailable_metrics": unavailable,
    }
