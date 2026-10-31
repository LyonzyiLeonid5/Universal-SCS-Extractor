"""Safe in-place manifest and compression tools for HashFS archives.

The original C# plugins patched payloads and table rows in place.  Rebuilding
the archive into a sibling temporary file is both simpler and safer: a larger
compressed stream cannot overwrite the next payload, and the source archive
is replaced only after the complete output has been validated.
"""
from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path
import re
import shutil
import struct
import tempfile
import zlib

from trucklib_hashfs import HashFsReader, hash_path
from trucklib_sii import decode as decode_sii
from zip_reader import ZipReader


@dataclass
class V1Entry:
    hash: int
    offset: int
    flags: int
    crc: int
    size: int
    compressed_size: int

    @property
    def is_directory(self) -> bool:
        return bool(self.flags & 1)

    @property
    def is_compressed(self) -> bool:
        return bool(self.flags & 2)

    @property
    def stored_size(self) -> int:
        return self.compressed_size if self.is_compressed else self.size


@dataclass
class RewriteResult:
    path: Path
    entries: int
    changed_entries: int
    old_size: int
    new_size: int


@dataclass
class ManifestResult(RewriteResult):
    removed_versions: int
    replacements: int


def _read_v1_table(path: Path) -> tuple[bytearray, int, list[V1Entry]]:
    file_size = path.stat().st_size
    with path.open('rb') as stream:
        header = bytearray(stream.read(64))
        if len(header) < 24 or header[:4] != b'SCS#':
            raise ValueError(f'{path.name}: not a HashFS archive')
        version, = struct.unpack_from('<H', header, 4)
        if version != 1:
            raise ValueError(f'{path.name}: operation currently supports HashFS v1 only')
        if header[8:12] != b'CITY':
            raise ValueError(f'{path.name}: unsupported HashFS hash method')
        count, table_offset = struct.unpack_from('<IQ', header, 12)
        table_size = count * 32
        if table_offset + table_size > file_size:
            raise ValueError(f'{path.name}: truncated HashFS v1 entry table')
        stream.seek(table_offset)
        table = stream.read(table_size)
    if len(table) != table_size:
        raise ValueError(f'{path.name}: truncated HashFS v1 entry table')
    entries = [V1Entry(*struct.unpack_from('<QQIIII', table, index * 32))
               for index in range(count)]
    for entry in entries:
        if entry.offset + entry.stored_size > file_size:
            raise ValueError(
                f'{path.name}: entry {entry.hash:016x} payload is out of bounds')
    return header, table_offset, entries


def _align(stream, multiple: int = 16) -> int:
    position = stream.tell()
    aligned = (position + multiple - 1) // multiple * multiple
    if aligned != position:
        stream.write(bytes(aligned - position))
    return aligned


def _copy_payload(source, target, offset: int, length: int) -> None:
    source.seek(offset)
    remaining = length
    while remaining:
        block = source.read(min(1024 * 1024, remaining))
        if not block:
            raise EOFError('Unexpected end of HashFS payload')
        target.write(block)
        remaining -= len(block)


def _read_payload(source, entry: V1Entry) -> bytes:
    source.seek(entry.offset)
    data = source.read(entry.stored_size)
    if len(data) != entry.stored_size:
        raise EOFError(f'Truncated entry {entry.hash:016x}')
    if not entry.is_compressed:
        return data
    try:
        result = zlib.decompress(data)
    except zlib.error as exc:
        raise ValueError(f'Invalid zlib entry {entry.hash:016x}') from exc
    if len(result) != entry.size:
        raise ValueError(
            f'Entry {entry.hash:016x} expands to {len(result)}, expected {entry.size}')
    return result


def _rewrite_v1(path: str | Path, *, compress_files: bool = False,
                transform_hash: int | None = None,
                transform=None) -> RewriteResult:
    archive = Path(path)
    header, _old_table_offset, entries = _read_v1_table(archive)
    old_size = archive.stat().st_size
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=archive.stem + '.', suffix='.tmp', dir=archive.parent)
    os.close(descriptor)
    temporary = Path(temporary_name)
    changed = 0
    new_rows: list[V1Entry] = []
    try:
        with archive.open('rb') as source, temporary.open('w+b') as target:
            target.write(header)
            for entry in entries:
                _align(target)
                new_offset = target.tell()
                needs_transform = transform_hash is not None and entry.hash == transform_hash
                should_compress = (compress_files and not entry.is_directory
                                   and not entry.is_compressed)
                if needs_transform or should_compress:
                    content = _read_payload(source, entry)
                    if needs_transform:
                        content = transform(content)
                    keep_compressed = entry.is_compressed or should_compress
                    stored = zlib.compress(content, level=9) if keep_compressed else content
                    target.write(stored)
                    flags = entry.flags
                    if compress_files:
                        flags = {4: 0, 5: 1}.get(flags, flags)
                    flags = (flags | 2) if keep_compressed else (flags & ~2)
                    crc = (zlib.crc32(content) & 0xffffffff) if entry.crc else 0
                    new_rows.append(V1Entry(
                        entry.hash, new_offset, flags, crc, len(content),
                        len(stored) if keep_compressed else entry.compressed_size))
                    changed += 1
                else:
                    _copy_payload(source, target, entry.offset, entry.stored_size)
                    flags = ({4: 0, 5: 1}.get(entry.flags, entry.flags)
                             if compress_files else entry.flags)
                    new_rows.append(V1Entry(
                        entry.hash, new_offset, flags, entry.crc,
                        entry.size, entry.compressed_size))

            table_offset = _align(target)
            for row in new_rows:
                target.write(struct.pack(
                    '<QQIIII', row.hash, row.offset, row.flags, row.crc,
                    row.size, row.compressed_size))
            target.write(b'Universal SCS Extractor')
            new_size = target.tell()
            struct.pack_into('<IQ', header, 12, len(new_rows), table_offset)
            target.seek(0)
            target.write(header)
            target.flush()
            os.fsync(target.fileno())

        # Validate the complete table and every newly compressed payload before
        # replacing the user's archive.
        check = HashFsReader(temporary)
        try:
            if check.version != 1 or check.entry_count != len(entries):
                raise ValueError('HashFS verification failed after rewriting')
            for original, rewritten in zip(entries, new_rows):
                if ((compress_files and not original.is_directory
                     and not original.is_compressed)
                        or (transform_hash is not None
                            and original.hash == transform_hash)):
                    check_entry = next(
                        (item for item in check.entries.values()
                         if item.hash == rewritten.hash), None)
                    if check_entry is None:
                        raise ValueError(
                            f'Entry {rewritten.hash:016x} is missing after rewriting')
                    check.read_entry(check_entry)
        finally:
            check.close()
        os.replace(temporary, archive)
        return RewriteResult(archive, len(entries), changed, old_size, new_size)
    except Exception:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass
        raise


def _compress_v1_without_compaction(path: Path) -> RewriteResult:
    header, table_offset, entries = _read_v1_table(path)
    old_size = path.stat().st_size
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=path.stem + '.', suffix='.tmp', dir=path.parent)
    os.close(descriptor)
    temporary = Path(temporary_name)
    changed = 0
    try:
        shutil.copyfile(path, temporary)
        with path.open('rb') as source, temporary.open('r+b') as target:
            for index, entry in enumerate(entries):
                flags = {4: 0, 5: 1}.get(entry.flags, entry.flags)
                new_offset = entry.offset
                new_compressed_size = entry.compressed_size
                if not entry.is_directory and not entry.is_compressed:
                    content = _read_payload(source, entry)
                    stored = zlib.compress(content, level=9)
                    if len(stored) <= entry.size:
                        target.seek(entry.offset)
                        target.write(stored)
                        target.write(bytes(entry.size - len(stored)))
                    else:
                        target.seek(0, os.SEEK_END)
                        new_offset = _align(target)
                        target.write(stored)
                    flags |= 2
                    new_compressed_size = len(stored)
                    changed += 1
                row_offset = table_offset + index * 32
                target.seek(row_offset + 8)
                target.write(struct.pack('<Q', new_offset))
                target.seek(row_offset + 16)
                target.write(struct.pack('<I', flags))
                target.seek(row_offset + 28)
                target.write(struct.pack('<I', new_compressed_size))
            target.flush()
            os.fsync(target.fileno())
            target.seek(0, os.SEEK_END)
            new_size = target.tell()

        check = HashFsReader(temporary)
        try:
            if check.version != 1 or check.entry_count != len(entries):
                raise ValueError('HashFS verification failed after compression')
            for entry in check.entries.values():
                if not entry.is_directory:
                    check.read_entry(entry)
        finally:
            check.close()
        os.replace(temporary, path)
        return RewriteResult(path, len(entries), changed, old_size, new_size)
    except Exception:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass
        raise


def compress_hashfs_v1(path: str | Path, *, compact: bool = True) -> RewriteResult:
    """Compress all currently uncompressed non-directory v1 entries."""
    archive = Path(path)
    _header, _table_offset, entries = _read_v1_table(archive)
    hashes = [entry.hash for entry in entries]
    if len(hashes) != len(set(hashes)):
        raise ValueError(
            f'{archive.name}: duplicate hashes prevent safe compression')
    return (_rewrite_v1(path, compress_files=True) if compact
            else _compress_v1_without_compaction(archive))


def _manifest_transform(archive: Path,
                        replacements: list[tuple[str, str]] | None):
    removed = 0
    replaced = 0
    replacement_pairs = list(replacements or [])

    def transform(data: bytes) -> bytes:
        nonlocal removed, replaced
        decoded = decode_sii(data)
        try:
            text = decoded.decode('utf-8-sig')
        except UnicodeDecodeError as exc:
            raise ValueError(f'{archive.name}: manifest.sii is not valid UTF-8') from exc
        if not text.lstrip().startswith('SiiNunit'):
            raise ValueError(f'{archive.name}: manifest.sii does not start with SiiNunit')
        pattern = re.compile(
            r'^[ \t]*compatible_versions\[\]\s*:\s*"[^"]*"\s*;?[ \t]*(?:\r?\n)?',
            flags=re.IGNORECASE | re.MULTILINE)
        text, removed = pattern.subn('', text)
        for old, new in replacement_pairs:
            occurrences = text.count(old)
            if occurrences:
                text = text.replace(old, new)
                replaced += occurrences
        return text.encode('utf-8')

    def counts() -> tuple[int, int]:
        return removed, replaced

    return transform, counts


def edit_manifest_v1(path: str | Path,
                     replacements: list[tuple[str, str]] | None = None
                     ) -> ManifestResult:
    """Remove compatible_versions[] and optionally replace v1 manifest text."""
    archive = Path(path)
    header, _table_offset, entries = _read_v1_table(archive)
    salt, = struct.unpack_from('<H', header, 6)
    manifest_hash = hash_path('/manifest.sii', salt)
    matches = [entry for entry in entries if entry.hash == manifest_hash]
    if not matches:
        raise FileNotFoundError(f'{archive.name}: /manifest.sii was not found')
    if len(matches) != 1:
        raise ValueError(f'{archive.name}: duplicate /manifest.sii entries')
    transform, counts = _manifest_transform(archive, replacements)

    result = _rewrite_v1(
        archive, transform_hash=manifest_hash, transform=transform)
    removed, replaced = counts()
    return ManifestResult(
        result.path, result.entries, result.changed_entries,
        result.old_size, result.new_size, removed, replaced)


def edit_manifest_v2(path: str | Path,
                     replacements: list[tuple[str, str]] | None = None
                     ) -> ManifestResult:
    """Safely relocate a modified manifest and v2 metadata table."""
    archive = Path(path)
    old_size = archive.stat().st_size
    data = archive.read_bytes() if old_size < 64 * 1024 * 1024 else None
    with archive.open('rb') as source:
        header = bytearray(source.read(64))
        if len(header) < 45 or header[:4] != b'SCS#':
            raise ValueError(f'{archive.name}: not a HashFS archive')
        version, salt = struct.unpack_from('<HH', header, 4)
        if version != 2:
            raise ValueError(f'{archive.name}: not a HashFS v2 archive')
        entry_count = struct.unpack_from('<I', header, 12)[0]
        entry_length, metadata_count, metadata_length = struct.unpack_from(
            '<III', header, 16)
        entry_start, metadata_start = struct.unpack_from('<QQ', header, 28)

        def read_range(offset: int, length: int) -> bytes:
            if offset < 0 or length < 0 or offset + length > old_size:
                raise ValueError(f'{archive.name}: table or payload is out of bounds')
            if data is not None:
                return data[offset:offset + length]
            source.seek(offset)
            value = source.read(length)
            if len(value) != length:
                raise EOFError(f'{archive.name}: truncated archive data')
            return value

        def decode_table(offset: int, stored: int, expected: int,
                         label: str) -> bytes:
            raw = read_range(offset, stored)
            if stored == expected:
                return raw
            try:
                decoded = zlib.decompress(raw)
            except zlib.error as exc:
                raise ValueError(f'{archive.name}: invalid {label}') from exc
            if len(decoded) != expected:
                raise ValueError(f'{archive.name}: incorrect {label} size')
            return decoded

        entry_table = decode_table(
            entry_start, entry_length, entry_count * 16, 'entry table')
        metadata = bytearray(decode_table(
            metadata_start, metadata_length, metadata_count * 4,
            'metadata table'))
        manifest_hash = hash_path('/manifest.sii', salt)
        rows = [struct.unpack_from('<QIHH', entry_table, index * 16)
                for index in range(entry_count)]
        matches = [row for row in rows if row[0] == manifest_hash]
        if not matches:
            raise FileNotFoundError(f'{archive.name}: /manifest.sii was not found')
        if len(matches) != 1:
            raise ValueError(f'{archive.name}: duplicate /manifest.sii entries')
        _hashed, descriptor_index, descriptor_count, _flags = matches[0]
        if descriptor_index * 4 + descriptor_count * 4 > len(metadata):
            raise ValueError(f'{archive.name}: manifest metadata is out of bounds')
        primary = None
        for index in range(descriptor_count):
            descriptor = struct.unpack_from(
                '<I', metadata, (descriptor_index + index) * 4)[0]
            body_index, kind = descriptor & 0x00ffffff, descriptor >> 24
            if kind in (128, 129):
                primary = body_index * 4
                break
        if primary is None or primary + 16 > len(metadata):
            raise ValueError(f'{archive.name}: unsupported manifest metadata')
        compressed_field, size_field, unknown, block = struct.unpack_from(
            '<IIII', metadata, primary)
        compression_flags = compressed_field >> 28
        compressed_size = compressed_field & 0x0fffffff
        uncompressed_size = size_field & 0x0fffffff
        stored_size = compressed_size if compression_flags else uncompressed_size
        raw = read_range(block * 16, stored_size)
        try:
            if compression_flags == 1:
                content = zlib.decompress(raw)
            elif compression_flags == 2:
                content = zlib.decompress(raw, -zlib.MAX_WBITS)
            elif compression_flags == 0:
                content = raw
            else:
                raise ValueError(
                    f'{archive.name}: unsupported manifest compression flag '
                    f'{compression_flags}')
        except zlib.error as exc:
            raise ValueError(f'{archive.name}: invalid compressed manifest') from exc
        if len(content) != uncompressed_size:
            raise ValueError(f'{archive.name}: incorrect manifest size')
        transform, counts = _manifest_transform(archive, replacements)
        updated = transform(content)
        if compression_flags == 1:
            stored_updated = zlib.compress(updated, level=9)
        elif compression_flags == 2:
            compressor = zlib.compressobj(level=9, wbits=-zlib.MAX_WBITS)
            stored_updated = compressor.compress(updated) + compressor.flush()
        else:
            stored_updated = updated
        if len(stored_updated) > 0x0fffffff or len(updated) > 0x0fffffff:
            raise ValueError(f'{archive.name}: updated manifest is too large')

    descriptor, temporary_name = tempfile.mkstemp(
        prefix=archive.stem + '.', suffix='.tmp', dir=archive.parent)
    os.close(descriptor)
    temporary = Path(temporary_name)
    try:
        shutil.copyfile(archive, temporary)
        with temporary.open('r+b') as target:
            target.seek(0, os.SEEK_END)
            payload_offset = _align(target)
            target.write(stored_updated)
            struct.pack_into(
                '<IIII', metadata, primary,
                (compression_flags << 28) | len(stored_updated),
                (size_field & 0xf0000000) | len(updated), unknown,
                payload_offset // 16)
            encoded_metadata = (
                bytes(metadata) if metadata_length == metadata_count * 4
                else zlib.compress(bytes(metadata), level=9))
            metadata_offset = _align(target)
            target.write(encoded_metadata)
            struct.pack_into('<I', header, 24, len(encoded_metadata))
            struct.pack_into('<Q', header, 36, metadata_offset)
            target.seek(0)
            target.write(header)
            target.flush()
            os.fsync(target.fileno())
            target.seek(0, os.SEEK_END)
            new_size = target.tell()

        check = HashFsReader(temporary)
        try:
            if check.read('/manifest.sii') != updated:
                raise ValueError('Manifest verification failed after rewriting')
        finally:
            check.close()
        os.replace(temporary, archive)
        removed, replaced = counts()
        return ManifestResult(
            archive, entry_count, 1, old_size, new_size, removed, replaced)
    except Exception:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass
        raise


def _raw_deflate(data: bytes) -> bytes:
    compressor = zlib.compressobj(level=9, wbits=-zlib.MAX_WBITS)
    return compressor.compress(data) + compressor.flush()


def _rewrite_zip_or_aem(archive: Path, records: list[tuple[str, bytes, int]],
                        *, aem: bool) -> int:
    """Atomically write conventional ZIP or the AEM local-header variant."""
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=archive.stem + '.', suffix='.tmp', dir=archive.parent)
    os.close(descriptor)
    temporary = Path(temporary_name)
    old_size = archive.stat().st_size
    try:
        rows: list[tuple[str, int, int, int, int, int]] = []
        with temporary.open('wb') as target:
            for name, content, method in records:
                name_bytes = name.encode('utf-8')
                if len(name_bytes) > 0xffff:
                    raise ValueError(f'{archive.name}: ZIP entry name is too long: {name}')
                directory = name.endswith('/')
                payload = (content if method == 0 else _raw_deflate(content))
                if max(len(payload), len(content), target.tell()) >= 0xffffffff:
                    raise ValueError(f'{archive.name}: ZIP64 manifest rewrite is not supported')
                offset = target.tell()
                crc = zlib.crc32(content) & 0xffffffff
                if aem:
                    target.write(b'AEM!' + bytes(22))
                    if payload:
                        target.write(struct.pack('<I', len(name_bytes)))
                        target.write(name_bytes)
                    else:
                        target.write(b'AEM!' + bytes(len(name_bytes)))
                    flags = 1
                else:
                    flags = 0x800
                    target.write(struct.pack(
                        '<4sHHHHHIIIHH', b'PK\x03\x04', 20, flags, method,
                        0, 0, crc, len(payload), len(content), len(name_bytes), 0))
                    target.write(name_bytes)
                target.write(payload)
                rows.append((name, method, crc, len(payload), len(content), offset))

            central_start = target.tell()
            for name, method, crc, compressed, uncompressed, offset in rows:
                name_bytes = name.encode('utf-8')
                directory = name.endswith('/')
                if aem:
                    made_by, needed, flags = 31, (10 if method == 0 else 20), 1
                    central_crc = struct.unpack('<I', b'AEM!')[0]
                    external = 0x10 if directory else 0x20
                else:
                    made_by, needed, flags = (3 << 8) | 20, 20, 0x800
                    central_crc = crc
                    external = ((0o40775 << 16) | 0x10 if directory
                                else (0o100664 << 16))
                target.write(struct.pack(
                    '<4sHHHHHHIIIHHHHHII', b'PK\x01\x02', made_by, needed,
                    flags, method, 0, 0, central_crc, compressed, uncompressed,
                    len(name_bytes), 0, 0, 0, 0, external, offset))
                target.write(name_bytes)
            central_size = target.tell() - central_start
            if len(rows) >= 0xffff or central_size >= 0xffffffff or central_start >= 0xffffffff:
                raise ValueError(f'{archive.name}: ZIP64 manifest rewrite is not supported')
            target.write(struct.pack(
                '<4sHHHHIIH', b'PK\x05\x06', 0, 0, len(rows), len(rows),
                central_size, central_start, 0))
            target.flush()
            os.fsync(target.fileno())
        check = ZipReader(temporary)
        try:
            if check.read('manifest.sii') != next(data for name, data, _ in records
                                                   if name == 'manifest.sii'):
                raise ValueError('Manifest verification failed after ZIP rewrite')
        finally:
            check.close()
        os.replace(temporary, archive)
        return old_size
    except Exception:
        temporary.unlink(missing_ok=True)
        raise


def edit_manifest_zip_or_aem(path: str | Path,
                             replacements: list[tuple[str, str]] | None = None
                             ) -> ManifestResult:
    """Edit a ZIP/AEM manifest while retaining its archive family."""
    archive = Path(path)
    reader = ZipReader(archive)
    try:
        manifest_name = next((name for name in reader.entries
                              if name.casefold() == 'manifest.sii'), None)
        if manifest_name is None:
            raise FileNotFoundError(f'{archive.name}: /manifest.sii was not found')
        transform, counts = _manifest_transform(archive, replacements)
        records: list[tuple[str, bytes, int]] = []
        for name, entry in reader.entries.items():
            data = reader.read(name)
            if name == manifest_name:
                data = transform(data)
            records.append((name, data, entry.compress_type))
        is_aem = reader._is_aem
    finally:
        # Windows does not permit replacing an archive while its mmap is open.
        reader.close()
    try:
        old_size = _rewrite_zip_or_aem(archive, records, aem=is_aem)
        removed, replaced = counts()
        return ManifestResult(archive, len(records), 1, old_size,
                              archive.stat().st_size, removed, replaced)
    except Exception:
        raise


def edit_manifest(path: str | Path,
                  replacements: list[tuple[str, str]] | None = None
                  ) -> ManifestResult:
    archive = Path(path)
    with archive.open('rb') as stream:
        prefix = stream.read(6)
    if len(prefix) < 4 or prefix[:4] != b'SCS#':
        return edit_manifest_zip_or_aem(archive, replacements)
    version, = struct.unpack_from('<H', prefix, 4)
    if version == 1:
        return edit_manifest_v1(archive, replacements)
    if version == 2:
        return edit_manifest_v2(archive, replacements)
    raise ValueError(f'{archive.name}: unsupported HashFS version {version}')


def parse_replacements(values: list[str]) -> list[tuple[str, str]]:
    result: list[tuple[str, str]] = []
    for value in values:
        for item in value.split(';'):
            item = item.strip()
            if not item:
                continue
            if '=' not in item:
                raise ValueError(
                    f'Invalid manifest replacement {item!r}; expected OLD=NEW')
            old, new = item.split('=', 1)
            if not old:
                raise ValueError('Manifest replacement OLD value may not be empty')
            result.append((old, new))
    return result

