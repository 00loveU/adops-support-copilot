from __future__ import annotations

import csv
import unittest
from collections import Counter
from pathlib import Path


DATA_DIR = Path(__file__).resolve().parents[1] / "data"
ANCHOR_DATE = "2026-09-08"


def read_csv(name: str) -> list[dict[str, str]]:
    with (DATA_DIR / name).open(encoding="utf-8", newline="") as file:
        return list(csv.DictReader(file))


class MockDataTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.advertisers = read_csv("advertisers.csv")
        cls.campaigns = read_csv("campaigns.csv")
        cls.metrics = read_csv("daily_metrics.csv")
        cls.campaign_by_code = {row["campaign_code"]: row for row in cls.campaigns}
        cls.anchor_by_code = {
            campaign["campaign_code"]: metric
            for campaign in cls.campaigns
            for metric in cls.metrics
            if metric["campaign_id"] == campaign["id"] and metric["metric_date"] == ANCHOR_DATE
        }

    def test_shape_and_integrity(self) -> None:
        self.assertEqual(5, len(self.advertisers))
        self.assertEqual(15, len(self.campaigns))
        self.assertEqual(450, len(self.metrics))
        self.assertEqual(5, len({row["advertiser_code"] for row in self.advertisers}))
        self.assertEqual(15, len({row["campaign_code"] for row in self.campaigns}))

        advertiser_ids = {row["id"] for row in self.advertisers}
        campaign_ids = {row["id"] for row in self.campaigns}
        self.assertTrue(all(row["advertiser_id"] in advertiser_ids for row in self.campaigns))
        self.assertTrue(all(row["campaign_id"] in campaign_ids for row in self.metrics))
        self.assertEqual({30}, set(Counter(row["campaign_id"] for row in self.metrics).values()))
        self.assertEqual(
            450,
            len({(row["campaign_id"], row["metric_date"]) for row in self.metrics}),
        )

        for row in self.metrics:
            impressions, clicks, conversions, spend, revenue = map(
                int,
                (
                    row["impressions"],
                    row["clicks"],
                    row["conversions"],
                    row["spend_cents"],
                    row["revenue_cents"],
                ),
            )
            self.assertGreaterEqual(impressions, clicks)
            self.assertGreaterEqual(clicks, conversions)
            self.assertTrue(all(value >= 0 for value in (impressions, clicks, conversions, spend, revenue)))

    def test_anchor_metrics_and_rule_boundaries(self) -> None:
        def values(code: str) -> tuple[int, int, int, int, int]:
            row = self.anchor_by_code[code]
            return tuple(
                map(
                    int,
                    (
                        row["impressions"],
                        row["clicks"],
                        row["conversions"],
                        row["spend_cents"],
                        row["revenue_cents"],
                    ),
                )
            )

        self.assertEqual((20000, 300, 6, 30000, 60000), values("CMP001"))

        impressions, clicks, _, _, _ = values("CMP002")
        self.assertGreaterEqual(impressions, 1000)
        self.assertLess(clicks / impressions, 0.005)

        _, clicks, conversions, spend, revenue = values("CMP007")
        self.assertGreaterEqual(clicks, 100)
        self.assertGreaterEqual(spend, 30000)
        self.assertEqual(0, conversions)
        self.assertLess(revenue / spend, 0.8)

        _, clicks, conversions, spend, revenue = values("CMP013")
        self.assertEqual(0.005, conversions / clicks)
        self.assertGreater(spend / conversions, 12000)
        self.assertLess(revenue / spend, 0.8)

        impressions, _, _, spend, _ = values("CMP014")
        self.assertLess(impressions, 1000)
        self.assertLess(spend, 30000)

        self.assertEqual("paused", self.campaign_by_code["CMP015"]["status"])
        self.assertEqual((0, 0, 0, 0, 0), values("CMP015"))

    def test_spend_change_baselines(self) -> None:
        for code, anchor_spend, expected_ratio in (("CMP003", 32000, 1.6), ("CMP011", 8000, 0.4)):
            campaign_id = self.campaign_by_code[code]["id"]
            previous = [
                int(row["spend_cents"])
                for row in self.metrics
                if row["campaign_id"] == campaign_id and "2026-09-01" <= row["metric_date"] <= "2026-09-07"
            ]
            self.assertEqual(7, len(previous))
            average = sum(previous) / len(previous)
            self.assertEqual(20000, average)
            self.assertEqual(expected_ratio, anchor_spend / average)


if __name__ == "__main__":
    unittest.main()
