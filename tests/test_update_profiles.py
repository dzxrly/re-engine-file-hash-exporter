from __future__ import annotations

import json
import tempfile
import unittest
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
from unittest.mock import patch

try:
    import tomllib
except ModuleNotFoundError:  # pragma: no cover - Python < 3.11
    import tomli as tomllib  # type: ignore[no-redef]

import update


class ProfileUpdateScriptTests(unittest.TestCase):
    def test_exported_toml_uses_suffix_map_config_shape(self) -> None:
        data = {
            "version": 1,
            "description": "Example",
            "suffix_types": {"numeric": "Numbers"},
            "extensions": {
                "mesh": {
                    "suffix_type": "date_code",
                    "date_format": "YYMMDD",
                    "priority_dates": ["2024-01-01"],
                    "priority_tails": [7],
                }
            },
        }

        text = update.profile_data_to_toml(data)
        parsed = tomllib.loads(text)

        self.assertIn("languages", parsed)
        self.assertEqual(parsed["prefixes"], ["natives/STM/"])
        self.assertFalse(parsed["use_builtin_suffix_map"])
        self.assertEqual(parsed["suffix_map"]["mesh"], [240101007])

    def test_main_merges_toml_into_json_and_exports(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            json_path = root / "file_suffix_profiles.json"
            toml_path = root / "patch.toml"
            output_path = root / "file_suffix_profiles.toml"

            json_path.write_text(
                json.dumps(
                    {
                        "version": 1,
                        "description": "Original",
                        "suffix_types": {"numeric": "Numbers"},
                        "extensions": {
                            "mesh": {
                                "suffix_type": "date_code",
                                "priority_dates": ["2024-01-01"],
                            }
                        },
                    }
                ),
                encoding="utf-8",
            )
            toml_path.write_text(
                """
description = "Updated"

[extensions.".Mesh"]
priority_tails = [7]

[extensions.tex]
suffix_type = "numeric"
priority_versions = [1, 2, 3]
""".strip(),
                encoding="utf-8",
            )

            with redirect_stdout(StringIO()):
                exit_code = update.main(
                    [
                        str(toml_path),
                        "--profiles-json",
                        str(json_path),
                        "--output",
                        str(output_path),
                    ]
                )

            self.assertEqual(exit_code, 0)
            merged = json.loads(json_path.read_text(encoding="utf-8"))
            self.assertEqual(merged["description"], "Updated")
            self.assertEqual(merged["extensions"]["mesh"]["priority_dates"], ["2024-01-01"])
            self.assertEqual(merged["extensions"]["mesh"]["priority_tails"], [7])
            self.assertEqual(merged["extensions"]["tex"]["priority_versions"], [1, 2, 3])
            self.assertTrue(output_path.is_file())
            exported = tomllib.loads(output_path.read_text(encoding="utf-8"))
            self.assertEqual(exported["suffix_map"]["mesh"], [240101007])
            self.assertEqual(exported["suffix_map"]["tex"], [1, 2, 3])

    def test_ree_project_list_merge_updates_numeric_and_date_profiles(self) -> None:
        data = {
            "extensions": {
                "mesh": {
                    "suffix_type": "date_code",
                    "date_format": "YYMMDD",
                    "tail_width": 3,
                    "priority_dates": ["2024-01-01"],
                    "priority_tails": [1],
                },
                "user": {
                    "suffix_type": "numeric",
                    "priority_versions": [2],
                },
            }
        }
        sources = [
            (
                "sample.list",
                [
                    "natives/stm/ch0000.mesh.240102007\n",
                    "natives/stm/ch0001.mesh.240102009.STM\n",
                    "natives/stm/defaultbtableorderlist.user.3\n",
                    "natives/stm/new_resource.abc.42\n",
                ],
            )
        ]

        stats = update.merge_ree_project_list_sources(data, sources)

        self.assertEqual(stats.files, 1)
        self.assertEqual(stats.versioned_paths, 4)
        self.assertEqual(data["extensions"]["mesh"]["priority_dates"], ["2024-01-01", "2024-01-02"])
        self.assertEqual(data["extensions"]["mesh"]["priority_tails"], [1, 7, 9])
        self.assertEqual(data["extensions"]["user"]["priority_versions"], [2, 3])
        self.assertEqual(data["extensions"]["abc"]["suffix_type"], "numeric")
        self.assertEqual(data["extensions"]["abc"]["priority_versions"], [42])

    def test_new_ree_project_extension_can_infer_date_code_profile(self) -> None:
        data = {"extensions": {}}

        stats = update.merge_ree_project_list_sources(
            data,
            [
                (
                    "sample.list",
                    [
                        "natives/stm/example.tex.241106027\n",
                        "natives/stm/example2.tex.250206176\n",
                    ],
                )
            ],
        )

        self.assertEqual(stats.new_extensions, {"tex"})
        self.assertEqual(data["extensions"]["tex"]["suffix_type"], "date_code")
        self.assertEqual(data["extensions"]["tex"]["date_format"], "YYMMDD")
        self.assertEqual(data["extensions"]["tex"]["tail_width"], 3)
        self.assertEqual(data["extensions"]["tex"]["priority_dates"], ["2024-11-06", "2025-02-06"])
        self.assertEqual(data["extensions"]["tex"]["priority_tails"], [27, 176])

    def test_main_merges_remote_ree_project_lists_and_exports(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            json_path = root / "file_suffix_profiles.json"
            output_path = root / "file_suffix_profiles.toml"

            json_path.write_text(
                json.dumps(
                    {
                        "version": 1,
                        "description": "Original",
                        "extensions": {
                            "user": {
                                "suffix_type": "numeric",
                                "priority_versions": [2],
                            }
                        },
                    }
                ),
                encoding="utf-8",
            )
            remote_sources = [
                (
                    "sample.list",
                    [
                        "natives/stm/defaultbtableorderlist.user.3",
                        "natives/stm/extra_resource.abc.42",
                    ],
                )
            ]

            with patch.object(update, "iter_github_ree_project_list_sources", return_value=remote_sources):
                with redirect_stdout(StringIO()):
                    exit_code = update.main(
                        [
                            "--ree-projects-github",
                            "--profiles-json",
                            str(json_path),
                            "--output",
                            str(output_path),
                        ]
                    )

            self.assertEqual(exit_code, 0)
            merged = json.loads(json_path.read_text(encoding="utf-8"))
            self.assertEqual(merged["extensions"]["user"]["priority_versions"], [2, 3])
            self.assertEqual(merged["extensions"]["abc"]["priority_versions"], [42])
            self.assertTrue(output_path.is_file())
            exported = tomllib.loads(output_path.read_text(encoding="utf-8"))
            self.assertEqual(exported["suffix_map"]["abc"], [42])

    def test_default_export_name_is_universal_config(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            json_path = root / "file_suffix_profiles.json"
            expected_output = root / "universal_config.toml"

            json_path.write_text(
                json.dumps(
                    {
                        "version": 1,
                        "extensions": {
                            "tex": {
                                "suffix_type": "numeric",
                                "priority_versions": [7],
                            }
                        },
                    }
                ),
                encoding="utf-8",
            )

            with patch.object(update, "DEFAULT_TOML_PATH", expected_output):
                with redirect_stdout(StringIO()):
                    exit_code = update.main(["--profiles-json", str(json_path)])

            self.assertEqual(exit_code, 0)
            self.assertTrue(expected_output.is_file())
            exported = tomllib.loads(expected_output.read_text(encoding="utf-8"))
            self.assertEqual(exported["suffix_map"]["tex"], [7])

    def test_proxy_url_replaces_github_clone_base(self) -> None:
        with patch.object(update, "PROXY_URL", "https://github-proxy.example/base"):
            self.assertEqual(
                update._ree_pak_tool_repository_url(),
                "https://github-proxy.example/base/Ekey/REE.PAK.Tool.git",
            )


if __name__ == "__main__":
    unittest.main()
