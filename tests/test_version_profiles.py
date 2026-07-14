from __future__ import annotations

import unittest
from datetime import date
from unittest.mock import patch

from re_file_hash_exporter.core.models import SuffixDiscoveryOptions
from re_file_hash_exporter.core.versions.profiles import (
    build_auto_detect_version_plan,
    build_profile_then_range_version_plan,
    is_today_date_end,
    profile_baseline_max_version,
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
        options = SuffixDiscoveryOptions(
            selected_extensions=["mesh"],
            mode="auto_detect",
            date_start="0",
            date_end="today",
        )

        with patch("re_file_hash_exporter.core.versions.profiles._local_today", return_value=date(2024, 1, 12)):
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
        options = SuffixDiscoveryOptions(
            selected_extensions=["mesh"],
            mode="auto_detect",
            date_start="0",
            date_end="today",
        )

        with patch("re_file_hash_exporter.core.versions.profiles._local_today", return_value=date(2024, 1, 5)):
            plan = build_auto_detect_version_plan("mesh", {}, options, {"mesh": profile})

        self.assertEqual(plan.low, 2401100)
        self.assertEqual(plan.high, 2401109)
        self.assertEqual(plan.count, 10)

    def test_legacy_date_range_accepts_today_date_end(self) -> None:
        profile = {
            "suffix_type": "date_code",
            "date_format": "YYMMDD",
            "tail_width": 1,
        }
        options = SuffixDiscoveryOptions(
            selected_extensions=["mesh"],
            mode="auto_detect",
            date_start="2024-01-10",
            date_end="today",
        )

        with patch("re_file_hash_exporter.core.versions.profiles._local_today", return_value=date(2024, 1, 12)):
            plan = build_auto_detect_version_plan("mesh", {}, options, {"mesh": profile})

        self.assertEqual(plan.low, 2401100)
        self.assertEqual(plan.high, 2401129)
        self.assertEqual(plan.count, 30)

    def test_today_date_end_token_is_case_insensitive(self) -> None:
        self.assertTrue(is_today_date_end(" Today "))
        self.assertFalse(is_today_date_end("7"))

    def test_profile_baseline_max_version_uses_numeric_priority_versions(self) -> None:
        profiles = {"tex": {"suffix_type": "numeric", "priority_versions": [2, 5, 3]}}

        self.assertEqual(profile_baseline_max_version("tex", profiles), 5)

    def test_profile_baseline_max_version_uses_date_code_profile_high(self) -> None:
        profiles = {
            "mesh": {
                "suffix_type": "date_code",
                "date_format": "YYMMDD",
                "tail_width": 2,
                "priority_dates": ["2024-01-10", "2024-01-12"],
            }
        }

        self.assertEqual(profile_baseline_max_version(".mesh", profiles), 24011299)


class ProfileThenRangeTests(unittest.TestCase):
    def test_numeric_known_versions_are_followed_by_new_range_and_min_is_ignored(self) -> None:
        options = SuffixDiscoveryOptions(
            selected_extensions=["rcol"],
            mode="profile_then_range",
            min_version=999,
            max_version=42,
        )
        profiles = {"rcol": {"suffix_type": "numeric", "priority_versions": [38, 2, 10, 38]}}

        plan = build_profile_then_range_version_plan(".rcol", options, profiles)

        self.assertEqual(list(plan.iter_values()), [2, 10, 38, 39, 40, 41, 42])
        self.assertEqual(plan.count, 7)
        self.assertEqual(plan.low, 2)
        self.assertEqual(plan.high, 42)

    def test_numeric_known_versions_are_kept_when_max_is_below_latest(self) -> None:
        options = SuffixDiscoveryOptions(
            selected_extensions=["rcol"],
            mode="profile_then_range",
            max_version=20,
        )
        profiles = {"rcol": {"suffix_type": "numeric", "priority_versions": [2, 10, 38]}}

        plan = build_profile_then_range_version_plan("rcol", options, profiles)

        self.assertEqual(list(plan.iter_values()), [2, 10, 38])

    def test_missing_numeric_profile_falls_back_to_zero_through_max(self) -> None:
        options = SuffixDiscoveryOptions(
            selected_extensions=["unknown"],
            mode="profile_then_range",
            min_version=99,
            max_version=3,
        )

        plan = build_profile_then_range_version_plan("unknown", options, {})

        self.assertEqual(list(plan.iter_values()), [0, 1, 2, 3])

    def test_date_known_combinations_are_followed_by_valid_calendar_values(self) -> None:
        options = SuffixDiscoveryOptions(
            selected_extensions=["mesh"],
            mode="profile_then_range",
            date_start="ignored",
            date_end="1",
        )
        profile = {
            "suffix_type": "date_code",
            "date_format": "YYMMDD",
            "tail_width": 1,
            "priority_dates": ["2024-01-30", "2024-01-31"],
            "priority_tails": [2, 8],
        }

        plan = build_profile_then_range_version_plan("mesh", options, {"mesh": profile})

        self.assertEqual(
            list(plan.iter_values()),
            [2401302, 2401308, 2401312, 2401318, 2401319, *range(2402010, 2402020)],
        )
        self.assertFalse(plan.contains(2401320))

    def test_date_end_today_uses_its_own_bound(self) -> None:
        options = SuffixDiscoveryOptions(
            selected_extensions=["mesh"],
            mode="profile_then_range",
            max_version=1,
            date_end="today",
        )
        profile = {
            "suffix_type": "date_code",
            "date_format": "YYMMDD",
            "tail_width": 1,
            "priority_dates": ["2024-01-10"],
            "priority_tails": [8],
        }

        with patch("re_file_hash_exporter.core.versions.profiles._local_today", return_value=date(2024, 1, 11)):
            plan = build_profile_then_range_version_plan("mesh", options, {"mesh": profile})

        self.assertEqual(list(plan.iter_values()), [2401108, 2401109, *range(2401110, 2401120)])


if __name__ == "__main__":
    unittest.main()
