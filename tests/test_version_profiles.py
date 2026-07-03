from __future__ import annotations

import unittest
from datetime import date
from unittest.mock import patch

from re_file_hash_exporter.core.models import BruteForceOptions
from re_file_hash_exporter.core.version_profiles import (
    build_auto_detect_version_plan,
    is_today_date_end,
    plan_auto_detect_versions,
)


class DateCodeTodayTests(unittest.TestCase):
    def test_today_date_end_extends_plan_to_local_today(self) -> None:
        profile = {
            "suffix_type": "date_code",
            "date_format": "YYMMDD",
            "tail_width": 1,
            "priority_dates": ["2024-01-10"],
            "priority_tails": [2],
        }
        options = BruteForceOptions(
            selected_extensions=["mesh"],
            mode="auto_detect",
            date_start="0",
            date_end="today",
        )

        with patch("re_file_hash_exporter.core.version_profiles._local_today", return_value=date(2024, 1, 12)):
            plan = build_auto_detect_version_plan("mesh", {}, options, {"mesh": profile})

        self.assertEqual(plan.low, 2401100)
        self.assertEqual(plan.high, 2401129)
        self.assertEqual(plan.count, 30)
        self.assertTrue(plan.contains(2401129))
        self.assertFalse(plan.contains(2401130))

    def test_today_date_end_does_not_shrink_priority_date_range(self) -> None:
        profile = {
            "suffix_type": "date_code",
            "date_format": "YYMMDD",
            "tail_width": 1,
            "priority_dates": ["2024-01-10"],
        }
        options = BruteForceOptions(
            selected_extensions=["mesh"],
            mode="auto_detect",
            date_start="0",
            date_end="today",
        )

        with patch("re_file_hash_exporter.core.version_profiles._local_today", return_value=date(2024, 1, 5)):
            plan = build_auto_detect_version_plan("mesh", {}, options, {"mesh": profile})

        self.assertEqual(plan.low, 2401100)
        self.assertEqual(plan.high, 2401109)
        self.assertEqual(plan.count, 10)

    def test_legacy_list_planner_accepts_today_date_end(self) -> None:
        profile = {
            "suffix_type": "date_code",
            "date_format": "YYMMDD",
            "tail_width": 1,
            "priority_dates": ["2024-01-10"],
        }
        options = BruteForceOptions(
            selected_extensions=["mesh"],
            mode="auto_detect",
            date_start="0",
            date_end="today",
        )

        with patch("re_file_hash_exporter.core.version_profiles._local_today", return_value=date(2024, 1, 12)):
            versions = plan_auto_detect_versions("mesh", {}, options, {"mesh": profile})

        self.assertEqual(len(versions), 30)
        self.assertIn(2401129, versions)
        self.assertNotIn(2401130, versions)

    def test_legacy_date_range_accepts_today_date_end(self) -> None:
        profile = {
            "suffix_type": "date_code",
            "date_format": "YYMMDD",
            "tail_width": 1,
        }
        options = BruteForceOptions(
            selected_extensions=["mesh"],
            mode="auto_detect",
            date_start="2024-01-10",
            date_end="today",
        )

        with patch("re_file_hash_exporter.core.version_profiles._local_today", return_value=date(2024, 1, 12)):
            plan = build_auto_detect_version_plan("mesh", {}, options, {"mesh": profile})

        self.assertEqual(plan.low, 2401100)
        self.assertEqual(plan.high, 2401129)
        self.assertEqual(plan.count, 30)

    def test_today_date_end_token_is_case_insensitive(self) -> None:
        self.assertTrue(is_today_date_end(" Today "))
        self.assertFalse(is_today_date_end("7"))


if __name__ == "__main__":
    unittest.main()
