from __future__ import annotations

import unittest
from collections import Counter
from pathlib import Path

from re_file_hash_exporter.core.bruteforce import brute_force_suffixes
from re_file_hash_exporter.core.hash_utf16 import hash_mixed, hash_mixed_prepared_parts, prepare_mixed_text
from re_file_hash_exporter.core.models import BruteForceOptions, DmpScanResult
from re_file_hash_exporter.core.pak_hash import PakHashGroup, pak_group_identity
from re_file_hash_exporter.core.search.candidate_policy import candidate_count_for_entries
from re_file_hash_exporter.core.search.cpu_matcher import match_entries
from re_file_hash_exporter.core.search.gpu_batches import iter_prepared_gpu_batches
from re_file_hash_exporter.core.search.path_catalog import RawPathEntry, collect_raw_path_entries_by_extension


class SearchOptimizationTests(unittest.TestCase):
    def test_prepared_hash_matches_full_string_hash(self) -> None:
        path = "natives/STM/foo/bar.tex.123.STM"
        self.assertEqual(hash_mixed(path), hash_mixed_prepared_parts([prepare_mixed_text(path)]))

    def test_streaming_variants_use_path_evidence(self) -> None:
        scan = DmpScanResult(
            unversioned_paths={
                "tex": Counter(
                    {
                        "natives/STM/foo/plain.tex": 1,
                        "natives/STM/streaming/foo/streamed.tex": 1,
                    }
                )
            }
        )
        entries = collect_raw_path_entries_by_extension(scan, ["tex"])["tex"]
        flags = {entry.raw_path: (entry.seen_plain, entry.seen_streaming) for entry in entries}

        self.assertEqual(flags["foo/plain.tex"], (True, False))
        self.assertEqual(flags["foo/streamed.tex"], (False, True))
        self.assertEqual(
            candidate_count_for_entries(
                entries,
                "tex",
                version_count=1,
                include_platform_suffixes=False,
                language_mode="off",
                include_streaming=True,
                profiles={},
            ),
            2,
        )

    def test_cpu_matcher_reuses_prefix_state_and_finds_streaming_platform_match(self) -> None:
        entry = RawPathEntry("foo/bar.tex", seen_plain=True, seen_streaming=True)
        target = "natives/STM/streaming/foo/bar.tex.7.STM"
        result = match_entries(
            entries=[entry],
            extension="tex",
            versions=[7],
            pak_hashes={hash_mixed(target)},
            include_platform=True,
            language_mode="off",
            include_streaming=True,
            profiles={},
        )

        self.assertEqual(result.matches, [("foo/bar.tex", 7, target)])
        self.assertEqual(result.scanned_candidates, 6)

    def test_cpu_matcher_stops_after_first_match_for_version(self) -> None:
        first = "natives/STM/foo/first.rcol.27"
        second = "natives/STM/foo/second.rcol.27"
        result = match_entries(
            entries=[
                RawPathEntry("foo/first.rcol", seen_plain=True),
                RawPathEntry("foo/second.rcol", seen_plain=True),
            ],
            extension="rcol",
            versions=[27],
            pak_hashes={hash_mixed(first), hash_mixed(second)},
            include_platform=False,
            language_mode="off",
            include_streaming=False,
            profiles={},
        )

        self.assertEqual(result.matches, [("foo/first.rcol", 27, first)])
        self.assertEqual(result.scanned_candidates, 1)

    def test_gpu_prepared_batch_units_match_full_path_units(self) -> None:
        entry = RawPathEntry("foo/bar.tex", seen_plain=True, seen_streaming=False)
        batch = next(
            iter_prepared_gpu_batches(
                entries=[entry],
                extension="tex",
                versions=[7],
                include_platform_suffixes=True,
                language_mode="off",
                include_streaming=True,
                profiles={},
                batch_size=8,
                cancel_requested=None,
            )
        )
        platform_candidate = next(item for item in batch if item.full_path.endswith(".7.STM"))
        prepared = prepare_mixed_text(platform_candidate.full_path)

        self.assertEqual(platform_candidate.full_path, "natives/STM/foo/bar.tex.7.STM")
        self.assertEqual(platform_candidate.upper_units, prepared.upper_units)
        self.assertEqual(platform_candidate.lower_units, prepared.lower_units)

    def test_bruteforce_can_use_cached_pak_groups(self) -> None:
        target = "natives/STM/foo/a.tex.7"

        class Cache:
            def load_groups(self, pak_paths, workers=0, progress=None):
                return [PakHashGroup(Path("base.pak"), {hash_mixed(target)}, "base.pak", 0)]

        scan = DmpScanResult(unversioned_paths={"tex": Counter({"natives/STM/foo/a.tex": 1})})
        result = brute_force_suffixes(
            scan,
            [Path("base.pak")],
            {},
            BruteForceOptions(
                selected_extensions=["tex"],
                mode="custom",
                custom_versions="7",
                include_platform_suffixes=False,
                include_streaming=False,
                language_mode="off",
            ),
            pak_cache=Cache(),
        )

        self.assertFalse(result.cancelled)
        self.assertEqual([match.full_path for match in result.matches], [target])

    def test_subpak_identity_is_independent_from_main_pak(self) -> None:
        self.assertEqual(pak_group_identity(Path("re_chunk_000.pak")), ("re_chunk_000.pak", None))
        self.assertEqual(
            pak_group_identity(Path("re_chunk_000.pak.patch_001.pak")),
            ("re_chunk_000.pak", 1),
        )
        self.assertEqual(
            pak_group_identity(Path("re_chunk_000.pak.sub_000.pak")),
            ("re_chunk_000.pak.sub_000.pak", None),
        )
        self.assertEqual(
            pak_group_identity(Path("re_chunk_000.pak.sub_000.pak.patch_001.pak")),
            ("re_chunk_000.pak.sub_000.pak", 1),
        )

    def test_subpak_patch_lower_bound_does_not_inherit_main_pak_versions(self) -> None:
        high_main_version = "natives/STM/foo/a.tex.100"
        low_sub_patch_version = "natives/STM/foo/a.tex.50"

        class Cache:
            def load_groups(self, pak_paths, workers=0, progress=None):
                return [
                    PakHashGroup(
                        Path("re_chunk_000.pak"),
                        {hash_mixed(high_main_version)},
                        "re_chunk_000.pak",
                        0,
                    ),
                    PakHashGroup(
                        Path("re_chunk_000.pak.sub_000.pak"),
                        set(),
                        "re_chunk_000.pak.sub_000.pak",
                        1,
                    ),
                    PakHashGroup(
                        Path("re_chunk_000.pak.sub_000.pak.patch_001.pak"),
                        {hash_mixed(low_sub_patch_version)},
                        "re_chunk_000.pak.sub_000.pak",
                        2,
                        patch_index=1,
                    ),
                ]

        scan = DmpScanResult(unversioned_paths={"tex": Counter({"natives/STM/foo/a.tex": 1})})
        result = brute_force_suffixes(
            scan,
            [Path("unused.pak")],
            {},
            BruteForceOptions(
                selected_extensions=["tex"],
                mode="custom",
                custom_versions="50,100",
                processes=1,
                include_platform_suffixes=False,
                include_streaming=False,
                language_mode="off",
            ),
            pak_cache=Cache(),
        )

        self.assertFalse(result.cancelled)
        self.assertEqual(
            {match.full_path for match in result.matches},
            {high_main_version, low_sub_patch_version},
        )

    def test_bruteforce_skips_known_versions_and_reports_new_suffix_only(self) -> None:
        known = "natives/STM/foo/a.rcol.27"
        new = "natives/STM/foo/a.rcol.28"

        class Cache:
            def load_groups(self, pak_paths, workers=0, progress=None):
                return [PakHashGroup(Path("base.pak"), {hash_mixed(known), hash_mixed(new)}, "base.pak", 0)]

        scan = DmpScanResult(unversioned_paths={"rcol": Counter({"natives/STM/foo/a.rcol": 1})})
        result = brute_force_suffixes(
            scan,
            [Path("base.pak")],
            {"rcol": Counter({27: 1})},
            BruteForceOptions(
                selected_extensions=["rcol"],
                mode="custom",
                custom_versions="27-28",
                include_platform_suffixes=False,
                include_streaming=False,
                language_mode="off",
                processes=1,
            ),
            pak_cache=Cache(),
        )

        self.assertFalse(result.cancelled)
        self.assertEqual([match.version for match in result.matches], [28])
        self.assertEqual([match.full_path for match in result.matches], [new])


if __name__ == "__main__":
    unittest.main()
