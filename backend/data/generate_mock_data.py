from __future__ import annotations

import csv
import random
from datetime import date, datetime, timedelta, timezone
from pathlib import Path


DATA_DIR = Path(__file__).resolve().parent
START_DATE = date(2026, 8, 10)
END_DATE = date(2026, 9, 8)
CREATED_AT = datetime(2026, 9, 9, tzinfo=timezone.utc).isoformat().replace("+00:00", "Z")
RANDOM_SEED = 20260909


ADVERTISERS = [
    (1, "ADV001", "星河食品旗舰店", "食品", "active"),
    (2, "ADV002", "云杉美妆旗舰店", "美妆", "active"),
    (3, "ADV003", "星芽母婴生活馆", "母婴", "active"),
    (4, "ADV004", "青禾服饰旗舰店", "服饰", "active"),
    (5, "ADV005", "简筑家居生活馆", "家居", "active"),
]

CAMPAIGNS = [
    (1, "CMP001", 1, "早餐麦片日常推广", "active", 50000),
    (2, "CMP002", 1, "低糖饼干素材测试", "active", 50000),
    (3, "CMP003", 1, "坚果礼盒限时推广", "active", 60000),
    (4, "CMP004", 2, "补水面膜人群测试", "active", 50000),
    (5, "CMP005", 2, "精华液新品推广", "active", 100000),
    (6, "CMP006", 2, "防晒霜日常推广", "active", 60000),
    (7, "CMP007", 3, "婴儿湿巾拉新计划", "active", 80000),
    (8, "CMP008", 3, "儿童洗护推广", "active", 80000),
    (9, "CMP009", 3, "纸尿裤日常推广", "active", 60000),
    (10, "CMP010", 4, "秋季新品启动计划", "active", 50000),
    (11, "CMP011", 4, "通勤女装稳定投放", "active", 50000),
    (12, "CMP012", 4, "基础款男装推广", "active", 50000),
    (13, "CMP013", 5, "人体工学椅推广", "active", 120000),
    (14, "CMP014", 5, "香薰新品小流量测试", "active", 10000),
    (15, "CMP015", 5, "收纳用品暂停计划", "paused", 30000),
]

# 2026-09-08 的固定验收数据。金额单位为分。
ANCHOR_METRICS = {
    "CMP001": (20000, 300, 6, 30000, 60000),
    "CMP002": (20000, 80, 4, 16000, 40000),
    "CMP003": (16000, 240, 8, 32000, 64000),
    "CMP004": (30000, 300, 1, 20000, 30000),
    "CMP005": (18000, 250, 5, 65000, 100000),
    "CMP006": (24000, 360, 9, 36000, 75600),
    "CMP007": (10000, 150, 0, 40000, 0),
    "CMP008": (25000, 500, 10, 50000, 35000),
    "CMP009": (22000, 330, 8, 32000, 64000),
    "CMP010": (0, 0, 0, 0, 0),
    "CMP011": (6000, 90, 3, 8000, 18000),
    "CMP012": (15000, 225, 6, 24000, 48000),
    "CMP013": (50000, 1000, 5, 70000, 35000),
    "CMP014": (500, 1, 0, 200, 0),
    "CMP015": (0, 0, 0, 0, 0),
}


def write_csv(path: Path, fieldnames: list[str], rows: list[dict[str, object]]) -> None:
    with path.open("w", encoding="utf-8", newline="") as file:
        writer = csv.DictWriter(file, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def build_daily_metrics() -> list[dict[str, object]]:
    rng = random.Random(RANDOM_SEED)
    rows: list[dict[str, object]] = []
    row_id = 1

    for campaign_id, campaign_code, _, _, status, _ in CAMPAIGNS:
        current = START_DATE
        while current <= END_DATE:
            if status == "paused":
                metrics = (0, 0, 0, 0, 0)
            else:
                impressions = rng.randint(12000, 22000)
                clicks = round(impressions * rng.uniform(0.014, 0.019))
                conversions = max(1, round(clicks * rng.uniform(0.02, 0.035)))
                spend_cents = clicks * rng.randint(80, 120)
                revenue_cents = spend_cents * 2
                metrics = (impressions, clicks, conversions, spend_cents, revenue_cents)

            # 固定验收日前 7 天基准，避免非波动样例意外命中消耗波动规则。
            if date(2026, 9, 1) <= current <= date(2026, 9, 7):
                impressions, clicks, conversions, _, _ = metrics
                baseline_spend = (
                    20000
                    if campaign_code in {"CMP003", "CMP011"}
                    else ANCHOR_METRICS[campaign_code][3]
                )
                metrics = (
                    impressions,
                    clicks,
                    conversions,
                    baseline_spend,
                    baseline_spend * 2,
                )

            if current == END_DATE:
                metrics = ANCHOR_METRICS[campaign_code]

            impressions, clicks, conversions, spend_cents, revenue_cents = metrics
            rows.append(
                {
                    "id": row_id,
                    "campaign_id": campaign_id,
                    "metric_date": current.isoformat(),
                    "impressions": impressions,
                    "clicks": clicks,
                    "conversions": conversions,
                    "spend_cents": spend_cents,
                    "revenue_cents": revenue_cents,
                }
            )
            row_id += 1
            current += timedelta(days=1)

    return rows


def main() -> None:
    advertiser_rows = [
        {
            "id": item[0],
            "advertiser_code": item[1],
            "name": item[2],
            "industry": item[3],
            "status": item[4],
            "created_at": CREATED_AT,
        }
        for item in ADVERTISERS
    ]
    campaign_rows = [
        {
            "id": item[0],
            "campaign_code": item[1],
            "advertiser_id": item[2],
            "name": item[3],
            "status": item[4],
            "daily_budget_cents": item[5],
            "start_date": "2026-08-01",
            "end_date": "",
            "created_at": CREATED_AT,
        }
        for item in CAMPAIGNS
    ]

    write_csv(
        DATA_DIR / "advertisers.csv",
        ["id", "advertiser_code", "name", "industry", "status", "created_at"],
        advertiser_rows,
    )
    write_csv(
        DATA_DIR / "campaigns.csv",
        [
            "id",
            "campaign_code",
            "advertiser_id",
            "name",
            "status",
            "daily_budget_cents",
            "start_date",
            "end_date",
            "created_at",
        ],
        campaign_rows,
    )
    write_csv(
        DATA_DIR / "daily_metrics.csv",
        [
            "id",
            "campaign_id",
            "metric_date",
            "impressions",
            "clicks",
            "conversions",
            "spend_cents",
            "revenue_cents",
        ],
        build_daily_metrics(),
    )


if __name__ == "__main__":
    main()
