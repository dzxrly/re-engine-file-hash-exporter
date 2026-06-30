from __future__ import annotations

import struct
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

ProgressCallback = Callable[[str], None]

HEADER_MAGIC = b"KPKA"
ENTRY_ENCRYPTION = 1 << 3
EXTRA_U32 = 1 << 4
EXTRA_DATA = 1 << 2

MODULUS_BYTES = bytes(
    [
        0x7D, 0x0B, 0xF8, 0xC1, 0x7C, 0x23, 0xFD, 0x3B,
        0xD4, 0x75, 0x16, 0xD2, 0x33, 0x21, 0xD8, 0x10,
        0x71, 0xF9, 0x7C, 0xD1, 0x34, 0x93, 0xBA, 0x77,
        0x26, 0xFC, 0xAB, 0x2C, 0xEE, 0xDA, 0xD9, 0x1C,
        0x89, 0xE7, 0x29, 0x7B, 0xDD, 0x8A, 0xAE, 0x50,
        0x39, 0xB6, 0x01, 0x6D, 0x21, 0x89, 0x5D, 0xA5,
        0xA1, 0x3E, 0xA2, 0xC0, 0x8C, 0x93, 0x13, 0x36,
        0x65, 0xEB, 0xE8, 0xDF, 0x06, 0x17, 0x67, 0x96,
        0x06, 0x2B, 0xAC, 0x23, 0xED, 0x8C, 0xB7, 0x8B,
        0x90, 0xAD, 0xEA, 0x71, 0xC4, 0x40, 0x44, 0x9D,
        0x1C, 0x7B, 0xBA, 0xC4, 0xB6, 0x2D, 0xD6, 0xD2,
        0x4B, 0x62, 0xD6, 0x26, 0xFC, 0x74, 0x20, 0x07,
        0xEC, 0xE3, 0x59, 0x9A, 0xE6, 0xAF, 0xB9, 0xA8,
        0x35, 0x8B, 0xE0, 0xE8, 0xD3, 0xCD, 0x45, 0x65,
        0xB0, 0x91, 0xC4, 0x95, 0x1B, 0xF3, 0x23, 0x1E,
        0xC6, 0x71, 0xCF, 0x3E, 0x35, 0x2D, 0x6B, 0xE3,
        0x00,
    ]
)
EXPONENT = int.from_bytes(bytes([0x01, 0x00, 0x01, 0x00]), "little")
MODULUS = int.from_bytes(MODULUS_BYTES, "little")


@dataclass(slots=True)
class PakHeader:
    path: Path
    major_version: int
    minor_version: int
    feature: int
    total_files: int
    header_hash: int

    @property
    def entry_size(self) -> int:
        if self.major_version == 2:
            return 24
        if self.major_version == 4:
            return 48
        raise ValueError(f"Unsupported PAK major version: {self.major_version}")


def decrypt_key(enc_key: bytes) -> bytes:
    resized = enc_key + b"\x00" * max(0, 129 - len(enc_key))
    value = int.from_bytes(resized[:129], "little")
    decrypted = pow(value, EXPONENT, MODULUS)
    return decrypted.to_bytes(max(32, (decrypted.bit_length() + 7) // 8), "little")


def decrypt_pak_data(data: bytes, enc_key: bytes) -> bytes:
    key = decrypt_key(enc_key)
    out = bytearray(len(data))
    for index, byte in enumerate(data):
        mask = (index + key[index % 32] * key[index % 29]) & 0xFF
        out[index] = byte ^ mask
    return bytes(out)


def read_header(handle, path: Path) -> PakHeader:
    raw = handle.read(16)
    if len(raw) != 16:
        raise ValueError(f"PAK header too short: {path}")
    magic, major, minor, feature, total_files, header_hash = struct.unpack("<4sBBHII", raw)
    if magic != HEADER_MAGIC:
        raise ValueError(f"Invalid PAK magic for {path}: {magic!r}")
    if major not in (2, 4):
        raise ValueError(f"Unsupported PAK major version {major} for {path}")
    return PakHeader(path, major, minor, feature, total_files, header_hash)


def parse_entry_hashes(header: PakHeader, entry_table: bytes) -> set[int]:
    hashes: set[int] = set()
    if header.major_version == 2 and header.minor_version == 0:
        for offset in range(0, len(entry_table), 24):
            _file_offset, _size, lower, upper = struct.unpack_from("<QQII", entry_table, offset)
            hashes.add((upper << 32) | lower)
        return hashes

    for offset in range(0, len(entry_table), 48):
        lower, upper = struct.unpack_from("<II", entry_table, offset)
        hashes.add((upper << 32) | lower)
    return hashes


def read_pak_hashes(path: Path) -> set[int]:
    with path.open("rb") as handle:
        header = read_header(handle, path)
        table_size = header.entry_size * header.total_files
        entry_table = handle.read(table_size)
        if len(entry_table) != table_size:
            raise ValueError(f"PAK entry table truncated: {path}")

        if header.feature & EXTRA_U32:
            handle.read(4)
        if header.feature & EXTRA_DATA:
            handle.read(9)
        if header.feature & ENTRY_ENCRYPTION:
            raw_key = handle.read(128)
            if len(raw_key) != 128:
                raise ValueError(f"PAK entry key truncated: {path}")
            entry_table = decrypt_pak_data(entry_table, raw_key)

    return parse_entry_hashes(header, entry_table)


def load_hashes_from_paks(
    pak_paths: list[Path],
    workers: int = 0,
    progress: ProgressCallback | None = None,
) -> set[int]:
    if not pak_paths:
        raise ValueError("No PAK files selected.")
    max_workers = workers if workers and workers > 0 else min(8, len(pak_paths))
    all_hashes: set[int] = set()

    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = {executor.submit(read_pak_hashes, path): path for path in pak_paths}
        for index, future in enumerate(as_completed(futures), start=1):
            path = futures[future]
            hashes = future.result()
            all_hashes.update(hashes)
            if progress:
                progress(f"Loaded PAK metadata [{index}/{len(pak_paths)}]: {path.name} ({len(hashes)} hashes)")

    return all_hashes
