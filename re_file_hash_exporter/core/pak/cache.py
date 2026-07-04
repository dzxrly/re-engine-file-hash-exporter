from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from .reader import PakHashGroup, load_hash_groups_from_paks

ProgressCallback = Callable[[str], None]


@dataclass(frozen=True, slots=True)
class PakCacheKey:
    path: str
    size: int
    mtime_ns: int


class PakHashCache:
    def __init__(self) -> None:
        self._groups_by_key: dict[tuple[PakCacheKey, ...], list[PakHashGroup]] = {}

    def load_groups(
        self,
        pak_paths: list[Path],
        workers: int = 0,
        progress: ProgressCallback | None = None,
    ) -> list[PakHashGroup]:
        key = tuple(_cache_key(path) for path in pak_paths)
        cached = self._groups_by_key.get(key)
        if cached is not None:
            if progress:
                progress(f"Using cached PAK metadata for {len(pak_paths)} file(s).")
            return cached

        groups = load_hash_groups_from_paks(pak_paths, workers=workers, progress=progress)
        self._groups_by_key[key] = groups
        return groups

    def clear(self) -> None:
        self._groups_by_key.clear()


def _cache_key(path: Path) -> PakCacheKey:
    resolved = path.resolve()
    stat = resolved.stat()
    return PakCacheKey(path=str(resolved), size=stat.st_size, mtime_ns=stat.st_mtime_ns)
