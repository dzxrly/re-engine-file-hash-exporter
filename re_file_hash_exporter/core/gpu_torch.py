from __future__ import annotations

import math
from typing import Callable, Iterable

from .constants import DEFAULT_PLATFORM_SUFFIXES, DEFAULT_PREFIXES, LANGUAGES

ProgressCallback = Callable[[str], None]
CancelCallback = Callable[[], bool]

MASK32 = 0xFFFF_FFFF
MURMUR3_C1 = 0x85EB_CA6B
MURMUR3_C2 = 0xC2B2_AE35
MURMUR3_R1 = 16
MURMUR3_R2 = 13
MURMUR3_M = 5
MURMUR3_N = 0xE654_6B64
MURMUR3_BLOCK_C1 = 0xCC9E_2D51
MURMUR3_BLOCK_C2 = 0x1B87_3593
MURMUR3_BLOCK_R1 = 15


def torch_cuda_status() -> tuple[bool, str]:
    try:
        import torch
    except Exception as err:
        return False, f"torch is not installed or failed to import: {err}"

    if not torch.cuda.is_available():
        return False, "torch is installed, but CUDA is not available."

    name = torch.cuda.get_device_name(0)
    return True, f"torch CUDA backend ready: {name}"


def _u32(torch, value):
    return torch.bitwise_and(value, MASK32)


def _rotl32(torch, value, bits: int):
    value = _u32(torch, value)
    return _u32(torch, torch.bitwise_or(value << bits, value >> (32 - bits)))


def _murmur3_calc_k(torch, k):
    k = _u32(torch, k * MURMUR3_BLOCK_C1)
    k = _rotl32(torch, k, MURMUR3_BLOCK_R1)
    return _u32(torch, k * MURMUR3_BLOCK_C2)


def _murmur3_finish(torch, state, processed):
    h = _u32(torch, torch.bitwise_xor(state, processed))
    h = torch.bitwise_xor(h, h >> MURMUR3_R1)
    h = _u32(torch, h * MURMUR3_C1)
    h = torch.bitwise_xor(h, h >> MURMUR3_R2)
    h = _u32(torch, h * MURMUR3_C2)
    h = torch.bitwise_xor(h, h >> MURMUR3_R1)
    return _u32(torch, h)


def _hash_units_case(torch, units, lengths, uppercase: bool):
    converted = units
    if uppercase:
        converted = torch.where((units >= 97) & (units <= 122), units - 32, units)
    else:
        converted = torch.where((units >= 65) & (units <= 90), units + 32, units)

    state = torch.full((units.shape[0],), MASK32, dtype=torch.int64, device=units.device)
    pair_count = units.shape[1] // 2
    positions = torch.arange(units.shape[0], dtype=torch.int64, device=units.device)
    del positions

    for pair_index in range(pair_count):
        left = converted[:, pair_index * 2]
        right = converted[:, pair_index * 2 + 1]
        active = lengths >= (pair_index * 2 + 2)
        k = torch.bitwise_or(left, right << 16)
        next_state = torch.bitwise_xor(state, _murmur3_calc_k(torch, k))
        next_state = _rotl32(torch, next_state, MURMUR3_R2)
        next_state = _u32(torch, next_state * MURMUR3_M + MURMUR3_N)
        state = torch.where(active, next_state, state)

    odd = (lengths & 1) == 1
    if bool(odd.any()):
        last_index = torch.clamp(lengths - 1, min=0)
        tail = converted.gather(1, last_index.view(-1, 1)).view(-1)
        tail_state = torch.bitwise_xor(state, _murmur3_calc_k(torch, tail))
        state = torch.where(odd, tail_state, state)

    return _murmur3_finish(torch, state, lengths * 2)


def hash_mixed_batch(paths: list[str], device: str = "cuda") -> list[int]:
    import torch

    if not paths:
        return []

    encoded = [path.encode("utf-16le") for path in paths]
    lengths = [len(data) // 2 for data in encoded]
    max_len = max(lengths)
    units = torch.zeros((len(paths), max_len), dtype=torch.int64)
    for row, data in enumerate(encoded):
        values = [int.from_bytes(data[index : index + 2], "little") for index in range(0, len(data), 2)]
        if values:
            units[row, : len(values)] = torch.tensor(values, dtype=torch.int64)

    units = units.to(device, non_blocking=True)
    length_tensor = torch.tensor(lengths, dtype=torch.int64, device=device)
    upper = _hash_units_case(torch, units, length_tensor, uppercase=True)
    lower = _hash_units_case(torch, units, length_tensor, uppercase=False)

    upper_values = upper.detach().cpu().tolist()
    lower_values = lower.detach().cpu().tolist()
    return [((upper_value & MASK32) << 32) | (lower_value & MASK32) for upper_value, lower_value in zip(upper_values, lower_values)]


def _candidate_paths(
    raw_path: str,
    version: int,
    include_platform_suffixes: bool,
    include_languages: bool,
    include_streaming: bool,
):
    raw_variants = [raw_path]
    if include_streaming:
        raw_variants.append(f"streaming/{raw_path}")

    for prefix in DEFAULT_PREFIXES:
        for raw_variant in raw_variants:
            base = f"{prefix}{raw_variant}.{version}"
            bases = [base]
            if include_platform_suffixes:
                bases.extend(f"{base}.{suffix}" for suffix in DEFAULT_PLATFORM_SUFFIXES)
            for full_path in bases:
                yield full_path
                if include_languages:
                    for language in LANGUAGES:
                        yield f"{full_path}.{language}"


def _candidate_count(
    raw_path_count: int,
    version_count: int,
    include_platform_suffixes: bool,
    include_languages: bool,
    include_streaming: bool,
) -> int:
    raw_variants = 2 if include_streaming else 1
    base_variants = 1 + (len(DEFAULT_PLATFORM_SUFFIXES) if include_platform_suffixes else 0)
    language_variants = 1 + (len(LANGUAGES) if include_languages else 0)
    return raw_path_count * version_count * raw_variants * base_variants * language_variants


def _iter_candidate_batches(
    raw_paths: Iterable[str],
    versions: list[int],
    include_platform_suffixes: bool,
    include_languages: bool,
    include_streaming: bool,
    batch_size: int,
    cancel_requested: CancelCallback | None,
):
    batch: list[tuple[str, int, str]] = []
    for raw_path in raw_paths:
        if cancel_requested and cancel_requested():
            break
        for version in versions:
            if cancel_requested and cancel_requested():
                break
            for full_path in _candidate_paths(
                raw_path,
                version,
                include_platform_suffixes,
                include_languages,
                include_streaming,
            ):
                batch.append((raw_path, version, full_path))
                if len(batch) >= batch_size:
                    yield batch
                    batch = []
    if batch:
        yield batch


def match_extension_with_torch(
    extension: str,
    raw_paths: list[str],
    versions: list[int],
    pak_hashes: set[int],
    include_platform_suffixes: bool,
    include_languages: bool,
    include_streaming: bool,
    progress: ProgressCallback | None,
    cancel_requested: CancelCallback | None,
    batch_size: int = 16384,
) -> tuple[list[tuple[str, int, str]], bool]:
    ok, message = torch_cuda_status()
    if not ok:
        raise RuntimeError(message)

    total_candidates = _candidate_count(
        len(raw_paths),
        len(versions),
        include_platform_suffixes,
        include_languages,
        include_streaming,
    )
    total_batches = max(1, math.ceil(total_candidates / batch_size))
    if progress:
        progress(
            f".{extension}: torch CUDA hashing {total_candidates} candidates "
            f"in {total_batches} batch(es), batch size {batch_size}."
        )

    matches: list[tuple[str, int, str]] = []
    seen: set[str] = set()
    for batch_index, batch in enumerate(
        _iter_candidate_batches(
            raw_paths,
            versions,
            include_platform_suffixes,
            include_languages,
            include_streaming,
            batch_size,
            cancel_requested,
        ),
        start=1,
    ):
        if cancel_requested and cancel_requested():
            return matches, True

        full_paths = [item[2] for item in batch]
        batch_versions = [item[1] for item in batch]
        version_text = _format_version_progress(min(batch_versions), max(batch_versions))
        hashes = hash_mixed_batch(full_paths, device="cuda")
        batch_matches = []
        for (raw_path, version, full_path), hash_value in zip(batch, hashes):
            if hash_value in pak_hashes and full_path not in seen:
                seen.add(full_path)
                batch_matches.append((raw_path, version, full_path))
        matches.extend(batch_matches)

        if progress:
            if batch_matches:
                progress(
                    f".{extension}: GPU batch {batch_index}/{total_batches} {version_text} found {len(batch_matches)} match(es)."
                )
            else:
                progress(f".{extension}: GPU batch {batch_index}/{total_batches} {version_text} complete.")
            for _raw_path, version, full_path in batch_matches:
                progress(f"MATCH .{extension}.{version} -> {full_path}")

    return matches, bool(cancel_requested and cancel_requested())


def _format_version_progress(min_version: int, max_version: int) -> str:
    if min_version == max_version:
        return f"version {min_version}"
    return f"versions {min_version}..{max_version}"
