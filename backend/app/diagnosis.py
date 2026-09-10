from __future__ import annotations

import sqlite3
from datetime import date, timedelta
from decimal import Decimal

from backend.app.metrics import get_campaign_metrics


RULE_DETAILS = {
    "ANOM-NO-IMPRESSION-001": (
        "启用计划无曝光",
        ["计划尚未通过审核。", "预算或投放条件限制过严。", "计划实际投放条件可能不满足。"],
        ["检查审核状态。", "检查预算、日期和投放条件。", "核对计划状态是否一致。"],
        "RULE-DELIVERY-001",
    ),
    "ANOM-LOW-CTR-001": (
        "点击率偏低",
        ["广告素材吸引力不足。", "素材与目标人群不匹配。", "广告卖点表达不清晰。"],
        ["检查素材与目标人群的一致性。", "对比不同素材的点击表现。", "检查标题、主图和核心卖点。"],
        "RULE-CTR-001",
    ),
    "ANOM-NO-CONVERSION-001": (
        "有点击但无转化",
        ["落地页可能无法正常完成转化。", "广告内容与落地页可能不一致。", "转化回传配置可能异常。", "商品价格或转化路径可能存在问题。"],
        ["检查落地页可用性。", "核对广告素材和落地页内容。", "检查转化回传链路。", "人工检查商品状态和转化路径。"],
        "RULE-CVR-001",
    ),
    "ANOM-LOW-CVR-001": (
        "转化率偏低",
        ["落地页体验可能较差。", "广告承诺与实际商品可能不一致。", "商品价格、库存或购买路径可能存在问题。"],
        ["检查落地页加载和购买流程。", "核对广告信息与商品信息。", "对比不同日期或计划的 CVR。"],
        "RULE-CVR-001",
    ),
    "ANOM-HIGH-CPA-001": (
        "转化成本偏高",
        ["点击成本可能偏高。", "转化率可能偏低。", "当前转化量可能不足以摊薄消耗。"],
        ["同时检查 CPC 和 CVR。", "对比同广告主其他计划的 CPA。", "人工评估目标 CPA 是否符合业务目标。"],
        "RULE-CPA-001",
    ),
    "ANOM-LOW-ROAS-001": (
        "广告投产比偏低",
        ["转化量可能不足。", "单次转化收入可能较低。", "广告消耗增长可能快于转化收入。"],
        ["结合 CVR、CPA 和转化收入排查。", "对比同广告主其他计划的 ROAS。", "不要在缺少商品成本时直接判断真实利润。"],
        "RULE-ROAS-001",
    ),
    "ANOM-SPEND-CHANGE-001": (
        "单日消耗异常波动",
        ["预算或投放状态可能发生变化。", "流量环境可能发生变化。", "素材、定向或审核状态可能发生变化。"],
        ["对比计划状态和预算。", "检查曝光量、点击量是否同步变化。", "结合其他指标判断，不要只根据消耗下结论。"],
        "RULE-SPEND-001",
    ),
}


def _unique(values: list[str]) -> list[str]:
    return list(dict.fromkeys(values))


def diagnose_campaign(
    connection: sqlite3.Connection, campaign_code: str, start_date: str, end_date: str
) -> dict[str, object]:
    metrics = get_campaign_metrics(connection, campaign_code, start_date, end_date)
    campaign = metrics["campaign"]
    raw = metrics["raw"]
    impressions = raw["impressions"]
    clicks = raw["clicks"]
    conversions = raw["conversions"]
    spend = raw["spend_cents"]
    revenue = raw["revenue_cents"]
    ctr = Decimal(clicks) / Decimal(impressions) if impressions else None
    cvr = Decimal(conversions) / Decimal(clicks) if clicks else None
    cpa = Decimal(spend) / Decimal(conversions) if conversions else None
    roas = Decimal(revenue) / Decimal(spend) if spend else None

    anomalies: list[dict[str, object]] = []
    warnings: list[str] = []
    causes: list[str] = []
    suggestions: list[str] = []
    source_codes: list[str] = []

    def add(rule_id: str, severity: str, metric: str, actual: object, operator: str, threshold: object) -> None:
        name, rule_causes, rule_suggestions, source_code = RULE_DETAILS[rule_id]
        anomalies.append(
            {
                "rule_id": rule_id,
                "name": name,
                "severity": severity,
                "metric": metric,
                "actual": actual,
                "operator": operator,
                "threshold": threshold,
            }
        )
        causes.extend(rule_causes)
        suggestions.extend(rule_suggestions)
        source_codes.append(source_code)

    no_impression = campaign["status"] == "active" and impressions == 0
    if no_impression:
        add("ANOM-NO-IMPRESSION-001", "high", "impressions", 0, "=", 0)
    else:
        if impressions >= 1_000 and ctr is not None:
            if ctr < Decimal("0.005"):
                add("ANOM-LOW-CTR-001", "high", "ctr", float(ctr), "<", 0.005)
            elif ctr < Decimal("0.01"):
                add("ANOM-LOW-CTR-001", "medium", "ctr", float(ctr), "<", 0.01)
        elif impressions > 0:
            warnings.append("曝光量不足 1,000，暂不判断 CTR 异常。")

        no_conversion = clicks >= 100 and spend >= 30_000 and conversions == 0
        if no_conversion:
            add("ANOM-NO-CONVERSION-001", "high", "conversions", 0, "=", 0)
        elif clicks >= 100 and conversions > 0 and cvr is not None:
            if cvr < Decimal("0.005"):
                add("ANOM-LOW-CVR-001", "high", "cvr", float(cvr), "<", 0.005)
            elif cvr < Decimal("0.01"):
                add("ANOM-LOW-CVR-001", "medium", "cvr", float(cvr), "<", 0.01)
        elif clicks > 0 and conversions > 0:
            warnings.append("点击量不足 100，暂不判断 CVR 异常。")

        if conversions >= 5 and cpa is not None:
            if cpa > Decimal("12000"):
                add("ANOM-HIGH-CPA-001", "high", "cpa_cents", float(cpa), ">", 12_000)
            elif cpa > Decimal("8000"):
                add("ANOM-HIGH-CPA-001", "medium", "cpa_cents", float(cpa), ">", 8_000)
        elif conversions > 0:
            warnings.append("转化量不足 5，暂不判断 CPA 异常。")

    if spend >= 30_000 and roas is not None:
        if roas < Decimal("0.8"):
            add("ANOM-LOW-ROAS-001", "high", "roas", float(roas), "<", 0.8)
        elif roas < Decimal("1.2"):
            add("ANOM-LOW-ROAS-001", "medium", "roas", float(roas), "<", 1.2)
    elif spend > 0:
        warnings.append("广告消耗不足 300 元，暂不判断 ROAS 异常。")

    if start_date == end_date:
        analysis_date = date.fromisoformat(start_date)
        baseline_start = (analysis_date - timedelta(days=7)).isoformat()
        baseline_end = (analysis_date - timedelta(days=1)).isoformat()
        spends = [
            row[0]
            for row in connection.execute(
                "SELECT spend_cents FROM daily_metrics WHERE campaign_id = ? "
                "AND metric_date BETWEEN ? AND ? AND spend_cents > 0",
                (campaign["id"], baseline_start, baseline_end),
            )
        ]
        if len(spends) < 5:
            warnings.append("前 7 天有效数据不足 5 天，暂不判断消耗波动。")
        else:
            baseline = Decimal(sum(spends)) / Decimal(len(spends))
            if baseline < Decimal("10000"):
                warnings.append("前 7 天平均日消耗不足 100 元，暂不判断消耗波动。")
            else:
                spend_ratio = Decimal(spend) / baseline
                if spend_ratio > Decimal("1.5"):
                    add(
                        "ANOM-SPEND-CHANGE-001",
                        "medium",
                        "spend_change_ratio",
                        float(spend_ratio),
                        ">",
                        1.5,
                    )
                elif spend_ratio < Decimal("0.5"):
                    add(
                        "ANOM-SPEND-CHANGE-001",
                        "medium",
                        "spend_change_ratio",
                        float(spend_ratio),
                        "<",
                        0.5,
                    )

    sources = []
    for source_code in _unique(source_codes):
        source = connection.execute(
            "SELECT s.source_code, s.title, c.section_title FROM knowledge_sources s "
            "JOIN knowledge_chunks c ON c.source_id = s.id "
            "WHERE s.source_code = ? AND c.section_title = '排查步骤' LIMIT 1",
            (source_code,),
        ).fetchone()
        if source:
            sources.append(dict(source))

    facts = [
        f"计划 {campaign_code} 当前状态为 {campaign['status']}。",
        f"查询范围内获得 {impressions} 次曝光、{clicks} 次点击和 {conversions} 次转化。",
        f"查询范围内消耗 {spend / 100:.2f} 元，转化收入 {revenue / 100:.2f} 元。",
    ]
    return {
        "metrics": metrics,
        "diagnosis": {
            "facts": facts,
            "anomalies": anomalies,
            "sample_warnings": _unique(warnings),
            "possible_causes": _unique(causes),
            "suggestions": _unique(suggestions),
        },
        "sources": sources,
    }
