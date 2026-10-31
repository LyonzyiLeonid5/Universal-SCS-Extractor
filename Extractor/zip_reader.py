"""ZIP reader using central-directory metadata, including damaged local headers."""
from __future__ import annotations

from dataclasses import dataclass
import logging
import mmap
from pathlib import Path
from struct import unpack_from
import zlib


@dataclass
class ZipEntry:
    filename: str
    compress_type: int
    CRC: int
    compress_size: int
    file_size: int
    header_offset: int


class ZipReader:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self._stream = self.path.open('rb')
        try:
            self.raw = mmap.mmap(self._stream.fileno(), 0, access=mmap.ACCESS_READ)
        except Exception:
            self._stream.close()
            raise
        self._is_aem = self.raw[:4] == b'AEM!'
        self.entries: dict[str, ZipEntry] = {}
        eocd = self._find_eocd()
        disk_entries, total_entries = unpack_from('<HH', self.raw, eocd + 8)
        directory_size, directory_offset = unpack_from('<II', self.raw, eocd + 12)
        if (disk_entries == 0xffff or total_entries == 0xffff or
                directory_size == 0xffffffff or directory_offset == 0xffffffff):
            locator = eocd - 20
            if locator < 0 or self.raw[locator:locator + 4] != b'PK\x06\x07':
                raise ValueError('ZIP64 End Of Central Directory locator is missing')
            zip64_offset = unpack_from('<Q', self.raw, locator + 8)[0]
            if self.raw[zip64_offset:zip64_offset + 4] != b'PK\x06\x06':
                raise ValueError('ZIP64 End Of Central Directory is missing')
            # Deliberately protected mods corrupt the record-size and disk fields.
            # The fixed entry-count/size/offset fields remain authoritative.
            total_entries = unpack_from('<Q', self.raw, zip64_offset + 32)[0]
            directory_size = unpack_from('<Q', self.raw, zip64_offset + 40)[0]
            directory_offset = unpack_from('<Q', self.raw, zip64_offset + 48)[0]
        self._read_directory(directory_offset, total_entries)

    def close(self) -> None:
        raw = getattr(self, 'raw', None)
        if raw is not None:
            raw.close()
            self.raw = None
        stream = getattr(self, '_stream', None)
        if stream is not None:
            stream.close()
            self._stream = None

    def __del__(self):
        try:
            self.close()
        except (BufferError, OSError):
            pass

    def _find_eocd(self) -> int:
        start = max(0, len(self.raw) - (22 + 0xffff))
        for offset in range(len(self.raw) - 22, start - 1, -1):
            if self.raw[offset:offset + 4] != b'PK\x05\x06':
                continue
            comment_length = unpack_from('<H', self.raw, offset + 20)[0]
            record_end = offset + 22 + comment_length
            if record_end == len(self.raw) or (self._is_aem and record_end <= len(self.raw)):
                return offset
        raise ValueError('No valid End Of Central Directory record found')

    @staticmethod
    def _zip64(extra: bytes, compressed: int, uncompressed: int,
               local_offset: int, disk: int) -> tuple[int, int, int]:
        pos = 0
        while pos + 4 <= len(extra):
            tag, length = unpack_from('<HH', extra, pos)
            body = extra[pos + 4:pos + 4 + length]
            if tag == 1:
                cursor = 0
                if uncompressed == 0xffffffff:
                    uncompressed = unpack_from('<Q', body, cursor)[0]; cursor += 8
                if compressed == 0xffffffff:
                    compressed = unpack_from('<Q', body, cursor)[0]; cursor += 8
                if local_offset == 0xffffffff:
                    local_offset = unpack_from('<Q', body, cursor)[0]; cursor += 8
                return compressed, uncompressed, local_offset
            pos += 4 + length
        raise ValueError('ZIP64 extra field is missing')

    def _read_directory(self, offset: int, count: int) -> None:
        position = offset
        for index in range(count):
            if self.raw[position:position + 4] != b'PK\x01\x02':
                raise ValueError(f'Invalid central directory entry {index}')
            (method, crc, compressed, uncompressed, name_len, extra_len,
             comment_len, disk, local_offset) = (
                unpack_from('<H', self.raw, position + 10)[0],
                unpack_from('<I', self.raw, position + 16)[0],
                unpack_from('<I', self.raw, position + 20)[0],
                unpack_from('<I', self.raw, position + 24)[0],
                unpack_from('<H', self.raw, position + 28)[0],
                unpack_from('<H', self.raw, position + 30)[0],
                unpack_from('<H', self.raw, position + 32)[0],
                unpack_from('<H', self.raw, position + 34)[0],
                unpack_from('<I', self.raw, position + 42)[0])
            cursor = position + 46
            name_bytes = self.raw[cursor:cursor + name_len]
            extra = self.raw[cursor + name_len:cursor + name_len + extra_len]
            if len(name_bytes) != name_len or len(extra) != extra_len:
                raise ValueError(f'Truncated central directory entry {index}')
            if (compressed == 0xffffffff or uncompressed == 0xffffffff or
                    local_offset == 0xffffffff or disk == 0xffff):
                compressed, uncompressed, local_offset = self._zip64(
                    extra, compressed, uncompressed, local_offset, disk)
            # ExtractorFork uses Encoding.UTF8.GetString, whose default fallback
            # replaces malformed sequences instead of rejecting the archive.
            name = name_bytes.decode('utf-8', 'replace')
            self.entries.setdefault(name, ZipEntry(name, method, crc, compressed,
                                                    uncompressed, local_offset))
            position = cursor + name_len + extra_len + comment_len

    def read(self, name: str) -> bytes:
        info = self.entries[name.lstrip('/')]
        offset = info.header_offset
        header = self.raw[offset:offset + 30]
        # SCS mods may replace even the four magic bytes (e.g. "HELLO").
        # The trusted central directory supplies the payload size and codec.
        if len(header) != 30:
            raise ValueError(f'Invalid local ZIP header: {name}')
        name_length, extra_length = unpack_from('<HH', header, 26)
        start = offset + 30 + name_length + extra_length
        end = start + info.compress_size
        if end > len(self.raw):
            raise ValueError(f'Truncated ZIP payload: {name}')
        compressed = self.raw[start:end]
        if info.compress_type == 0:
            data = compressed
        elif info.compress_type == 8:
            inflater = zlib.decompressobj(-15)
            recovery_margin = min(max(info.file_size // 100, 64 * 1024), 16 * 1024 * 1024)
            recovery_limit = info.file_size + recovery_margin
            try:
                data = inflater.decompress(compressed, recovery_limit + 1)
                if len(data) > recovery_limit or inflater.unconsumed_tail:
                    raise ValueError(f'Deflate output exceeds the recovery limit: {name}')
                data += inflater.flush()
            except zlib.error as exc:
                raise ValueError(f'Invalid deflate stream: {name}') from exc
            if len(data) > recovery_limit:
                raise ValueError(f'Deflate output exceeds the recovery limit: {name}')
            if len(data) < info.file_size:
                raise ValueError(f'Truncated ZIP entry: {name}')
            if len(data) != info.file_size or not inflater.eof:
                logging.getLogger('ZipReader').warning(
                    'Recovered malformed entry %s: declared %d bytes, recovered %d bytes, '
                    'deflate_end=%s', name, info.file_size, len(data), inflater.eof)
        else:
            raise ValueError(f'Unsupported ZIP compression method {info.compress_type}')
        if info.compress_type == 0 and len(data) != info.file_size:
            raise ValueError(f'ZIP entry size mismatch: {name}')
        return data

    def walk(self, starts: list[str] | None = None) -> list[str]:
        starts = starts or ['/']
        return sorted('/' + name for name in self.entries if not name.endswith('/')
                      and any(('/' + name).startswith(start.rstrip('/') + '/') or
                              ('/' + name) == start.rstrip('/') or start == '/'
                              for start in starts))
