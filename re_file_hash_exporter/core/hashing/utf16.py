from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable

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


def _u32(value: int) -> int:
    return value & MASK32


def _rotl32(value: int, bits: int) -> int:
    value &= MASK32
    return ((value << bits) | (value >> (32 - bits))) & MASK32


def _murmur3_calc_k(k: int) -> int:
    k = _u32(k * MURMUR3_BLOCK_C1)
    k = _rotl32(k, MURMUR3_BLOCK_R1)
    k = _u32(k * MURMUR3_BLOCK_C2)
    return k


def _murmur3_finish(state: int, processed: int) -> int:
    h = _u32(state ^ processed)
    h ^= h >> MURMUR3_R1
    h = _u32(h * MURMUR3_C1)
    h ^= h >> MURMUR3_R2
    h = _u32(h * MURMUR3_C2)
    h ^= h >> MURMUR3_R1
    return _u32(h)


class _Murmur3State:
    __slots__ = ("state", "processed", "tail", "tail_len")

    def __init__(self) -> None:
        self.state = 0xFFFF_FFFF
        self.processed = 0
        self.tail = bytearray(4)
        self.tail_len = 0

    def clone(self) -> "_Murmur3State":
        other = _Murmur3State()
        other.state = self.state
        other.processed = self.processed
        other.tail[:] = self.tail
        other.tail_len = self.tail_len
        return other

    def write_byte(self, byte: int) -> None:
        self.tail[self.tail_len] = byte & 0xFF
        self.tail_len += 1
        self.processed += 1

        if self.tail_len == 4:
            k = int.from_bytes(self.tail, "little")
            self.state ^= _murmur3_calc_k(k)
            self.state = _rotl32(self.state, MURMUR3_R2)
            self.state = _u32(self.state * MURMUR3_M + MURMUR3_N)
            self.tail_len = 0

    def write_u16(self, unit: int) -> None:
        self.write_byte(unit & 0xFF)
        self.write_byte((unit >> 8) & 0xFF)

    def write_units(self, units: Iterable[int]) -> None:
        for unit in units:
            self.write_u16(unit)

    def finish(self) -> int:
        state = self.state
        if self.tail_len:
            k = 0
            for index in range(self.tail_len):
                k |= self.tail[index] << (index * 8)
            state ^= _murmur3_calc_k(k)
        return _murmur3_finish(state, self.processed)


def _lower_ascii_utf16(unit: int) -> int:
    if ord("A") <= unit <= ord("Z"):
        return unit + 32
    return unit


def _upper_ascii_utf16(unit: int) -> int:
    if ord("a") <= unit <= ord("z"):
        return unit - 32
    return unit


def _utf16_units(value: str) -> list[int]:
    data = value.encode("utf-16le")
    return [int.from_bytes(data[i : i + 2], "little") for i in range(0, len(data), 2)]


def _case_utf16_units(value: str, uppercase: bool) -> tuple[int, ...]:
    if uppercase:
        return tuple(_upper_ascii_utf16(unit) for unit in _utf16_units(value))
    return tuple(_lower_ascii_utf16(unit) for unit in _utf16_units(value))


def _write_text(state: _Murmur3State, value: str, uppercase: bool) -> None:
    for unit in _utf16_units(value):
        state.write_u16(_upper_ascii_utf16(unit) if uppercase else _lower_ascii_utf16(unit))


@dataclass(frozen=True, slots=True)
class PreparedMixedText:
    text: str
    upper_units: tuple[int, ...]
    lower_units: tuple[int, ...]


@dataclass(frozen=True, slots=True)
class PreparedMurmurState:
    state: int
    processed_units: int
    tail_unit: int


@dataclass(frozen=True, slots=True)
class PreparedMixedState:
    upper: PreparedMurmurState
    lower: PreparedMurmurState


def prepare_mixed_text(value: str) -> PreparedMixedText:
    return PreparedMixedText(
        text=value,
        upper_units=_case_utf16_units(value, uppercase=True),
        lower_units=_case_utf16_units(value, uppercase=False),
    )


def prepare_mixed_state(value: PreparedMixedText | str) -> PreparedMixedState:
    prepared = value if isinstance(value, PreparedMixedText) else prepare_mixed_text(value)
    return PreparedMixedState(
        upper=_prepare_murmur_state(prepared.upper_units),
        lower=_prepare_murmur_state(prepared.lower_units),
    )


def _prepare_murmur_state(units: Iterable[int]) -> PreparedMurmurState:
    state = _Murmur3State()
    state.write_units(units)
    tail_unit = 0
    if state.tail_len:
        tail_unit = state.tail[0] | (state.tail[1] << 8)
    return PreparedMurmurState(
        state=state.state,
        processed_units=state.processed // 2,
        tail_unit=tail_unit,
    )


class MixedHashState:
    __slots__ = ("upper", "lower")

    def __init__(self) -> None:
        self.upper = _Murmur3State()
        self.lower = _Murmur3State()

    def clone(self) -> "MixedHashState":
        other = MixedHashState.__new__(MixedHashState)
        other.upper = self.upper.clone()
        other.lower = self.lower.clone()
        return other

    def write_text(self, value: str) -> None:
        self.write_prepared(prepare_mixed_text(value))

    def write_prepared(self, value: PreparedMixedText) -> None:
        self.upper.write_units(value.upper_units)
        self.lower.write_units(value.lower_units)

    def digest(self) -> int:
        return (self.upper.finish() << 32) | self.lower.finish()


def hash_mixed_prepared_parts(parts: Iterable[PreparedMixedText]) -> int:
    state = MixedHashState()
    for part in parts:
        state.write_prepared(part)
    return state.digest()


def hash_lower_case(value: str) -> int:
    state = _Murmur3State()
    for unit in _utf16_units(value):
        state.write_u16(_lower_ascii_utf16(unit))
    return state.finish()


def hash_upper_case(value: str) -> int:
    state = _Murmur3State()
    for unit in _utf16_units(value):
        state.write_u16(_upper_ascii_utf16(unit))
    return state.finish()


def hash_mixed(value: str) -> int:
    upper = _Murmur3State()
    lower = _Murmur3State()
    for unit in _utf16_units(value):
        upper.write_u16(_upper_ascii_utf16(unit))
        lower.write_u16(_lower_ascii_utf16(unit))
    return (upper.finish() << 32) | lower.finish()


def hash_mixed_parts(parts) -> int:
    upper = _Murmur3State()
    lower = _Murmur3State()
    for part in parts:
        _write_text(upper, str(part), uppercase=True)
        _write_text(lower, str(part), uppercase=False)
    return (upper.finish() << 32) | lower.finish()
