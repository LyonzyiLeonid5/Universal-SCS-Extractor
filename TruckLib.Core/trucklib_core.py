"""Python port of TruckLib.Core 0.3.0."""
from __future__ import annotations
from abc import ABC, abstractmethod
from collections.abc import Iterable, MutableSequence
from math import asin, atan2, degrees, sqrt
from pathlib import Path, PurePosixPath
from struct import pack, unpack
from typing import BinaryIO, Generic, TypeVar

class UnsupportedVersionException(Exception): pass

class IBinarySerializable(ABC):
    @abstractmethod
    def deserialize(self, stream: BinaryIO, version: int | None = None) -> None: ...
    @abstractmethod
    def serialize(self, stream: BinaryIO) -> None: ...

class IFileSystem(ABC):
    directory_separator = '/'
    @abstractmethod
    def file_exists(self, path: str) -> bool: ...
    @abstractmethod
    def directory_exists(self, path: str) -> bool: ...
    @abstractmethod
    def get_files(self, path: str) -> list[str]: ...
    @abstractmethod
    def get_parent(self, path: str) -> str | None: ...
    @abstractmethod
    def open(self, path: str) -> BinaryIO: ...
    def read_all_bytes(self, path: str) -> bytes:
        with self.open(path) as stream: return stream.read()
    def read_all_text(self, path: str, encoding: str = 'utf-8') -> str:
        return self.read_all_bytes(path).decode(encoding)

class DiskFileSystem(IFileSystem):
    directory_separator = '\\' if __import__('os').name == 'nt' else '/'
    def file_exists(self, path: str) -> bool: return Path(path).is_file()
    def directory_exists(self, path: str) -> bool: return Path(path).is_dir()
    def get_files(self, path: str) -> list[str]: return [str(x) for x in Path(path).iterdir() if x.is_file()]
    def get_parent(self, path: str) -> str | None:
        parent = Path(path).parent
        return None if parent == Path(path) else str(parent.resolve())
    def open(self, path: str) -> BinaryIO: return Path(path).open('rb')

def normalize_path(path: str) -> str:
    path = path.replace('\\', '/'); parts = []
    for part in path.split('/'):
        if part in ('', '.'): continue
        if part == '..':
            if not parts: raise ValueError(f'Path escapes archive root: {path!r}')
            parts.pop()
        else: parts.append(part)
    return '/' + '/'.join(parts)

def parent_path(path: str) -> str: return str(PurePosixPath(normalize_path(path)).parent)
def read_exact(stream: BinaryIO, size: int) -> bytes:
    if size < 0: raise ValueError('Negative read size')
    data = stream.read(size)
    if len(data) != size: raise EOFError(f'Expected {size} bytes, got {len(data)}')
    return data

class FlagField:
    def __init__(self, bits: int = 0): self.bits = bits & 0xFFFFFFFF
    @staticmethod
    def _check(value: int, low: int, high: int) -> None:
        if value < low or value > high: raise IndexError(value)
    def __getitem__(self, index: int) -> bool:
        self._check(index, 0, 31); return bool(self.bits & (1 << index))
    def __setitem__(self, index: int, value: bool) -> None:
        self._check(index, 0, 31); self.bits = self.bits | (1 << index) if value else self.bits & ~(1 << index)
    def get_byte(self, index: int) -> int:
        self._check(index, 0, 3); return (self.bits >> index * 8) & 255
    def set_byte(self, index: int, value: int) -> None:
        self._check(index, 0, 3)
        if not 0 <= value <= 255: raise ValueError(value)
        shift = index * 8; self.bits = (self.bits & ~(255 << shift)) | (value << shift)
    def to_bool_array(self) -> list[bool]: return [self[i] for i in range(32)]
    def get_bit_string(self, start: int, length: int) -> int:
        if length == 0: return 0
        self._check(start, 0, 31); self._check(length, 0, 31)
        if start + length > 32: raise IndexError
        return (self.bits >> start) & ((1 << length) - 1)
    def set_bit_string(self, start: int, length: int, value: int) -> None:
        if length == 0: return
        self._check(start, 0, 31); self._check(length, 0, 31)
        if start + length > 32: raise IndexError
        mask = (1 << length) - 1; self.bits = (self.bits & ~(mask << start)) | ((value & mask) << start)
    def __str__(self): return f'{self.bits:032b}'
    def __eq__(self, other): return isinstance(other, FlagField) and self.bits == other.bits
    def __hash__(self): return hash(self.bits)

class Nibble(int):
    MIN_VALUE, MAX_VALUE = 0, 15
    def __new__(cls, value: int = 0):
        if not 0 <= value <= 15: raise ValueError('Value must be between 0 and 15.')
        return int.__new__(cls, value)
    def __add__(self, other): return Nibble((int(self) + int(other)) & 15)
    def __radd__(self, other): return self + other
    def __sub__(self, other): return Nibble((int(self) - int(other)) & 15)
    def __rsub__(self, other): return Nibble((int(other) - int(self)) & 15)

class Token:
    CHARACTER_SET = '\0' + '0123456789abcdefghijklmnopqrstuvwxyz_'; MAX_LENGTH = 12
    def __init__(self, value: int | str = 0):
        self.value = self.string_to_token(value) if isinstance(value, str) else int(value)
        if self.value < 0: raise ValueError("Value can't be less than zero.")
    @property
    def string(self): return self.token_to_string(self.value)
    @string.setter
    def string(self, value): self.value = self.string_to_token(value)
    @classmethod
    def is_valid_token(cls, value): return len(value) <= cls.MAX_LENGTH and all(c in cls.CHARACTER_SET for c in value)
    @classmethod
    def string_to_token(cls, value):
        if not value: return 0
        if not cls.is_valid_token(value): raise ValueError('Input is not a valid token.')
        return sum(cls.CHARACTER_SET.index(c) * 38 ** i for i, c in enumerate(value.lower()))
    @classmethod
    def token_to_string(cls, value):
        if value == 0: return ''
        chars = []
        while value: value, rem = divmod(value, 38); chars.append(cls.CHARACTER_SET[rem])
        return ''.join(chars)
    def deserialize(self, stream, version=None): self.value, = unpack('<Q', read_exact(stream, 8))
    def serialize(self, stream): stream.write(pack('<Q', self.value))
    def __str__(self): return self.string
    def __int__(self): return self.value
    def __eq__(self, other):
        if isinstance(other, Token): return self.value == other.value
        if isinstance(other, str): return self.string == other
        return isinstance(other, int) and other >= 0 and self.value == other
    def __hash__(self): return hash(self.value)

T = TypeVar('T')
class LimitedList(MutableSequence[T], Generic[T]):
    def __init__(self, max_capacity: int, collection: Iterable[T] = ()):
        if max_capacity <= 0: raise ValueError('max_capacity must be positive')
        self.max_capacity = max_capacity; self._items = list(collection)
        if len(self._items) > max_capacity: raise ValueError('collection is too large')
    def __len__(self): return len(self._items)
    def __getitem__(self, index): return self._items[index]
    def __setitem__(self, index, value): self._items[index] = value
    def __delitem__(self, index): del self._items[index]
    def insert(self, index, value):
        if len(self) >= self.max_capacity or index >= self.max_capacity: raise IndexError('List is full.')
        self._items.insert(index, value)

def push(array, item): return ([] if array is None else list(array)) + [item]
def bool_to_byte(value): return int(value)
def _floats(stream, count): return unpack('<' + 'f' * count, read_exact(stream, count * 4))
def read_vector2(stream): return _floats(stream, 2)
def read_vector3(stream): return _floats(stream, 3)
def read_vector4(stream): return _floats(stream, 4)
def read_matrix4x4(stream): return _floats(stream, 16)
def read_quaternion(stream):
    w, x, y, z = _floats(stream, 4); return x, y, z, w
def write_vector(stream, value):
    vals = tuple(value); stream.write(pack('<' + 'f' * len(vals), *vals))
def write_quaternion(stream, value):
    x, y, z, w = value; write_vector(stream, (w, x, y, z))
def read_pascal_string(stream):
    length, = unpack('<Q', read_exact(stream, 8)); return read_exact(stream, length).decode('utf-8')
def write_pascal_string(stream, value):
    data = value.encode('utf-8'); stream.write(pack('<Q', len(data)) + data)
def read_color(stream): return tuple(read_exact(stream, 4))
def write_color(stream, value): stream.write(bytes(value))
def quaternion_to_euler(q, as_degrees=False):
    x, y, z, w = q; norm = sqrt(x*x+y*y+z*z+w*w); x,y,z,w = x/norm,y/norm,z/norm,w/norm; test=x*w-y*z
    if test > .4995: result=(1.5707963267948966,2*atan2(y,x),0.0)
    elif test < -.4995: result=(-1.5707963267948966,-2*atan2(y,x),0.0)
    else: result=(asin(2*(w*x-y*z)),atan2(2*w*y+2*z*x,1-2*(x*x+y*y)),atan2(2*w*z+2*x*y,1-2*(z*z+x*x)))
    return tuple(map(degrees,result)) if as_degrees else result

__all__ = [name for name in globals() if not name.startswith('_')]
