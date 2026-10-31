"""Minimal AES-256 CBC decryptor for ScsC files; no third-party runtime."""
from __future__ import annotations


def _mul(a: int, b: int) -> int:
    result = 0
    for _ in range(8):
        if b & 1:
            result ^= a
        a = ((a << 1) ^ (0x11b if a & 0x80 else 0)) & 0xff
        b >>= 1
    return result


def _sbox() -> list[int]:
    box = []
    for value in range(256):
        inverse = next((n for n in range(1, 256) if _mul(value, n) == 1), 0)
        transformed = inverse
        for shift in range(1, 5):
            transformed ^= ((inverse << shift) | (inverse >> (8 - shift))) & 0xff
        box.append(transformed ^ 0x63)
    return box


SBOX = _sbox()
INVBOX = [SBOX.index(n) for n in range(256)]


def _keys(key: bytes) -> list[list[int]]:
    if len(key) != 32:
        raise ValueError('AES-256 requires a 32-byte key')
    words = [list(key[i:i + 4]) for i in range(0, 32, 4)]
    rcon = 1
    for i in range(8, 60):
        temp = words[-1].copy()
        if i % 8 == 0:
            temp = temp[1:] + temp[:1]
            temp = [SBOX[n] for n in temp]
            temp[0] ^= rcon
            rcon = _mul(rcon, 2)
        elif i % 8 == 4:
            temp = [SBOX[n] for n in temp]
        words.append([a ^ b for a, b in zip(words[i - 8], temp)])
    return [[n for word in words[i:i + 4] for n in word] for i in range(0, 60, 4)]


def _decrypt_block(block: bytes, round_keys: list[list[int]]) -> bytes:
    state = [n ^ k for n, k in zip(block, round_keys[14])]
    for round_no in range(13, -1, -1):
        for row in range(1, 4):
            values = [state[4 * col + row] for col in range(4)]
            for col in range(4):
                state[4 * col + row] = values[(col - row) % 4]
        state = [INVBOX[n] for n in state]
        state = [n ^ k for n, k in zip(state, round_keys[round_no])]
        if round_no:
            for col in range(4):
                a = state[col * 4:col * 4 + 4]
                state[col * 4:col * 4 + 4] = [
                    _mul(a[0], 14) ^ _mul(a[1], 11) ^ _mul(a[2], 13) ^ _mul(a[3], 9),
                    _mul(a[0], 9) ^ _mul(a[1], 14) ^ _mul(a[2], 11) ^ _mul(a[3], 13),
                    _mul(a[0], 13) ^ _mul(a[1], 9) ^ _mul(a[2], 14) ^ _mul(a[3], 11),
                    _mul(a[0], 11) ^ _mul(a[1], 13) ^ _mul(a[2], 9) ^ _mul(a[3], 14),
                ]
    return bytes(state)


def decrypt_cbc(ciphertext: bytes, key: bytes, iv: bytes) -> bytes:
    if len(iv) != 16 or len(ciphertext) % 16:
        raise ValueError('Invalid AES-CBC IV or ciphertext length')
    keys = _keys(key)
    output = bytearray()
    previous = iv
    for offset in range(0, len(ciphertext), 16):
        block = ciphertext[offset:offset + 16]
        output.extend(a ^ b for a, b in zip(_decrypt_block(block, keys), previous))
        previous = block
    if not output:
        raise ValueError('Empty AES-CBC payload')
    padding = output[-1]
    if not 1 <= padding <= 16 or output[-padding:] != bytes([padding]) * padding:
        raise ValueError('Invalid AES-CBC padding')
    return bytes(output[:-padding])
