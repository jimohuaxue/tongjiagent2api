import tempfile
import unittest
from pathlib import Path

from core.usage import UsageRecord, estimate_tokens, init_usage_schema, record_usage, usage_summary


class TestUsage(unittest.TestCase):
    def test_estimate_tokens_counts_mixed_text(self) -> None:
        self.assertGreaterEqual(estimate_tokens("hello 世界"), 3)

    def test_usage_summary_groups_models_and_buckets(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "usage.sqlite3"
            init_usage_schema(db_path)
            record_usage(
                UsageRecord(
                    provider="tongji",
                    protocol="openai",
                    model="qwen3-vl-235b",
                    status="ok",
                    started_at=1_700_000_000,
                    latency_ms=1200,
                    input_tokens=100,
                    output_tokens=20,
                ),
                db_path,
            )
            record_usage(
                UsageRecord(
                    provider="tongji",
                    protocol="openai",
                    model="glm-5.1",
                    status="error",
                    started_at=1_700_000_100,
                    latency_ms=3000,
                    input_tokens=50,
                    output_tokens=0,
                    error="empty response",
                ),
                db_path,
            )

            payload = usage_summary(days=1, granularity="hour", db_path=db_path, now=1_700_000_500)

        self.assertEqual(payload["totals"]["requests"], 2)
        self.assertEqual(payload["totals"]["errors"], 1)
        self.assertEqual(payload["totals"]["tokens"], 170)
        self.assertEqual(payload["models"][0]["model"], "qwen3-vl-235b")
        self.assertTrue(payload["buckets"])
        self.assertEqual(payload["recent"][0]["model"], "glm-5.1")

    def test_usage_summary_normalizes_known_tongji_aliases(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "usage.sqlite3"
            init_usage_schema(db_path)
            record_usage(
                UsageRecord(
                    provider="tongji",
                    protocol="openai",
                    model="gpt-5.5",
                    status="ok",
                    started_at=1_700_000_000,
                    latency_ms=1000,
                    input_tokens=10,
                    output_tokens=5,
                ),
                db_path,
            )

            payload = usage_summary(days=1, db_path=db_path, now=1_700_000_500)

        self.assertEqual(payload["models"], [
            {
                "model": "glm-5.1",
                "requests": 1,
                "tokens": 15,
                "input_tokens": 10,
                "output_tokens": 5,
                "avg_latency_seconds": 1.0,
            }
        ])
        self.assertEqual(payload["recent"][0]["model"], "glm-5.1")


if __name__ == "__main__":
    unittest.main()
