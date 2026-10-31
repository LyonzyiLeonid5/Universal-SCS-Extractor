"""Pure Python CityHash64 port of the algorithm bundled with TruckLib.HashFs."""
from __future__ import annotations

from struct import unpack_from

MASK = (1 << 64) - 1
K0 = 0xc3a5c85c97cb3127
K1 = 0xb492b66fbe98f273
K2 = 0x9ae16a3b2f90404f
K3 = 0xc949d7c7509e6557
KMUL = 0x9ddfea08eb382d69


def u(value: int) -> int:
    return value & MASK


def f32(data: bytes, offset: int = 0) -> int:
    return unpack_from('<I', data, offset)[0]


def f64(data: bytes, offset: int = 0) -> int:
    return unpack_from('<Q', data, offset)[0]


def rot(value: int, shift: int) -> int:
    value &= MASK
    return value if shift == 0 else u((value >> shift) | (value << (64 - shift)))


def mix(value: int) -> int:
    value = u(value)
    return value ^ (value >> 47)


def h16(a: int, b: int) -> int:
    a = u((a ^ b) * KMUL)
    a ^= a >> 47
    b = u((b ^ a) * KMUL)
    b ^= b >> 47
    return u(b * KMUL)


def weak(data: bytes, offset: int, a: int, b: int) -> tuple[int, int]:
    w, x, y, z = unpack_from('<QQQQ', data, offset)
    a = u(a + w)
    b = rot(u(b + a + z), 21)
    c = a
    a = u(a + x + y)
    b = u(b + rot(a, 44))
    return u(a + z), u(b + c)


def cityhash64(data: bytes) -> int:
    n = len(data)
    if n <= 16:
        if n > 8:
            a, b = f64(data), f64(data, n - 8)
            return h16(a, rot(u(b + n), n)) ^ b
        if n >= 4:
            return h16(n + (f32(data) << 3), f32(data, n - 4))
        if n:
            a, b, c = data[0], data[n >> 1], data[n - 1]
            return u(mix(((a + (b << 8)) * K2) ^ ((n + (c << 2)) * K3)) * K2)
        return K2
    if n <= 32:
        a, b = u(f64(data) * K1), f64(data, 8)
        c, d = u(f64(data, n - 8) * K2), u(f64(data, n - 16) * K0)
        return h16(u(rot(u(a - b), 43) + rot(c, 30) + d),
                   u(a + rot(b ^ K3, 20) - c + n))
    if n <= 64:
        z = f64(data, 24)
        a = u(f64(data) + u((n + f64(data, n - 16)) * K0))
        b, c = rot(u(a + z), 52), rot(a, 37)
        a = u(a + f64(data, 8)); c = u(c + rot(a, 7)); a = u(a + f64(data, 16))
        vf, vs = u(a + z), u(b + rot(a, 31) + c)
        a, z = u(f64(data, 16) + f64(data, n - 32)), f64(data, n - 8)
        b, c = rot(u(a + z), 52), rot(a, 37)
        a = u(a + f64(data, n - 24)); c = u(c + rot(a, 7)); a = u(a + f64(data, n - 16))
        wf, ws = u(a + z), u(b + rot(a, 31) + c)
        r = mix(u(u(vf + ws) * K2 + u(wf + vs) * K0))
        return u(mix(u(r * K0 + vs)) * K2)
    x = f64(data, n - 40)
    y = u(f64(data, n - 16) + f64(data, n - 56))
    z = h16(u(f64(data, n - 48) + n), f64(data, n - 24))
    v = weak(data, n - 64, n, z)
    w = weak(data, n - 32, u(y + K1), x)
    x = u(x * K1 + f64(data))
    offset = 0
    blocks = (n - 1) // 64
    for _ in range(blocks):
        x = u(rot(u(x + y + v[0] + f64(data, offset + 8)), 37) * K1)
        y = u(rot(u(y + v[1] + f64(data, offset + 48)), 42) * K1)
        x ^= w[1]
        y = u(y + v[0] + f64(data, offset + 40))
        z = u(rot(u(z + w[0]), 33) * K1)
        v = weak(data, offset, u(v[1] * K1), u(x + w[0]))
        w = weak(data, offset + 32, u(z + w[1]), u(y + f64(data, offset + 16)))
        x, z = z, x
        offset += 64
    return h16(u(h16(v[0], w[0]) + u(mix(y) * K1) + z), u(h16(v[1], w[1]) + x))
