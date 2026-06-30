# RE File Hash Exporter

[English](README.md) | [简体中文](docs/README.zh-CN.md) | [繁體中文](docs/README.zh-TW.md)

`RE File Hash Exporter` is a small GUI tool for building `config.toml` files for RE Engine DMP and PAK workflows.

It scans resource paths from a selected `.DMP` file, exports known versioned suffixes, reports paths whose suffix versions are missing, and can optionally brute-force missing suffix versions against PAK entry hashes.

## Features

- Scan UTF-16LE resource paths from one exact `.DMP` file.
- Export the first-step `suffix_map` for paths that already contain numeric suffixes in the DMP.
- Group and report raw paths such as `name.ext` that do not include `.version`.
- Let the user select missing extensions and brute-force candidate version suffixes.
- Optionally show already versioned extensions in Step 2 so they can be searched for additional suffix versions.
- Match candidates against PAK metadata hashes without unpacking PAK content.
- Support multiple PAK files.
- Use CPU multiprocessing for brute-force matching.
- Use torch CUDA acceleration when `GPU acceleration (CUDA only)` is enabled and a CUDA-enabled `torch` install is available.
- Show `GPU batch size` only when GPU mode is requested.
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
cd C:\Software\mhws\re-file-hash-exporter
python main.py
```

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

## GPU Batch Size

When `GPU acceleration (CUDA only)` is enabled, the UI shows `GPU batch size`. The default is `16384`.

If GPU utilization is low and VRAM usage is comfortable, try increasing it gradually:

```text
16384 -> 32768 -> 65536 -> 131072
```

If CUDA reports out-of-memory errors or the system becomes unstable, lower the value by one step. Larger batches are not always faster because candidate generation and UTF-16 encoding still involve CPU-side work.

If `torch` is missing, not CUDA-enabled, or no CUDA device is available, the tool falls back to CPU multiprocessing and logs the reason.
