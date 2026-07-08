# RE File Hash Exporter

[English](README.md) | [简体中文](docs/README.zh-CN.md) | [繁體中文](docs/README.zh-TW.md)

`RE File Hash Exporter` is a small GUI tool for building `config.toml` files for RE Engine DMP and PAK workflows.

It scans resource paths from a selected `.DMP` file, exports known versioned suffixes, reports paths whose suffix versions are missing, and can optionally discover suffix versions for selected extensions against PAK entry hashes.

## Features

- Scan UTF-16LE resource paths from one exact `.DMP` file.
- Export the first-step `suffix_map` for paths that already contain numeric suffixes in the DMP.
- Group and report raw paths such as `name.ext` that do not include `.version`.
- Let the user select extensions and discover candidate version suffixes from current DMP path evidence.
- Provide `auto_detect` candidate planning from editable presets in `file_suffix_profiles.json`.
- Optionally show already versioned extensions in Step 2 so they can be searched for additional suffix versions.
- Match candidates against PAK metadata hashes without unpacking PAK content.
- Support multiple PAK files.
- Treat patch-only PAK selections as incremental scans seeded from `file_suffix_profiles.json`.
- Cache PAK metadata between repeated Step 2 runs when the selected PAK files do not change.
- Use CPU multiprocessing for suffix discovery matching.
- Reuse CPU worker pools per PAK group and reuse precomputed UTF-16 hash prefix states.
- Use torch CUDA acceleration when `GPU acceleration (CUDA only)` is enabled and a CUDA-enabled `torch` install is available.
- Use multiple CUDA devices in parallel when more than one GPU is selected or visible.
- Reduce GPU-side search overhead by batching pre-encoded UTF-16 candidate units instead of complete path strings.
- Show GPU-specific options only when GPU mode is requested.
- Allow stopping suffix discovery at any time.
- Lock file inputs and step options while a scan or suffix discovery task is running.

## Requirements

Install the Python packages listed in `requirements.txt`.

```powershell
pip install -r requirements.txt
```

`torch` is optional at runtime unless you want GPU acceleration. For CUDA acceleration, install a CUDA-enabled PyTorch build that matches your local NVIDIA driver and CUDA environment.

## Run

```powershell
python main.py
```

## CLI Mode

CLI mode reads a TOML config file and runs Step 1, then Step 2, without opening the GUI:

```powershell
python main.py --cli <config-file.toml>
# or
python main.py cli <config-file.toml>
```

Relative paths inside the file are resolved from the config file's directory. `output_path` defaults to `config.toml` in the same directory.
During Step 2, CLI mode uses a Rich progress bar fixed at the bottom of the terminal while logs scroll above it.
Press `Ctrl+C` once during Step 2 to request a graceful stop; partial matches found so far are merged into the output config, matching the GUI `Stop` behavior. Press `Ctrl+C` again to force interruption.

Complete example:

```toml
dmp_path = "dump.DMP"
output_path = "config.toml"
run_step2 = true

pak_paths = []
pak_dirs = ["*.[pP][aA][kK]"]
pak_glob = "*.[pP][aA][kK]"

[step2]
selected_extensions = "all_missing"
mode = "auto_detect"
min_version = 0
max_version = 4096
custom_versions = ""
neighbor_radius = 32
date_start = "0"
date_end = "today"
processes = 0
include_platform_suffixes = true
language_mode = "localized"
include_streaming = true
include_versioned_extensions = false
request_gpu = false
gpu_batch_size = 16384
gpu_devices = "auto"
gpu_workers_per_device = 1
gpu_batch_sizes = ""
```

Recommended layout: put input/output and PAK selection at the top level, and put suffix discovery options under `[step2]`. Step 2 options may also be placed at the top level. A legacy `[bruteforce]` table is accepted, but `[step2]` takes precedence when both exist.

Top-level fields:

| Field | Type and default | Description |
| --- | --- | --- |
| `dmp_path` | string, required | DMP file to scan in Step 1. Relative paths are resolved from the config file's directory. |
| `output_path` | string, default `config.toml` | Output config path written by Step 1 and updated by Step 2. |
| `run_step2` | boolean, default `true` | Set to `false` to only run Step 1. Can be top-level or inside `[step2]`. |
| `pak_paths` | string or string array, default empty | Exact PAK files or glob patterns. Examples: `"base.pak"`, `"*.pak"`, `["base.pak", "patch_001.pak"]`. |
| `pak_dirs` | string or string array, default empty | Directories to scan with `pak_glob`, or glob patterns such as `"*.[pP][aA][kK]"`. |
| `pak_dir` | string or string array, default empty | Compatibility alias for `pak_dirs`. Prefer `pak_dirs` in new configs. |
| `pak_glob` | string, default `*.[pP][aA][kK]` | File pattern used only for `pak_dirs` entries that are directories. |

`[step2]` fields:

| Field | Type and default | Description |
| --- | --- | --- |
| `selected_extensions` | string, string array, or omitted | Extensions to search. Omit or use `"all_missing"` to search extensions with unversioned paths from Step 1. `"missing"` and `"auto"` are aliases. Use `"all"` to include every extension with any Step 1 path evidence. CSV strings such as `"tex,rcol"` and arrays such as `["tex", "rcol"]` are accepted. Leading dots are optional. |
| `mode` | string, default `"small_range"` | Candidate version planning mode. Allowed values: `"small_range"`, `"adaptive"`, `"custom"`, `"auto_detect"`. |
| `min_version` | integer, default `0` | In `small_range`, the inclusive lower version. In `auto_detect` numeric profiles, how far to expand below the preset minimum. Must be non-negative. |
| `max_version` | integer, default `4096` | In `small_range`, the inclusive upper version. In `auto_detect` numeric profiles, how far to expand above the preset maximum. Must be at least `min_version` except in `auto_detect`. |
| `custom_versions` | string, default empty | Used only by `custom`. Supports comma/newline separated values and ranges such as `"12,18,30-40"`. |
| `neighbor_radius` | integer, default `32` | Used by `adaptive`; searches known versions plus and minus this radius. |
| `date_start` | string, default empty | For `auto_detect` `date_code` profiles with `priority_dates`, this is `Date -days`, the number of days to expand before the earliest priority date. |
| `date_end` | string, default empty | For `auto_detect` `date_code` profiles with `priority_dates`, this is `Date +days`, the number of days to expand after the latest priority date. Use `"today"` to expand through the local system date without shrinking the preset priority range. |
| `processes` | integer, default `0` | CPU worker count. `0` uses the machine CPU count. |
| `include_platform_suffixes` | boolean, default `true` | Generate platform suffix variants such as `.STM` and `.X64` when path evidence suggests they may be needed. |
| `language_mode` | string, default `"localized"` | Language suffix mode. Allowed values: `"localized"`, `"off"`, `"all"`. |
| `include_streaming` | boolean, default `true` | Generate `streaming/` path variants when path evidence suggests they may be needed. |
| `include_versioned_extensions` | boolean, default `false` | When `selected_extensions` is omitted or `"all_missing"`, also auto-select extensions that only had versioned path evidence in Step 1. Once an extension is selected, Step 2 always uses both versioned and unversioned raw paths as evidence. |
| `request_gpu` | boolean, default `false` | Request torch CUDA acceleration. If CUDA or torch is unavailable, the search falls back to CPU and logs the reason. |
| `gpu_batch_size` | integer, default `16384` | Default GPU candidate batch size for every selected CUDA device. This is not auto-tuned. |
| `gpu_devices` | `"auto"`, integer, or integer array, default `[]` | CUDA devices to use. `"auto"` or an empty value uses every visible CUDA device. Examples: `0`, `[0, 1, 2, 3]`, `"0,1"`. |
| `gpu_workers_per_device` | integer, default `1` | Number of worker processes per CUDA device. Start with `1`; higher values can increase contention. |
| `gpu_batch_sizes` | string or TOML table, default empty | Optional per-device batch overrides. String form: `"0:524288,1:262144"`. Table form: `{0 = 524288, 1 = 262144}`. Values must be positive. |

Use TOML booleans (`true` / `false`) and integers for numeric fields. Boolean strings such as `"yes"` and `"no"` are accepted by the CLI, but plain TOML booleans are clearer.

## Basic Workflow

1. Select the exact `.DMP` file.
2. Choose where to save `config.toml`.
3. Add one or more `.pak` files.
4. Run Step 1 to scan the DMP and export known suffixes.
5. Select the extensions you want to search from the Step 1 path evidence.
6. Optionally enable `Show versioned-only extensions` to list extensions that only appeared with known suffixes.
7. Run Step 2 to discover suffix versions and merge successful matches back into `config.toml`.

Step 2 stays disabled until Step 1 finishes successfully.
While Step 1 or Step 2 is running, file inputs and task options are locked. During Step 2, only `Stop` remains available.

## Outputs

After choosing an output path, the tool writes:

- `config.toml`: a config file compatible with ree-path-searcher / ree-pak-researcher style workflows.
- `<name>.missing_versions.txt`: a report of raw paths found in Step 1 without numeric suffix versions.

## Suffix Discovery Strategy

Step 2 takes the selected extensions from the Step 1 raw path evidence and generates candidates such as:

```text
natives/STM/<raw_path>.<version>
natives/STM/<raw_path>.<version>.X64
natives/STM/<raw_path>.<version>.STM
...
```

The tool computes the RE Engine mixed UTF-16 hash for each candidate and compares it with the hash set read from the PAK entry tables. Successful matches are merged into `suffix_map`, then `config.toml` is saved again.

Candidate generation is profile-guided. Step 1 keeps light path evidence for each raw path, including whether the DMP reference was seen under `streaming/` and whether a platform tail such as `.STM` or `.X64` was already present. Step 2 uses both versioned and unversioned raw paths for selected extensions, together with `file_suffix_profiles.json`, to avoid unnecessary path variants.

## Candidate Modes

`Candidate mode` controls only how Step 2 builds the candidate version-number list used in `<raw_path>.<version>`. Path variants are controlled separately by `Platform suffixes`, `Languages`, and `Streaming variants`.

- `small_range`: tries every version from `Min version` to `Max version`, inclusive. The default range is `0..4096`. This is the broadest option, but it can take the longest.
- `adaptive`: uses known suffix versions found by Step 1 for the same extension, then expands around each known version by `Neighbor radius`. With the default radius `32`, a known version `100` plans `68..132`. If the selected extension has no known version, it falls back to the `Min version..Max version` range.
- `custom`: tries only the values entered in `Custom versions`. Use commas or new lines to separate values, and use ranges such as `12, 18, 30-40`. The values are deduplicated and sorted. In this mode, `Min version`, `Max version`, and `Neighbor radius` are ignored.
- `auto_detect`: reads `file_suffix_profiles.json` from the project root and plans versions per selected extension. `numeric` profiles use `priority_versions` as the baseline range: `Min version` subtracts from the preset lower bound, and `Max version` adds to the preset upper bound. For example, a preset `2..38` with `Min version = 10` and `Max version = 4096` searches `0..4134`. `date_code` profiles use `priority_dates` as the baseline date range; `Date -days` expands the lower date bound, `Date +days` expands the upper date bound, `Date +days = today` uses the local system date as the upper bound, and `priority_tails` are tried before the remaining `000..999` tails.

As a rule of thumb, start with `auto_detect` when searching several different file types, use `adaptive` when Step 1 has found related known versions, use `small_range` when you need a broader search, and use `custom` when you already know the likely version numbers.

## Language Modes

`Languages` controls whether Step 2 adds the `.Ja` and `.En` probe language suffixes. These probes are used to confirm that a numeric suffix exists without expanding every RE Engine language tag.

- `localized`: the default. Language suffixes are generated only for likely localized resources: extensions marked with `"language_search": true` in `file_suffix_profiles.json`, built-in localized extensions such as `.msg`, `.asrc`, `.bnk`, `.pck`, `.sbnk`, and `.spck`, or raw paths containing localization-style folders such as `/message/`, `/text/`, `/subtitle/`, `/voice`, `/dialog/`, or `/localization/`.
- `off`: never generates language suffix variants.
- `all`: generates the `.Ja` and `.En` probe suffix variants for every selected path.

The preset file is intentionally plain JSON so it can be tuned without code changes. Its top-level `languages` array is reused when generating TOML config files and keeps the full RE Engine language list by default, independent from the `.Ja` / `.En` Step 2 search probes. It also defines baseline ranges and priority values, while the UI controls how far those ranges expand. Add or edit entries under `extensions`; use `suffix_type = "numeric"` with optional `priority_versions`, or `suffix_type = "date_code"` with optional `priority_dates` and `priority_tails`. Add `"language_search": true` only for file types whose paths commonly use RE Engine language suffixes.

## Candidate Pruning

Each extension profile can also narrow path variants:

- `language_search`: `true` enables `.Ja` and `.En` language suffix probes for the extension; `false` disables them even in broad language modes.
- `streaming_search`: `false` disables `streaming/` variants, `true` searches them for every path, and `"observed"` searches them only for paths that were seen as streaming references in the DMP.
- `platform_search`: `false` disables `.X64` / `.STM` variants, `"observed"` searches only platform suffixes seen in the DMP, and a list such as `["STM"]` limits the suffixes explicitly.

When `streaming_search` is omitted, Step 2 defaults to path-level streaming evidence instead of doubling every raw path. When `platform_search` is omitted, the UI `Platform suffixes` option keeps the previous broad behavior.

## Performance Notes

CPU matching precomputes the hash state for long path prefixes such as `natives/STM/<raw_path>.`, then appends pre-encoded version, platform, and language suffix fragments. This avoids re-encoding and re-hashing the full path for every candidate.

When several extensions are searched against the same PAK group, CPU workers are reused for the whole group. PAK entry hashes are cached on the workflow object using path, size, and modification time, so repeated Step 2 runs with unchanged PAKs skip metadata loading.

If a selected PAK group starts with a patch file and no base PAK was loaded, Step 2 still uses incremental mode. The initial lower bound is seeded from each selected extension's `file_suffix_profiles.json` baseline, then later patches continue from versions discovered in earlier patches from the same group.

## GPU Batch Size

When `GPU acceleration (CUDA only)` is enabled, the UI shows `GPU batch size`. The default is `16384`.

If GPU utilization is low and VRAM usage is comfortable, try increasing it gradually:

```text
16384 -> 32768 -> 65536 -> 131072
```

If CUDA reports out-of-memory errors or the system becomes unstable, lower the value by one step. Larger batches are not always faster because candidate generation and UTF-16 encoding still involve CPU-side work.

If `torch` is missing, not CUDA-enabled, or no CUDA device is available, the tool falls back to CPU multiprocessing and logs the reason.

## Multi-GPU Search

When GPU mode is enabled, `GPU devices = auto` uses every visible CUDA device. You can limit the search to specific devices with values such as `0,1` in the GUI or `gpu_devices = [0, 1]` in CLI config.

The search scheduler assigns version chunks dynamically to GPU workers, so faster devices receive more work as they finish earlier. Matches are merged in the main process and discovered suffix versions are shared between workers to reduce duplicate work.

Use `gpu_batch_size` as the default batch size for every selected GPU. If devices have different VRAM, set per-device overrides:

```toml
[step2]
request_gpu = true
gpu_devices = [0, 1]
gpu_batch_size = 262144
gpu_batch_sizes = "0:524288,1:131072"
gpu_workers_per_device = 1
```
