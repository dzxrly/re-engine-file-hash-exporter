from __future__ import annotations

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

    def finish(self) -> int:
        if self.tail_len:
            k = 0
            for index in range(self.tail_len):
                k |= self.tail[index] << (index * 8)
            self.state ^= _murmur3_calc_k(k)
        return _murmur3_finish(self.state, self.processed)


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
