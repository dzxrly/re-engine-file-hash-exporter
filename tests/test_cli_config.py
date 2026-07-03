from __future__ import annotations

import importlib
import sys
import tempfile
import unittest
from collections import Counter
from io import StringIO
from pathlib import Path

from re_file_hash_exporter.cli import ConfigError, Step2ProgressRenderer, load_cli_config, select_extensions_from_scan
from re_file_hash_exporter.core.models import BruteForceProgress, DmpScanResult


class FakeRichConsole:
    def __init__(self) -> None:
        self.messages: list[str] = []

    def print(self, message: str) -> None:
        self.messages.append(message)


class FakeRichProgress:
    def __init__(self) -> None:
        self.console = FakeRichConsole()
        self.started = False
        self.stopped = False
        self.added_tasks: list[dict] = []
        self.updates: list[dict] = []
        self.resets: list[dict] = []

    def start(self) -> None:
        self.started = True

    def stop(self) -> None:
        self.stopped = True

    def add_task(self, description: str, **kwargs):
        self.added_tasks.append({"description": description, **kwargs})
        return 42

    def update(self, task_id, **kwargs) -> None:
        self.updates.append({"task_id": task_id, **kwargs})

    def reset(self, task_id, **kwargs) -> None:
        self.resets.append({"task_id": task_id, **kwargs})


class CliConfigTests(unittest.TestCase):
    def test_main_module_does_not_import_gui_at_import_time(self) -> None:
        sys.modules.pop("re_file_hash_exporter.__main__", None)
        sys.modules.pop("re_file_hash_exporter.ui.app", None)

        importlib.import_module("re_file_hash_exporter.__main__")

        self.assertNotIn("re_file_hash_exporter.ui.app", sys.modules)

    def test_load_config_resolves_relative_paths_and_step2_options(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / "dump.DMP").write_bytes(b"fake")
            pak_dir = root / "paks"
            pak_dir.mkdir()
            (pak_dir / "base.pak").write_bytes(b"pak")
            config_path = root / "cli_config.toml"
            config_path.write_text(
                """
dmp_path = "dump.DMP"
output_path = "out/config.toml"
pak_dirs = ["paks"]

[step2]
selected_extensions = ["tex", ".rcol"]
mode = "custom"
custom_versions = "7"
date_end = "today"
processes = 1
language_mode = "off"
include_platform_suffixes = false
include_streaming = false
gpu_devices = [0, 1]
gpu_batch_sizes = "0:524288,1:262144"
gpu_workers_per_device = 2
gpu_producers_per_device = 3
gpu_prefetch_batches_per_device = 4
""".strip(),
                encoding="utf-8",
            )

            config = load_cli_config(config_path)

            self.assertEqual(config.config_path, config_path.resolve())
            self.assertEqual(config.base_dir, root.resolve())
            self.assertEqual(config.dmp_path, (root / "dump.DMP").resolve())
            self.assertEqual(config.output_path, (root / "out" / "config.toml").resolve())
            self.assertEqual(config.pak_paths, [(pak_dir / "base.pak").resolve()])
            self.assertEqual(config.step2.selected_extensions, ["tex", "rcol"])
            self.assertEqual(config.step2.mode, "custom")
            self.assertEqual(config.step2.custom_versions, "7")
            self.assertEqual(config.step2.date_end, "today")
            self.assertEqual(config.step2.processes, 1)
            self.assertEqual(config.step2.language_mode, "off")
            self.assertFalse(config.step2.include_platform_suffixes)
            self.assertFalse(config.step2.include_streaming)
            self.assertEqual(config.step2.gpu_devices, [0, 1])
            self.assertEqual(config.step2.gpu_batch_sizes, {0: 524288, 1: 262144})
            self.assertEqual(config.step2.gpu_workers_per_device, 2)
            self.assertEqual(config.step2.gpu_producers_per_device, 3)
            self.assertEqual(config.step2.gpu_prefetch_batches_per_device, 4)

    def test_step2_requires_paks_when_enabled(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / "dump.DMP").write_bytes(b"fake")
            config_path = root / "cli_config.toml"
            config_path.write_text('dmp_path = "dump.DMP"\n', encoding="utf-8")

            with self.assertRaises(ConfigError):
                load_cli_config(config_path)

    def test_step2_can_be_disabled_without_paks(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / "dump.DMP").write_bytes(b"fake")
            config_path = root / "cli_config.toml"
            config_path.write_text(
                'dmp_path = "dump.DMP"\nrun_step2 = false\n',
                encoding="utf-8",
            )

            config = load_cli_config(config_path)

            self.assertFalse(config.run_step2)
            self.assertEqual(config.pak_paths, [])

    def test_config_directory_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / "cli_config.toml").write_text("run_step2 = false\n", encoding="utf-8")

            with self.assertRaisesRegex(ConfigError, "not a directory"):
                load_cli_config(root)

    def test_default_pak_dir_glob_finds_uppercase_pak_extensions(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / "dump.DMP").write_bytes(b"fake")
            pak_dir = root / "paks"
            pak_dir.mkdir()
            upper_pak = pak_dir / "BASE.PAK"
            upper_pak.write_bytes(b"pak")
            config_path = root / "cli_config.toml"
            config_path.write_text(
                'dmp_path = "dump.DMP"\npak_dirs = ["paks"]\n',
                encoding="utf-8",
            )

            config = load_cli_config(config_path)

            self.assertEqual(config.pak_paths, [upper_pak.resolve()])

    def test_pak_dirs_accepts_glob_patterns(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / "dump.DMP").write_bytes(b"fake")
            pak_dir = root / "paks"
            pak_dir.mkdir()
            base_pak = pak_dir / "base.pak"
            patch_pak = pak_dir / "base.patch_001.pak"
            ignored = pak_dir / "notes.txt"
            base_pak.write_bytes(b"pak")
            patch_pak.write_bytes(b"pak")
            ignored.write_text("not a pak", encoding="utf-8")
            config_path = root / "cli_config.toml"
            config_path.write_text(
                'dmp_path = "dump.DMP"\npak_dirs = ["paks/*.pak"]\n',
                encoding="utf-8",
            )

            config = load_cli_config(config_path)

            self.assertEqual(config.pak_paths, [base_pak.resolve(), patch_pak.resolve()])

    def test_select_extensions_defaults_to_all_missing(self) -> None:
        scan = DmpScanResult(
            unversioned_paths={
                "tex": Counter({"natives/STM/foo/a.tex": 1}),
                "exe": Counter({"ignored.exe": 1}),
            },
            versioned_paths={"rcol": Counter({"natives/STM/foo/a.rcol": 1})},
        )

        self.assertEqual(select_extensions_from_scan(scan, None), ["tex"])

    def test_select_all_can_include_versioned_extensions(self) -> None:
        scan = DmpScanResult(
            unversioned_paths={"tex": Counter({"natives/STM/foo/a.tex": 1})},
            versioned_paths={"rcol": Counter({"natives/STM/foo/a.rcol": 1})},
        )

        self.assertEqual(
            select_extensions_from_scan(scan, "all", include_versioned_extensions=True),
            ["rcol", "tex"],
        )

    def test_step2_progress_renderer_updates_rich_progress_and_logs_above_it(self) -> None:
        fake_progress = FakeRichProgress()
        progress = BruteForceProgress(
            completed_extensions=1,
            total_extensions=2,
            completed_scan_count=50,
            total_scan_count=100,
            elapsed_seconds=3.0,
            current_extension="tex",
            phase="searching",
            phase_detail="Searching .tex",
        )

        with Step2ProgressRenderer(progress_factory=lambda: fake_progress) as renderer:
            renderer(progress)
            renderer("MATCH .tex.7 -> natives/STM/foo/a.tex.7")

        self.assertTrue(fake_progress.started)
        self.assertTrue(fake_progress.stopped)
        self.assertEqual(fake_progress.added_tasks[0]["description"], "Step 2")
        self.assertEqual(fake_progress.updates[0]["task_id"], 42)
        self.assertEqual(fake_progress.updates[0]["completed"], 50.0)
        self.assertEqual(fake_progress.updates[0]["extensions"], "ext 1/2")
        self.assertEqual(fake_progress.updates[0]["scans"], "scans 50/100")
        self.assertEqual(fake_progress.updates[0]["phase"], "Searching .tex")
        self.assertEqual(fake_progress.console.messages, ["MATCH .tex.7 -> natives/STM/foo/a.tex.7"])

    def test_step2_progress_renderer_resets_rich_eta_when_phase_progress_rewinds(self) -> None:
        fake_progress = FakeRichProgress()
        planning_done = BruteForceProgress(
            completed_extensions=2,
            total_extensions=2,
            completed_scan_count=500,
            total_scan_count=0,
            elapsed_seconds=3.0,
            phase="planning",
            phase_detail="Planning .tex",
        )
        search_started = BruteForceProgress(
            completed_extensions=0,
            total_extensions=2,
            completed_scan_count=0,
            total_scan_count=1000,
            elapsed_seconds=3.1,
            phase="searching",
            phase_detail="Searching base.pak",
        )

        with Step2ProgressRenderer(progress_factory=lambda: fake_progress) as renderer:
            renderer(planning_done)
            renderer(search_started)

        self.assertEqual(fake_progress.updates[0]["completed"], 100.0)
        self.assertEqual(fake_progress.resets, [{"task_id": 42, "total": 100.0, "completed": 0.0}])
        self.assertEqual(fake_progress.updates[1]["completed"], 0.0)
        self.assertEqual(fake_progress.updates[1]["phase"], "Searching base.pak")

    def test_step2_progress_renderer_falls_back_when_rich_is_missing(self) -> None:
        out = StringIO()
        err = StringIO()
        missing_rich = ModuleNotFoundError("No module named 'rich'")
        missing_rich.name = "rich"

        def raise_missing_rich():
            raise missing_rich

        progress = BruteForceProgress(
            completed_extensions=0,
            total_extensions=1,
            completed_scan_count=0,
            total_scan_count=10,
            elapsed_seconds=0.0,
            phase="loading_paks",
            phase_detail="Loading metadata",
        )

        with Step2ProgressRenderer(
            progress_factory=raise_missing_rich,
            fallback_stream=out,
            error_stream=err,
        ) as renderer:
            renderer(progress)
            renderer("Loading PAK entry hashes...")

        self.assertIn("Rich is not installed", err.getvalue())
        self.assertIn("[loading_paks | Loading metadata]", out.getvalue())
        self.assertIn("Loading PAK entry hashes...", out.getvalue())


if __name__ == "__main__":
    unittest.main()
