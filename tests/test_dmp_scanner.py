from __future__ import annotations

import tempfile
import unittest
from collections import Counter
from pathlib import Path

from re_file_hash_exporter.core.dmp.scanner import scan_dmp_file
from re_file_hash_exporter.core.workflow import ExportWorkflow


def _wide(text: str) -> bytes:
    return text.encode("utf-16le")


class DmpScannerTests(unittest.TestCase):
    def test_binary_terminated_suffix_fragment_is_ignored(self) -> None:
        valid_date_code = _wide("natives/STM/foo/valid.tex.251111100\0")
        suspicious_fragment = _wide("natives/STM/foo/fragment.tex.2511111")
        pointer_like_data = bytes.fromhex("9a e5 df fb 7a 00 00 00")

        with tempfile.TemporaryDirectory() as temp:
            dmp_path = Path(temp) / "sample.DMP"
            dmp_path.write_bytes(
                valid_date_code
                + suspicious_fragment
                + pointer_like_data
                + b"\x00\x00"
            )

            scan = scan_dmp_file(dmp_path, chunk_size=1024, overlap=128)

        self.assertEqual(scan.suffix_counts["tex"], Counter({251111100: 1}))
        self.assertNotIn(2511111, scan.suffix_counts["tex"])
        self.assertEqual(
            scan.warnings,
            [
                "Ignored suspicious DMP path fragment ending in .tex.2511111 "
                "(1 occurrence; followed by non-ASCII/binary data instead of a UTF-16 text boundary)."
            ],
        )
        self.assertNotIn("natives/STM/foo/fragment.tex", scan.versioned_paths["tex"])

    def test_path_deferred_from_chunk_overlap_is_scanned_once_when_complete(self) -> None:
        path = "gamedesign/abcdefghijklmnopqrstuvwxyz/a.tex.251111100\0"

        with tempfile.TemporaryDirectory() as temp:
            dmp_path = Path(temp) / "chunked.DMP"
            dmp_path.write_bytes(_wide(path))

            scan = scan_dmp_file(dmp_path, chunk_size=64, overlap=48)

        self.assertEqual(scan.suffix_counts["tex"], Counter({251111100: 1}))
        self.assertEqual(scan.warnings, [])

    def test_quoted_modern_suffix_has_a_safe_text_boundary(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            dmp_path = Path(temp) / "quoted.DMP"
            dmp_path.write_bytes(_wide('natives/STM/foo/quoted.tex.251111100"\0'))

            scan = scan_dmp_file(dmp_path, chunk_size=1024, overlap=128)

        self.assertEqual(scan.suffix_counts["tex"], Counter({251111100: 1}))
        self.assertEqual(scan.warnings, [])

    def test_workflow_logs_warning_and_exports_only_trusted_suffixes(self) -> None:
        valid_date_code = _wide("natives/STM/foo/valid.tex.251111100\0")
        suspicious_fragment = _wide("natives/STM/foo/fragment.tex.2511111")
        pointer_like_data = bytes.fromhex("9a e5 df fb 7a 00 00 00")
        messages: list[object] = []

        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            dmp_path = root / "sample.DMP"
            output_path = root / "config.toml"
            dmp_path.write_bytes(valid_date_code + suspicious_fragment + pointer_like_data)

            scan = ExportWorkflow().run_simple_export(dmp_path, output_path, progress=messages.append)
            output_text = output_path.read_text(encoding="utf-8")

        self.assertEqual(scan.suffix_counts["tex"], Counter({251111100: 1}))
        self.assertIn("tex = [251111100]", output_text)
        self.assertTrue(
            any(str(message).startswith("Warning: Ignored suspicious DMP path fragment") for message in messages)
        )


if __name__ == "__main__":
    unittest.main()
