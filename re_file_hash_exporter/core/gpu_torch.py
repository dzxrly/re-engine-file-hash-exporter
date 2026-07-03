from __future__ import annotations

import math
import queue
import threading
from typing import Callable

from .search.gpu_batches import (
    PreparedGpuBase,
    PreparedGpuBatch,
    candidate_count_for_prepared_bases,
    iter_prepared_gpu_batches,
    prepare_gpu_bases,
)
from .search.path_catalog import RawPathEntry

ProgressCallback = Callable[[object], None]
CancelCallback = Callable[[], bool]
ScanProgressCallback = Callable[[int], None]
VersionFoundCallback = Callable[[int], bool]
MarkVersionFoundCallback = Callable[[int], None]

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


def resolve_cuda_devices(requested_devices: list[int] | None = None) -> tuple[bool, str, list[int]]:
    try:
        import torch
    except Exception as err:
        return False, f"torch is not installed or failed to import: {err}", []

    if not torch.cuda.is_available():
        return False, "torch is installed, but CUDA is not available.", []

    count = torch.cuda.device_count()
    if count <= 0:
        return False, "torch reports CUDA available, but no CUDA devices were found.", []

    if requested_devices:
        devices: list[int] = []
        seen: set[int] = set()
        for device in requested_devices:
            device = int(device)
            if device < 0 or device >= count:
                return False, f"Requested CUDA device {device}, but available devices are 0..{count - 1}.", []
            if device in seen:
                continue
            seen.add(device)
            devices.append(device)
    else:
        devices = list(range(count))

    names = ", ".join(f"cuda:{device} {torch.cuda.get_device_name(device)}" for device in devices)
    return True, f"torch CUDA backend ready: {len(devices)} device(s): {names}", devices


def torch_cuda_status(requested_devices: list[int] | None = None) -> tuple[bool, str]:
    ok, message, _devices = resolve_cuda_devices(requested_devices)
    return ok, message


def release_torch_cuda_cache(device: str = "cuda") -> None:
    try:
        import torch
    except Exception:
        return

    _release_device_cache(torch, device)


def _release_device_cache(torch, device: str) -> None:
    try:
        if str(device).lower().startswith("cuda") and torch.cuda.is_available():
            torch.cuda.empty_cache()
    except Exception:
        return


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

    for pair_index in range(pair_count):
        left = converted[:, pair_index * 2].to(torch.int64)
        right = converted[:, pair_index * 2 + 1].to(torch.int64)
        active = lengths >= (pair_index * 2 + 2)
        k = torch.bitwise_or(left, right << 16)
        next_state = torch.bitwise_xor(state, _murmur3_calc_k(torch, k))
        next_state = _rotl32(torch, next_state, MURMUR3_R2)
        next_state = _u32(torch, next_state * MURMUR3_M + MURMUR3_N)
        state = torch.where(active, next_state, state)

    odd = (lengths & 1) == 1
    if bool(odd.any()):
        last_index = torch.clamp(lengths - 1, min=0)
        tail = converted.gather(1, last_index.view(-1, 1).to(torch.int64)).view(-1).to(torch.int64)
        tail_state = torch.bitwise_xor(state, _murmur3_calc_k(torch, tail))
        state = torch.where(odd, tail_state, state)

    return _murmur3_finish(torch, state, lengths.to(torch.int64) * 2)


def _hash_preconverted_units(torch, units, lengths):
    state = torch.full((units.shape[0],), MASK32, dtype=torch.int64, device=units.device)
    pair_count = units.shape[1] // 2

    for pair_index in range(pair_count):
        left = units[:, pair_index * 2].to(torch.int64)
        right = units[:, pair_index * 2 + 1].to(torch.int64)
        active = lengths >= (pair_index * 2 + 2)
        k = torch.bitwise_or(left, right << 16)
        next_state = torch.bitwise_xor(state, _murmur3_calc_k(torch, k))
        next_state = _rotl32(torch, next_state, MURMUR3_R2)
        next_state = _u32(torch, next_state * MURMUR3_M + MURMUR3_N)
        state = torch.where(active, next_state, state)

    odd = (lengths & 1) == 1
    if bool(odd.any()):
        last_index = torch.clamp(lengths - 1, min=0)
        tail = units.gather(1, last_index.view(-1, 1).to(torch.int64)).view(-1).to(torch.int64)
        tail_state = torch.bitwise_xor(state, _murmur3_calc_k(torch, tail))
        state = torch.where(odd, tail_state, state)

    return _murmur3_finish(torch, state, lengths.to(torch.int64) * 2)


def hash_mixed_batch(paths: list[str], device: str = "cuda", release_cache: bool = True) -> list[int]:
    import torch

    if not paths:
        return []

    _set_cuda_device(torch, device)
    encoded = [path.encode("utf-16le") for path in paths]
    lengths = [len(data) // 2 for data in encoded]
    max_len = max(lengths)
    units = None
    length_tensor = None
    upper = None
    lower = None

    try:
        inference_context = getattr(torch, "inference_mode", torch.no_grad)
        with inference_context():
            units = torch.zeros((len(paths), max_len), dtype=torch.int32)
            for row, data in enumerate(encoded):
                values = [int.from_bytes(data[index : index + 2], "little") for index in range(0, len(data), 2)]
                if values:
                    units[row, : len(values)] = torch.tensor(values, dtype=torch.int32)

            units = units.to(device, non_blocking=True)
            length_tensor = torch.tensor(lengths, dtype=torch.int32, device=device)
            upper = _hash_units_case(torch, units, length_tensor, uppercase=True)
            lower = _hash_units_case(torch, units, length_tensor, uppercase=False)

            upper_values = upper.detach().cpu().tolist()
            lower_values = lower.detach().cpu().tolist()
            return [
                ((upper_value & MASK32) << 32) | (lower_value & MASK32)
                for upper_value, lower_value in zip(upper_values, lower_values)
            ]
    finally:
        del units, length_tensor, upper, lower
        if release_cache:
            _release_device_cache(torch, device)


def hash_prepared_mixed_batch(
    prepared_paths: list[tuple[tuple[int, ...], tuple[int, ...]]] | PreparedGpuBatch,
    device: str = "cuda",
    release_cache: bool = True,
) -> list[int]:
    import torch

    if not prepared_paths:
        return []

    _set_cuda_device(torch, device)
    compact_batch = prepared_paths if isinstance(prepared_paths, PreparedGpuBatch) else None
    lengths = (
        [int(length) for length in compact_batch.lengths]
        if compact_batch is not None
        else [len(upper_units) for upper_units, _lower_units in prepared_paths]
    )
    max_len = max(lengths)
    upper_units = None
    lower_units = None
    length_tensor = None
    upper = None
    lower = None

    try:
        inference_context = getattr(torch, "inference_mode", torch.no_grad)
        with inference_context():
            upper_units = torch.zeros((len(prepared_paths), max_len), dtype=torch.int32)
            lower_units = torch.zeros((len(prepared_paths), max_len), dtype=torch.int32)
            if compact_batch is not None:
                for row, length in enumerate(lengths):
                    if not length:
                        continue
                    offset = int(compact_batch.offsets[row])
                    upper_row = compact_batch.upper_units[offset : offset + length]
                    lower_row = compact_batch.lower_units[offset : offset + length]
                    upper_units[row, :length] = torch.tensor(upper_row, dtype=torch.int32)
                    lower_units[row, :length] = torch.tensor(lower_row, dtype=torch.int32)
            else:
                for row, (upper_row, lower_row) in enumerate(prepared_paths):
                    if upper_row:
                        upper_units[row, : len(upper_row)] = torch.tensor(upper_row, dtype=torch.int32)
                    if lower_row:
                        lower_units[row, : len(lower_row)] = torch.tensor(lower_row, dtype=torch.int32)

            upper_units = upper_units.to(device, non_blocking=True)
            lower_units = lower_units.to(device, non_blocking=True)
            length_tensor = torch.tensor(lengths, dtype=torch.int32, device=device)
            upper = _hash_preconverted_units(torch, upper_units, length_tensor)
            lower = _hash_preconverted_units(torch, lower_units, length_tensor)

            upper_values = upper.detach().cpu().tolist()
            lower_values = lower.detach().cpu().tolist()
            return [
                ((upper_value & MASK32) << 32) | (lower_value & MASK32)
                for upper_value, lower_value in zip(upper_values, lower_values)
            ]
    finally:
        del upper_units, lower_units, length_tensor, upper, lower
        if release_cache:
            _release_device_cache(torch, device)


def match_extension_with_torch(
    extension: str,
    entries: list[RawPathEntry],
    versions: list[int],
    pak_hashes: set[int],
    include_platform_suffixes: bool,
    language_mode: str,
    include_streaming: bool,
    profiles: dict[str, dict] | None,
    progress: ProgressCallback | None,
    cancel_requested: CancelCallback | None,
    scan_progress: ScanProgressCallback | None = None,
    batch_size: int = 16384,
    found_versions: set[int] | None = None,
    device: str = "cuda",
    is_version_found: VersionFoundCallback | None = None,
    mark_version_found: MarkVersionFoundCallback | None = None,
    producer_count: int = 1,
    prefetch_batches: int = 0,
    prepared_bases: tuple[PreparedGpuBase, ...] | None = None,
) -> tuple[list[tuple[str, int, str]], bool]:
    ok, message = torch_cuda_status(_requested_devices_for_device(device))
    if not ok:
        raise RuntimeError(message)

    discovered_versions = found_versions if found_versions is not None else set()
    version_lock = threading.Lock()

    def version_found(version: int) -> bool:
        with version_lock:
            return version in discovered_versions or bool(is_version_found and is_version_found(version))

    def mark_discovered(version: int) -> None:
        with version_lock:
            discovered_versions.add(version)
        if mark_version_found:
            mark_version_found(version)

    active_versions = [version for version in versions if not version_found(version)]
    if not active_versions:
        return [], bool(cancel_requested and cancel_requested())

    bases = prepared_bases
    if bases is None:
        bases = prepare_gpu_bases(
            entries,
            extension,
            include_platform_suffixes,
            language_mode,
            include_streaming,
            profiles,
        )
    total_candidates = candidate_count_for_prepared_bases(bases, len(active_versions))
    total_batches = max(1, math.ceil(total_candidates / batch_size))
    if progress:
        progress(
            f".{extension}: torch CUDA hashing {total_candidates} candidates on {device} "
            f"in {total_batches} batch(es), batch size {batch_size}, "
            f"{max(1, int(producer_count))} producer(s), prefetch {max(0, int(prefetch_batches))}."
        )

    matches: list[tuple[str, int, str]] = []
    seen: set[str] = set()
    try:
        for batch_index, batch in enumerate(
            _iter_gpu_batches_with_prefetch(
                bases,
                active_versions,
                batch_size,
                cancel_requested,
                None,
                is_version_found=version_found,
                producer_count=producer_count,
                prefetch_batches=prefetch_batches,
            ),
            start=1,
        ):
            if cancel_requested and cancel_requested():
                return matches, True

            version_text = _format_version_progress(batch.min_version, batch.max_version)
            hashes = hash_prepared_mixed_batch(batch, device=device, release_cache=False)
            if cancel_requested and cancel_requested():
                return matches, True
            if scan_progress:
                scan_progress(len(batch))
            batch_matches = []
            for index, hash_value in enumerate(hashes):
                version = int(batch.versions[index])
                if version_found(version):
                    continue
                if hash_value in pak_hashes:
                    full_path = batch.full_path_at(index, bases)
                    if full_path in seen:
                        continue
                    seen.add(full_path)
                    mark_discovered(version)
                    batch_matches.append((batch.raw_path_at(index, bases), version, full_path))
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
    finally:
        release_torch_cuda_cache(device)


def _format_version_progress(min_version: int | None, max_version: int | None) -> str:
    if min_version is None or max_version is None:
        return "versions unknown"
    if min_version == max_version:
        return f"version {min_version}"
    return f"versions {min_version}..{max_version}"


def _iter_gpu_batches_with_prefetch(
    bases: tuple[PreparedGpuBase, ...],
    active_versions: list[int],
    batch_size: int,
    cancel_requested: CancelCallback | None,
    found_versions: set[int] | None,
    is_version_found: VersionFoundCallback,
    producer_count: int,
    prefetch_batches: int,
):
    producer_count = max(1, int(producer_count or 1))
    prefetch_batches = max(0, int(prefetch_batches or 0))
    if producer_count <= 1 and prefetch_batches <= 0:
        yield from iter_prepared_gpu_batches(
            bases,
            active_versions,
            batch_size,
            cancel_requested,
            found_versions,
            is_version_found=is_version_found,
        )
        return

    batch_queue: queue.Queue[PreparedGpuBatch | BaseException | None] = queue.Queue(
        maxsize=max(1, prefetch_batches)
    )
    version_slices = _split_versions_for_producers(active_versions, producer_count)
    done_count = 0

    def produce(version_slice: list[int]) -> None:
        try:
            for batch in iter_prepared_gpu_batches(
                bases,
                version_slice,
                batch_size,
                cancel_requested,
                found_versions,
                is_version_found=is_version_found,
            ):
                if cancel_requested and cancel_requested():
                    break
                _put_prefetch_item(batch_queue, batch, cancel_requested)
        except BaseException as exc:
            _put_prefetch_item(batch_queue, exc, cancel_requested)
        finally:
            _put_prefetch_item(batch_queue, None, cancel_requested)

    threads = [
        threading.Thread(
            target=produce,
            args=(version_slice,),
            name=f"gpu-candidate-producer-{index}",
            daemon=True,
        )
        for index, version_slice in enumerate(version_slices)
        if version_slice
    ]
    if not threads:
        return

    for thread in threads:
        thread.start()

    try:
        while done_count < len(threads):
            item = batch_queue.get()
            if item is None:
                done_count += 1
                continue
            if isinstance(item, BaseException):
                raise item
            yield item
    finally:
        for thread in threads:
            thread.join(timeout=0.2)


def _split_versions_for_producers(versions: list[int], producer_count: int) -> list[list[int]]:
    producer_count = max(1, min(int(producer_count), len(versions) or 1))
    out = [[] for _index in range(producer_count)]
    for index, version in enumerate(versions):
        out[index % producer_count].append(version)
    return out


def _put_prefetch_item(
    batch_queue: "queue.Queue[PreparedGpuBatch | BaseException | None]",
    item: PreparedGpuBatch | BaseException | None,
    cancel_requested: CancelCallback | None,
) -> None:
    while True:
        if cancel_requested and cancel_requested():
            return
        try:
            batch_queue.put(item, timeout=0.1)
            return
        except queue.Full:
            continue


def _requested_devices_for_device(device: str) -> list[int] | None:
    text = str(device).lower()
    if text == "cuda":
        return None
    if text.startswith("cuda:"):
        try:
            return [int(text.split(":", 1)[1])]
        except ValueError:
            return None
    return None


def _set_cuda_device(torch, device: str) -> None:
    if str(device).lower().startswith("cuda:"):
        torch.cuda.set_device(device)
