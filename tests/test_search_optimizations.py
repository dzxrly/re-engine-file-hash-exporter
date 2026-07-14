from __future__ import annotations

import inspect
import unittest
from collections import Counter
from pathlib import Path
from unittest.mock import patch

from re_file_hash_exporter.core.hashing.utf16 import hash_mixed, hash_mixed_prepared_parts, prepare_mixed_text
from re_file_hash_exporter.core.models import SuffixDiscoveryMatch, SuffixDiscoveryOptions, DmpScanResult
from re_file_hash_exporter.core.pak.reader import PakHashGroup, pak_group_identity
from re_file_hash_exporter.core.search.candidate_policy import candidate_count_for_entries, iter_candidate_bases
from re_file_hash_exporter.core.search.cpu_matcher import match_entries
from re_file_hash_exporter.core.search.gpu_batches import iter_prepared_gpu_batches, prepare_gpu_bases
from re_file_hash_exporter.core.search.gpu_batches import iter_prepared_gpu_suffix_batches
from re_file_hash_exporter.core.search.gpu_pool import (
    MultiGpuSearchOutcome,
    _SharedHashView,
    _create_shared_hash_data,
    _cuda_owner_devices,
    _dynamic_version_chunk_size,
    _gpu_process_context,
    _init_gpu_worker,
)
from re_file_hash_exporter.core.search.path_catalog import RawPathEntry, collect_raw_path_entries_by_extension
from re_file_hash_exporter.core.suffix_discovery.engine import discover_suffixes
from re_file_hash_exporter.core.versions.plan import numeric_range_plan


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

    def test_language_suffix_search_uses_probe_languages_only(self) -> None:
        entry = RawPathEntry("message/foo.msg", seen_plain=True)
        bases = list(
            iter_candidate_bases(
                entry,
                "msg",
                include_platform_suffixes=False,
                language_mode="all",
                include_streaming=False,
                profiles={},
            )
        )

        self.assertEqual([base.language_suffixes for base in bases], [("Ja", "En")])
        self.assertEqual(
            candidate_count_for_entries(
                [entry],
                "msg",
                version_count=1,
                include_platform_suffixes=False,
                language_mode="all",
                include_streaming=False,
                profiles={},
            ),
            3,
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
        bases = prepare_gpu_bases(
            entries=[entry],
            extension="tex",
            include_platform_suffixes=True,
            language_mode="off",
            include_streaming=True,
            profiles={},
        )
        batch = next(
            iter_prepared_gpu_batches(
                bases=bases,
                versions=[7],
                batch_size=8,
                cancel_requested=None,
            )
        )
        platform_index = next(
            index
            for index in range(len(batch))
            if batch.full_path_at(index, bases).endswith(".7.STM")
        )
        prepared = prepare_mixed_text(batch.full_path_at(platform_index, bases))

        self.assertEqual(batch.full_path_at(platform_index, bases), "natives/STM/foo/bar.tex.7.STM")
        offset = batch.offsets[platform_index]
        length = batch.lengths[platform_index]
        self.assertEqual(tuple(batch.upper_units[offset : offset + length]), prepared.upper_units)
        self.assertEqual(tuple(batch.lower_units[offset : offset + length]), prepared.lower_units)

    def test_incremental_gpu_hash_matches_full_path_hash_when_torch_cuda_available(self) -> None:
        try:
            import torch
        except Exception as err:
            self.skipTest(f"torch is not available: {err}")
        if not torch.cuda.is_available():
            self.skipTest("CUDA is not available")

        from re_file_hash_exporter.core.gpu.torch_backend import (
            hash_incremental_prepared_mixed_batch,
            match_incremental_prepared_mixed_batch,
            prepare_pak_hash_key_tensor,
        )

        bases = prepare_gpu_bases(
            entries=[
                RawPathEntry("a/b.tex", seen_plain=True, seen_streaming=False),
                RawPathEntry("aa/b.tex", seen_plain=True, seen_streaming=False),
            ],
            extension="tex",
            include_platform_suffixes=True,
            language_mode="off",
            include_streaming=False,
            profiles={},
        )
        self.assertTrue(any(base.base_unit_count % 2 for base in bases))
        batch = next(
            iter_prepared_gpu_suffix_batches(
                bases=bases,
                versions=[7, 260101100],
                batch_size=64,
                cancel_requested=None,
            )
        )

        hashes = hash_incremental_prepared_mixed_batch(batch, bases, device="cuda")
        expected = [hash_mixed(batch.full_path_at(index, bases)) for index in range(len(batch))]

        self.assertEqual(hashes, expected)

        pak_hash_keys = prepare_pak_hash_key_tensor(torch, {expected[1], expected[-1], 0xFFFF_FFFF_FFFF_FFFF}, "cuda")
        matched_indexes = match_incremental_prepared_mixed_batch(batch, bases, pak_hash_keys, device="cuda")

        self.assertEqual(set(matched_indexes), {1, len(batch) - 1})

    def test_suffix_discovery_can_use_cached_pak_groups(self) -> None:
        target = "natives/STM/foo/a.tex.7"

        class Cache:
            def load_groups(self, pak_paths, workers=0, progress=None):
                return [PakHashGroup(Path("base.pak"), {hash_mixed(target)}, "base.pak", 0)]

        scan = DmpScanResult(unversioned_paths={"tex": Counter({"natives/STM/foo/a.tex": 1})})
        result = discover_suffixes(
            scan,
            [Path("base.pak")],
            {},
            SuffixDiscoveryOptions(
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
        result = discover_suffixes(
            scan,
            [Path("unused.pak")],
            {},
            SuffixDiscoveryOptions(
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

    def test_patch_only_uses_profile_baseline_as_incremental_lower_bound(self) -> None:
        old_version = "natives/STM/foo/a.tex.5"
        new_version = "natives/STM/foo/a.tex.6"
        messages: list[str] = []

        class Cache:
            def load_groups(self, pak_paths, workers=0, progress=None):
                return [
                    PakHashGroup(
                        Path("re_chunk_000.pak.patch_001.pak"),
                        {hash_mixed(old_version), hash_mixed(new_version)},
                        "re_chunk_000.pak",
                        0,
                        patch_index=1,
                    )
                ]

        scan = DmpScanResult(unversioned_paths={"tex": Counter({"natives/STM/foo/a.tex": 1})})
        with patch(
            "re_file_hash_exporter.core.suffix_discovery.engine.load_version_profiles",
            return_value={"tex": {"suffix_type": "numeric", "priority_versions": [5]}},
        ):
            result = discover_suffixes(
                scan,
                [Path("unused.pak")],
                {},
                SuffixDiscoveryOptions(
                    selected_extensions=["tex"],
                    mode="auto_detect",
                    min_version=0,
                    max_version=1,
                    processes=1,
                    include_platform_suffixes=False,
                    include_streaming=False,
                    language_mode="off",
                ),
                progress=messages.append,
                pak_cache=Cache(),
            )

        self.assertFalse(result.cancelled)
        self.assertEqual([match.full_path for match in result.matches], [new_version])
        text_messages = [message for message in messages if isinstance(message, str)]
        self.assertTrue(any("file_suffix_profiles.json baseline (.tex>5)" in message for message in text_messages))
        self.assertTrue(any(".tex: incremental lower bound > 5." in message for message in text_messages))

    def test_profile_then_range_searches_known_versions_and_new_tail_but_not_old_gaps(self) -> None:
        known_version = "natives/STM/foo/a.rcol.2"
        old_gap = "natives/STM/foo/a.rcol.5"
        new_tail = "natives/STM/foo/a.rcol.11"

        class Cache:
            def load_groups(self, pak_paths, workers=0, progress=None):
                return [
                    PakHashGroup(
                        Path("base.pak"),
                        {hash_mixed(known_version), hash_mixed(old_gap), hash_mixed(new_tail)},
                        "base.pak",
                        0,
                    )
                ]

        scan = DmpScanResult(unversioned_paths={"rcol": Counter({"natives/STM/foo/a.rcol": 1})})
        with patch(
            "re_file_hash_exporter.core.suffix_discovery.engine.load_version_profiles",
            return_value={"rcol": {"suffix_type": "numeric", "priority_versions": [2, 10]}},
        ):
            result = discover_suffixes(
                scan,
                [Path("unused.pak")],
                {},
                SuffixDiscoveryOptions(
                    selected_extensions=["rcol"],
                    mode="profile_then_range",
                    min_version=9,
                    max_version=11,
                    processes=1,
                    include_platform_suffixes=False,
                    include_streaming=False,
                    language_mode="off",
                ),
                pak_cache=Cache(),
            )

        self.assertFalse(result.cancelled)
        self.assertEqual([match.version for match in result.matches], [2, 11])
        self.assertNotIn(old_gap, {match.full_path for match in result.matches})

    def test_patch_only_profile_then_range_scans_known_profile_versions_first(self) -> None:
        known_version = "natives/STM/foo/a.tex.5"
        new_version = "natives/STM/foo/a.tex.6"

        class Cache:
            def load_groups(self, pak_paths, workers=0, progress=None):
                return [
                    PakHashGroup(
                        Path("re_chunk_000.pak.patch_001.pak"),
                        {hash_mixed(known_version), hash_mixed(new_version)},
                        "re_chunk_000.pak",
                        0,
                        patch_index=1,
                    )
                ]

        scan = DmpScanResult(unversioned_paths={"tex": Counter({"natives/STM/foo/a.tex": 1})})
        with patch(
            "re_file_hash_exporter.core.suffix_discovery.engine.load_version_profiles",
            return_value={"tex": {"suffix_type": "numeric", "priority_versions": [5]}},
        ):
            result = discover_suffixes(
                scan,
                [Path("unused.pak")],
                {},
                SuffixDiscoveryOptions(
                    selected_extensions=["tex"],
                    mode="profile_then_range",
                    max_version=6,
                    processes=1,
                    include_platform_suffixes=False,
                    include_streaming=False,
                    language_mode="off",
                ),
                pak_cache=Cache(),
            )

        self.assertFalse(result.cancelled)
        self.assertEqual([match.version for match in result.matches], [5, 6])

    def test_suffix_discovery_rechecks_known_versions_as_family_baseline_evidence(self) -> None:
        known = "natives/STM/foo/a.rcol.27"
        new = "natives/STM/foo/a.rcol.28"

        class Cache:
            def load_groups(self, pak_paths, workers=0, progress=None):
                return [PakHashGroup(Path("base.pak"), {hash_mixed(known), hash_mixed(new)}, "base.pak", 0)]

        scan = DmpScanResult(unversioned_paths={"rcol": Counter({"natives/STM/foo/a.rcol": 1})})
        result = discover_suffixes(
            scan,
            [Path("base.pak")],
            {"rcol": Counter({27: 1})},
            SuffixDiscoveryOptions(
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
        self.assertEqual([match.version for match in result.matches], [27, 28])
        self.assertEqual([match.full_path for match in result.matches], [known, new])

    def test_patch_with_no_new_candidates_after_family_hit_does_not_crash(self) -> None:
        target = "natives/STM/foo/a.tex.7"
        messages: list[str] = []

        class Cache:
            def load_groups(self, pak_paths, workers=0, progress=None):
                return [
                    PakHashGroup(
                        Path("re_chunk_000.pak"),
                        {hash_mixed(target)},
                        "re_chunk_000.pak",
                        0,
                    ),
                    PakHashGroup(
                        Path("re_chunk_000.pak.patch_001.pak"),
                        set(),
                        "re_chunk_000.pak",
                        1,
                        patch_index=1,
                    ),
                ]

        scan = DmpScanResult(unversioned_paths={"tex": Counter({"natives/STM/foo/a.tex": 1})})
        result = discover_suffixes(
            scan,
            [Path("unused.pak")],
            {},
            SuffixDiscoveryOptions(
                selected_extensions=["tex"],
                mode="custom",
                custom_versions="7",
                include_platform_suffixes=False,
                include_streaming=False,
                language_mode="off",
                processes=1,
            ),
            progress=messages.append,
            pak_cache=Cache(),
        )

        self.assertFalse(result.cancelled)
        self.assertEqual([match.full_path for match in result.matches], [target])
        self.assertTrue(
            any(
                isinstance(message, str) and "no new candidate versions" in message
                for message in messages
            )
        )

    def test_suffix_discovery_uses_versioned_paths_as_suffix_discovery_evidence(self) -> None:
        target = "natives/STM/foo/a.tex.8"

        class Cache:
            def load_groups(self, pak_paths, workers=0, progress=None):
                return [PakHashGroup(Path("base.pak"), {hash_mixed(target)}, "base.pak", 0)]

        scan = DmpScanResult(versioned_paths={"tex": Counter({"natives/STM/foo/a.tex": 1})})
        result = discover_suffixes(
            scan,
            [Path("base.pak")],
            {},
            SuffixDiscoveryOptions(
                selected_extensions=["tex"],
                mode="custom",
                custom_versions="8",
                include_platform_suffixes=False,
                include_streaming=False,
                language_mode="off",
                processes=1,
            ),
            pak_cache=Cache(),
        )

        self.assertFalse(result.cancelled)
        self.assertEqual([match.full_path for match in result.matches], [target])

    def test_base_families_do_not_share_discovered_version_skip_state(self) -> None:
        main_target = "natives/STM/main/a.tex.100"
        sub_target = "natives/STM/sub/a.tex.100"

        class Cache:
            def load_groups(self, pak_paths, workers=0, progress=None):
                return [
                    PakHashGroup(Path("re_chunk_000.pak"), {hash_mixed(main_target)}, "re_chunk_000.pak", 0),
                    PakHashGroup(
                        Path("re_chunk_000.pak.sub_000.pak"),
                        {hash_mixed(sub_target)},
                        "re_chunk_000.pak.sub_000.pak",
                        1,
                    ),
                ]

        scan = DmpScanResult(
            unversioned_paths={
                "tex": Counter(
                    {
                        "natives/STM/main/a.tex": 1,
                        "natives/STM/sub/a.tex": 1,
                    }
                )
            }
        )
        result = discover_suffixes(
            scan,
            [Path("unused.pak")],
            {},
            SuffixDiscoveryOptions(
                selected_extensions=["tex"],
                mode="custom",
                custom_versions="100",
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
            {main_target, sub_target},
        )

    def test_suffix_discovery_uses_multi_gpu_scheduler_when_multiple_devices_are_available(self) -> None:
        target = "natives/STM/foo/a.tex.7"

        class Cache:
            def load_groups(self, pak_paths, workers=0, progress=None):
                return [PakHashGroup(Path("base.pak"), {hash_mixed(target)}, "base.pak", 0)]

        def fake_multi_gpu_search(**kwargs):
            self.assertEqual(kwargs["device_ids"], [0, 1])
            self.assertEqual(kwargs["batch_sizes_by_device"], {0: 111, 1: 222})
            self.assertEqual(kwargs["producers_per_device"], 2)
            self.assertEqual(kwargs["prefetch_batches_per_device"], 2)
            kwargs["found_versions"].add(7)
            return MultiGpuSearchOutcome(
                matches=[SuffixDiscoveryMatch("tex", 7, "foo/a.tex", target)]
            )

        scan = DmpScanResult(unversioned_paths={"tex": Counter({"natives/STM/foo/a.tex": 1})})
        with (
            patch(
                "re_file_hash_exporter.core.suffix_discovery.engine.resolve_cuda_devices",
                return_value=(True, "torch CUDA backend ready: 2 device(s): cuda:0 A, cuda:1 B", [0, 1]),
            ),
            patch(
                "re_file_hash_exporter.core.suffix_discovery.engine.search_extension_multi_gpu",
                side_effect=fake_multi_gpu_search,
            ) as multi_gpu_search,
        ):
            result = discover_suffixes(
                scan,
                [Path("base.pak")],
                {},
                SuffixDiscoveryOptions(
                    selected_extensions=["tex"],
                    mode="custom",
                    custom_versions="7",
                    request_gpu=True,
                    gpu_devices=[0, 1],
                    gpu_batch_size=111,
                    gpu_batch_sizes={1: 222},
                    gpu_workers_per_device=2,
                    include_platform_suffixes=False,
                    include_streaming=False,
                    language_mode="off",
                ),
                pak_cache=Cache(),
            )

        self.assertEqual(multi_gpu_search.call_count, 1)
        self.assertFalse(result.cancelled)
        self.assertEqual([match.full_path for match in result.matches], [target])

    def test_gpu_worker_initializer_does_not_take_version_chunks_or_raw_hash_set(self) -> None:
        parameters = list(inspect.signature(_init_gpu_worker).parameters)

        self.assertNotIn("versions", parameters)
        self.assertNotIn("group_hashes", parameters)
        self.assertEqual(
            parameters,
            [
                "device_id",
                "batch_size",
                "producer_count",
                "prefetch_batches",
                "extension",
                "entries",
                "hash_descriptor",
                "include_platform_suffixes",
                "language_mode",
                "include_streaming",
                "profiles",
                "shared_found_versions",
                "stop_signal",
            ],
        )

    def test_gpu_worker_pool_uses_spawn_context_for_cuda(self) -> None:
        self.assertEqual(_gpu_process_context().get_start_method(), "spawn")

    def test_multi_gpu_scheduler_uses_one_cuda_owner_per_device(self) -> None:
        self.assertEqual(_cuda_owner_devices([0, 0, 1, 2, 1]), [0, 1, 2])

    def test_dynamic_gpu_chunk_size_scales_with_candidate_density(self) -> None:
        entries = [RawPathEntry("foo/bar.tex", seen_plain=True, seen_streaming=True)]
        chunk_size = _dynamic_version_chunk_size(
            entries=entries,
            extension="tex",
            plan=numeric_range_plan(0, 10000),
            batch_sizes_by_device={0: 8},
            include_platform_suffixes=True,
            language_mode="off",
            include_streaming=True,
            profiles={},
            max_version_chunk_size=4096,
            prefetch_batches_per_device=1,
        )

        self.assertGreaterEqual(chunk_size, 1)
        self.assertLess(chunk_size, 4096)

    def test_shared_hash_view_membership(self) -> None:
        shared_hashes = _create_shared_hash_data({7, 3, 99})
        try:
            view = _SharedHashView(shared_hashes.descriptor)
            try:
                self.assertIn(3, view)
                self.assertIn(99, view)
                self.assertNotIn(4, view)
                self.assertEqual(list(view), [3, 7, 99])
            finally:
                view.close()
        finally:
            shared_hashes.close()


if __name__ == "__main__":
    unittest.main()
