# RE File Hash Exporter

[English](README.md) | [简体中文](docs/README.zh-CN.md) | [繁體中文](docs/README.zh-TW.md)

`RE File Hash Exporter` is a small GUI tool for building `config.toml` files for RE Engine DMP and PAK workflows.

It scans resource paths from a selected `.DMP` file, exports known versioned suffixes, reports paths whose suffix versions are missing, and can optionally brute-force missing suffix versions against PAK entry hashes.

## Features

- Scan UTF-16LE resource paths from one exact `.DMP` file.
- Export the first-step `suffix_map` for paths that already contain numeric suffixes in the DMP.
- Group and report raw paths such as `name.ext` that do not include `.version`.
- Let the user select missing extensions and brute-force candidate version suffixes.
- Provide `auto_detect` candidate planning from editable presets in `file_suffix_profiles.json`.
- Optionally show already versioned extensions in Step 2 so they can be searched for additional suffix versions.
- Match candidates against PAK metadata hashes without unpacking PAK content.
- Support multiple PAK files.
- Cache PAK metadata between repeated Step 2 runs when the selected PAK files do not change.
- Use CPU multiprocessing for brute-force matching.
- Reuse CPU worker pools per PAK group and reuse precomputed UTF-16 hash prefix states.
- Use torch CUDA acceleration when `GPU acceleration (CUDA only)` is enabled and a CUDA-enabled `torch` install is available.
- Use multiple CUDA devices in parallel when more than one GPU is selected or visible.
- Reduce GPU-side search overhead by batching pre-encoded UTF-16 candidate units instead of complete path strings.
- Show GPU-specific options only when GPU mode is requested.
- Allow stopping brute-force matching at any time.
- Lock file inputs and step options while a scan or brute-force task is running.

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

```toml
dmp_path = "dump.DMP"
output_path = "config.toml"
pak_dirs = ["paks"]
# pak_paths = ["re_chunk_000.pak", "re_chunk_000.pak.patch_001.pak"]

[step2]
selected_extensions = "all_missing" # or "all", "tex,rcol", ["tex", "rcol"]
mode = "auto_detect"
min_version = 0
max_version = 4096
processes = 0
language_mode = "localized" # localized, off, all
include_platform_suffixes = true
include_streaming = true
include_versioned_extensions = false
request_gpu = false
gpu_batch_size = 16384
gpu_devices = "auto" # or [0, 1]
gpu_workers_per_device = 1
gpu_batch_sizes = "0:16384,1:16384" # optional per-device override
```

Set `run_step2 = false` if you want CLI mode to only scan the DMP and write the first-step config.

## Basic Workflow

1. Select the exact `.DMP` file.
2. Choose where to save `config.toml`.
3. Add one or more `.pak` files.
4. Run Step 1 to scan the DMP and export known suffixes.
5. If Step 1 reports missing extensions, select the extensions you want to search.
6. Optionally enable `Show versioned extensions` to search extensions that already have known suffixes.
7. Run Step 2 to brute-force suffix versions and merge successful matches back into `config.toml`.

Step 2 stays disabled until Step 1 finishes successfully.
While Step 1 or Step 2 is running, file inputs and task options are locked. During Step 2, only `Stop` remains available.

## Outputs

After choosing an output path, the tool writes:

- `config.toml`: a config file compatible with ree-path-searcher / ree-pak-researcher style workflows.
- `<name>.missing_versions.txt`: a report of raw paths found in Step 1 without numeric suffix versions.

## Brute-Force Strategy

Step 2 takes the selected missing extensions from the Step 1 raw path list and generates candidates such as:

```text
natives/STM/<raw_path>.<version>
natives/STM/<raw_path>.<version>.X64
natives/STM/<raw_path>.<version>.STM
...
```

The tool computes the RE Engine mixed UTF-16 hash for each candidate and compares it with the hash set read from the PAK entry tables. Successful matches are merged into `suffix_map`, then `config.toml` is saved again.

Candidate generation is profile-guided. Step 1 keeps light path evidence for each raw path, including whether the DMP reference was seen under `streaming/` and whether a platform tail such as `.STM` or `.X64` was already present. Step 2 uses that evidence together with `file_suffix_profiles.json` to avoid unnecessary path variants.

## Candidate Modes

`Candidate mode` controls only how Step 2 builds the candidate version-number list used in `<raw_path>.<version>`. Path variants are controlled separately by `Platform suffixes`, `Languages`, and `Streaming variants`.

- `small_range`: tries every version from `Min version` to `Max version`, inclusive. The default range is `0..4096`. This is the broadest option, but it can take the longest.
- `adaptive`: uses known suffix versions found by Step 1 for the same extension, then expands around each known version by `Neighbor radius`. With the default radius `32`, a known version `100` plans `68..132`. If the selected extension has no known version, it falls back to the `Min version..Max version` range.
- `custom`: tries only the values entered in `Custom versions`. Use commas or new lines to separate values, and use ranges such as `12, 18, 30-40`. The values are deduplicated and sorted. In this mode, `Min version`, `Max version`, and `Neighbor radius` are ignored.
- `auto_detect`: reads `file_suffix_profiles.json` from the project root and plans versions per selected extension. `numeric` profiles use `priority_versions` as the baseline range: `Min version` subtracts from the preset lower bound, and `Max version` adds to the preset upper bound. For example, a preset `2..38` with `Min version = 10` and `Max version = 4096` searches `0..4134`. `date_code` profiles use `priority_dates` as the baseline date range; `Date -days` expands the lower date bound, `Date +days` expands the upper date bound, and `priority_tails` are tried before the remaining `000..999` tails.

As a rule of thumb, start with `auto_detect` when searching several different file types, use `adaptive` when Step 1 has found related known versions, use `small_range` when you need a broader search, and use `custom` when you already know the likely version numbers.

## Language Modes

`Languages` controls whether Step 2 adds `.Ja`, `.En`, `.ZhCN`, and other language suffix variants.

- `localized`: the default. Language suffixes are generated only for likely localized resources: extensions marked with `"language_search": true` in `file_suffix_profiles.json`, built-in localized extensions such as `.msg`, `.asrc`, `.bnk`, `.pck`, `.sbnk`, and `.spck`, or raw paths containing localization-style folders such as `/message/`, `/text/`, `/subtitle/`, `/voice`, `/dialog/`, or `/localization/`.
- `off`: never generates language suffix variants.
- `all`: generates language suffix variants for every selected path, matching the older broad-search behavior.

The preset file is intentionally plain JSON so it can be tuned without code changes. It defines baseline ranges and priority values, while the UI controls how far those ranges expand. Add or edit entries under `extensions`; use `suffix_type = "numeric"` with optional `priority_versions`, or `suffix_type = "date_code"` with optional `priority_dates` and `priority_tails`. Add `"language_search": true` only for file types whose paths commonly use RE Engine language suffixes.

## Candidate Pruning

Each extension profile can also narrow path variants:

- `language_search`: `true` enables language suffixes for the extension; `false` disables them even in broad language modes.
- `streaming_search`: `false` disables `streaming/` variants, `true` searches them for every path, and `"observed"` searches them only for paths that were seen as streaming references in the DMP.
- `platform_search`: `false` disables `.X64` / `.STM` variants, `"observed"` searches only platform suffixes seen in the DMP, and a list such as `["STM"]` limits the suffixes explicitly.

When `streaming_search` is omitted, Step 2 defaults to path-level streaming evidence instead of doubling every raw path. When `platform_search` is omitted, the UI `Platform suffixes` option keeps the previous broad behavior.

## Performance Notes

CPU matching precomputes the hash state for long path prefixes such as `natives/STM/<raw_path>.`, then appends pre-encoded version, platform, and language suffix fragments. This avoids re-encoding and re-hashing the full path for every candidate.

When several extensions are searched against the same PAK group, CPU workers are reused for the whole group. PAK entry hashes are cached on the workflow object using path, size, and modification time, so repeated Step 2 runs with unchanged PAKs skip metadata loading.

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
