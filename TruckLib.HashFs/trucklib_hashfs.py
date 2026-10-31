"""Python reader for TruckLib.HashFs 0.2.6 archives.

The on-disk parser is self-contained. No CLR or Python wheels are required.
"""
from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from struct import pack, unpack, unpack_from
from io import BytesIO
from enum import IntEnum
import mmap
import zlib

from cityhash import cityhash64
from texture import TextureMeta, tobj as make_tobj, dds as make_dds, texture_metadata, DdsFile


@lru_cache(maxsize=262144)
def hash_path(path: str, salt: int = 0) -> int:
    path = path.removeprefix('/')
    if salt:
        path = str(salt) + path
    return cityhash64(path.encode('utf-8'))


@dataclass
class Entry:
    hash: int
    offset: int
    size: int
    compressed_size: int
    is_directory: bool
    is_compressed: bool
    kind: int = 128
    flags: int = 0
    texture: TextureMeta | None = None
    raw_deflate: bool = False


class HashFsError(ValueError):
    pass

class TexturePackingException(HashFsError):
    pass


class HashFsReader:
    def __init__(self, source: str | Path, *, salt: int | None = None,
                 table_at_end: bool = False):
        self.source = Path(source)
        # Split mods are commonly opened as a group.  Keeping every part in a
        # bytes object can consume gigabytes before path discovery even starts.
        # Map large archives instead; slices
        # still produce independent bytes for the decoders, while untouched
        # payload remains backed by the OS file cache.
        self._mapping = None
        if self.source.stat().st_size >= 64 * 1024 * 1024:
            with self.source.open('rb') as stream:
                self._mapping = mmap.mmap(stream.fileno(), 0, access=mmap.ACCESS_READ)
            self.data = self._mapping
        else:
            self.data = self.source.read_bytes()
        self.entries: dict[int, Entry] = {}
        # Some protected packs deliberately replace the lookup hashes of real
        # entries.  Deep discovery can still recover a path from the entry's
        # own contents (most reliably a TOBJ's referenced DDS).  Keep those
        # content-derived bindings separate from the on-disk hash table.
        self.path_aliases: dict[str, Entry] = {}
        self.duplicate_directories: dict[int, list[Entry]] = {}
        self.rejected_entries: list[tuple[int, str]] = []
        self.unimplemented_entries: list[tuple[int, int]] = []
        if len(self.data) < 24 or self.data[:4] != b'SCS#':
            raise HashFsError('Not a HashFS archive')
        self.version, header_salt = unpack_from('<HH', self.data, 4)
        self.salt = header_salt if salt is None else salt
        if self.data[8:12] != b'CITY':
            raise HashFsError('Unsupported HashFS hash method')
        self.entry_count = unpack_from('<I', self.data, 12)[0]
        if self.version == 1:
            self._parse_v1(table_at_end)
            self._recover_v1_offsets()
            if salt is None:
                self._recover_v1_salt()
        elif self.version == 2:
            self._parse_v2()
        else:
            raise HashFsError(f'Unsupported HashFS version {self.version}')

    def close(self) -> None:
        mapping = self._mapping
        if mapping is not None:
            self._mapping = None
            mapping.close()

    def __del__(self):
        try:
            self.close()
        except (AttributeError, BufferError):
            pass

    def _range(self, offset: int, length: int) -> bytes:
        if offset < 0 or length < 0 or offset + length > len(self.data):
            raise HashFsError(f'Archive range out of bounds: {offset}, {length}')
        return self.data[offset:offset + length]

    def _parse_v1(self, at_end: bool) -> None:
        offset = len(self.data) - self.entry_count * 32 if at_end else unpack_from('<Q', self.data, 16)[0]
        table = self._range(offset, self.entry_count * 32)
        rows = [unpack_from('<QQIIII', table, index * 32)
                for index in range(self.entry_count)]
        hash_counts: dict[int, int] = {}
        directory_hashes: set[int] = set()
        last_row: dict[int, int] = {}
        for row_index, (hashed, _position, flags, _crc, _size, _compressed) in enumerate(rows):
            hash_counts[hashed] = hash_counts.get(hashed, 0) + 1
            last_row[hashed] = row_index
            if flags & 1:
                directory_hashes.add(hashed)
        for row_index, (hashed, position, flags, _crc, size, compressed) in enumerate(rows):
            entry = Entry(hashed, position, size, compressed, bool(flags & 1), bool(flags & 2), flags=flags)
            duplicate_directory = (hash_counts[hashed] > 1
                                   and hashed in directory_hashes
                                   and entry.is_directory)
            # HashFS v1 readers resolve a duplicate file hash to the final
            # table row.  Keep that row extractable and classify only its
            # shadowed predecessors as junk.  Directory duplicates remain
            # separate listings as before.
            if (hash_counts[hashed] > 1 and not duplicate_directory
                    and row_index != last_row[hashed]):
                self.rejected_entries.append((hashed, 'DuplicateHash'))
                continue
            stored_size = compressed if entry.is_compressed else size
            reason = None
            if flags & 0x40000000:
                reason = 'MetadataOutOfBounds'
            elif size and not stored_size:
                reason = 'InvalidMetadata'
            elif position + stored_size > len(self.data):
                reason = 'PayloadOutOfBounds'
            elif (stored_size and position < offset + len(table)
                  and offset < position + stored_size):
                reason = 'PayloadOverlapsTable'
            elif entry.is_compressed and stored_size >= 2:
                cmf, flg = self.data[position:position + 2]
                if (cmf & 15) != 8 or (cmf >> 4) > 7 or ((cmf << 8) + flg) % 31:
                    reason = 'InvalidZlibHeader'
                elif (size / stored_size > 100
                      and self._looks_like_empty_header(
                          self.data[position:position + stored_size], size)):
                    reason = 'SuspiciousCompressionRatio'
            if reason:
                self.rejected_entries.append((hashed, reason))
                continue
            if hashed in self.entries:
                if entry.is_directory and self.entries[hashed].is_directory:
                    self.duplicate_directories.setdefault(hashed, []).append(entry)
            else:
                self.entries[hashed] = entry

    def _recover_v1_offsets(self) -> None:
        # One known DRM packer moves compressed payloads while setting flag 7.
        nudges = (-91, -75, -59, -43, -27, 37, 53, 69, 85, 101)
        for entry in self.entries.values():
            if not (entry.flags & 0x80 and entry.is_compressed):
                continue
            valid = []
            for nudge in nudges:
                position = entry.offset + nudge
                if position < 0 or position + entry.compressed_size > len(self.data):
                    continue
                if self.data[position:position + 2] not in (b'\x78\x01', b'\x78\x9c', b'\x78\xda'):
                    continue
                try:
                    self._zlib_exact(self._range(position, entry.compressed_size),
                                     entry.size, f'entry {entry.hash:016x}')
                    valid.append(position)
                except HashFsError:
                    continue
            if len(valid) == 1:
                entry.offset = valid[0]

    def _recover_v1_salt(self) -> None:
        manifest = self.get_entry('/manifest.sii')
        if manifest is None:
            return
        try:
            self.read_entry(manifest)
            return
        except (HashFsError, EOFError):
            pass
        original = self.salt
        for candidate in range(65536):
            self.salt = candidate
            manifest = self.get_entry('/manifest.sii')
            if manifest is not None:
                try:
                    self.read_entry(manifest)
                    return
                except (HashFsError, EOFError):
                    continue
        self.salt = original

    @staticmethod
    def _zlib_exact(data: bytes, expected: int, label: str,
                    wbits: int = zlib.MAX_WBITS) -> bytes:
        if expected > 512 * 1024 * 1024:
            raise HashFsError(f'{label} exceeds the 512 MiB safety limit')
        inflater = zlib.decompressobj(wbits)
        try:
            result = inflater.decompress(data, expected + 1)
            result += inflater.flush()
        except zlib.error as exc:
            raise HashFsError(f'Invalid zlib data in {label}') from exc
        if len(result) != expected or not inflater.eof:
            raise HashFsError(f'Incorrect uncompressed size in {label}: {len(result)} != {expected}')
        if inflater.unused_data or inflater.unconsumed_tail:
            raise HashFsError(f'Trailing compressed data in {label}')
        return result

    @staticmethod
    def _looks_like_empty_header(data: bytes, expected: int) -> bool:
        """Recognize anti-extractor entries containing a magic and zero fill.

        A compression ratio alone is not evidence of corruption: valid DDS
        payloads in real mods exceed 600:1.  Known decoy entries, however,
        contain only a short file signature followed by zero bytes.  Inflating
        a small prefix distinguishes those entries without expanding their
        often very large declared payloads while the archive is opened.
        """
        probe_length = min(expected, 256)
        if probe_length < 64:
            return False
        try:
            prefix = zlib.decompressobj().decompress(data, probe_length)
        except zlib.error:
            return False
        if len(prefix) < probe_length:
            return False
        return (sum(byte != 0 for byte in prefix) <= 16
                and len(set(prefix)) <= 16)

    def _parse_v2(self) -> None:
        if len(self.data) < 45:
            raise HashFsError('Truncated HashFS v2 header')
        entry_length, metadata_count, metadata_length = unpack_from('<III', self.data, 16)
        entry_start, metadata_start = unpack_from('<QQ', self.data, 28)
        def read_table(offset: int, stored_length: int, expected_length: int,
                       label: str) -> bytes:
            table = self._range(offset, stored_length)
            # HashFS v2 permits table compression to be omitted.  In that
            # case the stored length is already the exact decoded length. A
            # compressed table can coincidentally have that same length, so a
            # valid zlib stream must take precedence over the raw fallback.
            if stored_length == expected_length:
                try:
                    return self._zlib_exact(table, expected_length, label)
                except HashFsError:
                    return table
            return self._zlib_exact(table, expected_length, label)

        entries = read_table(entry_start, entry_length,
                             self.entry_count * 16, 'entry table')
        metadata = read_table(metadata_start, metadata_length,
                              metadata_count * 4, 'metadata table')
        rows = [unpack_from('<QIHH', entries, i * 16) for i in range(self.entry_count)]
        hash_counts: dict[int, int] = {}
        for hashed, *_ in rows:
            hash_counts[hashed] = hash_counts.get(hashed, 0) + 1
        for hashed, index, count, flags in sorted(rows, key=lambda row: row[1]):
            if hash_counts[hashed] > 1:
                self.rejected_entries.append((hashed, 'DuplicateHash'))
                continue
            if not count:
                self.rejected_entries.append((hashed, 'InvalidMetadata'))
                continue
            if index * 4 + count * 4 > len(metadata):
                self.rejected_entries.append((hashed, 'MetadataOutOfBounds'))
                continue
            descriptors = []
            for descriptor_index in range(count):
                descriptor = index * 4 + descriptor_index * 4
                body_index = (metadata[descriptor]
                              | metadata[descriptor + 1] << 8
                              | metadata[descriptor + 2] << 16)
                descriptors.append((body_index, metadata[descriptor + 3]))
            # Protected archives may prepend an unknown/decoy metadata chunk.
            # The game still uses the later recognized primary chunk.  Follow
            # each descriptor's explicit body index instead of assuming that
            # the first descriptor is primary and its body follows the list.
            primary = next(((body_index, chunk_type)
                            for body_index, chunk_type in descriptors
                            if chunk_type in (128, 129, 1)), None)
            if primary is None:
                self.unimplemented_entries.append((hashed, descriptors[0][1]))
                continue
            body_index, kind = primary
            body = body_index * 4
            texture = TextureMeta.parse(metadata[body:body + 12]) if kind == 1 else None
            if kind == 1:
                body += 12  # packed .tobj/.dds metadata
            if body + 16 > len(metadata):
                self.rejected_entries.append((hashed, 'InvalidMetadata'))
                continue
            compressed_field, size_field, _unknown, block = unpack_from('<IIII', metadata, body)
            compressed = compressed_field & 0x0fffffff
            size = size_field & 0x0fffffff
            if kind == 1:
                size = compressed
            position = block * 16
            compression_flags = (compressed_field >> 28) & 15
            is_compressed = compression_flags != 0
            raw_deflate = kind in (128, 129) and compression_flags == 2
            stored_size = compressed if is_compressed else size
            reason = None
            if size and not stored_size:
                reason = 'InvalidMetadata'
            elif position + stored_size > len(self.data):
                reason = 'PayloadOutOfBounds'
            elif stored_size and (position < entry_start + entry_length and entry_start < position + stored_size):
                reason = 'PayloadOverlapsTable'
            elif stored_size and (position < metadata_start + metadata_length and metadata_start < position + stored_size):
                reason = 'PayloadOverlapsTable'
            elif (kind in (128, 129) and compression_flags == 1
                  and stored_size >= 2):
                cmf, flg = self.data[position:position + 2]
                if (cmf & 15) != 8 or (cmf >> 4) > 7 or ((cmf << 8) + flg) % 31:
                    reason = 'InvalidZlibHeader'
                # Highly repetitive game assets can legitimately compress by
                # several hundred times.  Decoy entries seen in protected
                # archives consist only of a short signature and zero fill;
                # require that content marker instead of a size threshold.
                elif (size / stored_size > 100
                      and self._looks_like_empty_header(
                          self.data[position:position + stored_size], size)):
                    reason = 'SuspiciousCompressionRatio'
            if reason:
                self.rejected_entries.append((hashed, reason))
                continue
            self.entries.setdefault(hashed, Entry(
                hashed, position, size, compressed, kind == 129,
                is_compressed, kind, flags, texture, raw_deflate))

    def get_entry(self, path: str) -> Entry | None:
        normalized = '/' + path.replace('\\', '/').strip('/')
        normalized = normalized if normalized != '/' else '/'
        return (self.path_aliases.get(normalized)
                or self.entries.get(hash_path(normalized, self.salt)))

    def add_path_alias(self, path: str, entry: Entry) -> bool:
        normalized = '/' + path.replace('\\', '/').strip('/')
        normalized = normalized if normalized != '/' else '/'
        existing = self.get_entry(normalized)
        if existing is not None and existing is not entry:
            return False
        self.path_aliases[normalized] = entry
        return True

    def read_entry(self, entry: Entry) -> bytes:
        if entry.kind == 1:
            raise HashFsError('Packed texture requires a path; use read_files')
        size = entry.compressed_size if entry.is_compressed else entry.size
        payload = self._range(entry.offset, size)
        if entry.is_compressed:
            return self._zlib_exact(payload, entry.size,
                                    f'entry {entry.hash:016x}',
                                    -zlib.MAX_WBITS if entry.raw_deflate
                                    else zlib.MAX_WBITS)
        return payload

    def read_entry_prefix(self, entry: Entry, limit: int = 64 * 1024) -> bytes:
        """Read at most *limit* decompressed bytes without expanding the file."""
        if entry.kind == 1:
            raise HashFsError('Packed texture requires a path; use read_files')
        limit = max(0, min(limit, entry.size))
        if not limit:
            return b''
        stored_size = entry.compressed_size if entry.is_compressed else entry.size
        if not entry.is_compressed:
            return self._range(entry.offset, min(stored_size, limit))

        inflater = zlib.decompressobj(
            -zlib.MAX_WBITS if entry.raw_deflate else zlib.MAX_WBITS)
        result = bytearray()
        source_position = entry.offset
        source_end = entry.offset + stored_size
        try:
            while source_position < source_end and len(result) < limit:
                chunk_size = min(4 * 1024, source_end - source_position)
                chunk = self._range(source_position, chunk_size)
                source_position += chunk_size
                result.extend(inflater.decompress(chunk, limit - len(result)))
                if inflater.eof:
                    break
        except zlib.error as exc:
            raise HashFsError(
                f'Invalid compressed entry {entry.hash:016x}') from exc
        return bytes(result)

    def read_entry_probes(self, entries, limit: int = 256,
                          merge_gap: int = 16 * 1024,
                          max_range: int = 8 * 1024 * 1024):
        """Read many entry prefixes using a few physically ordered ranges.

        Deep discovery used to touch the memory mapping once per entry.  On a
        mechanical disk that turns a modest amount of data into tens of
        thousands of seeks.  Adjacent probes are coalesced here.  If a small
        compressed file ends inside its first 4 KiB, its complete decompressed
        payload is returned too so the caller does not read it a second time.
        """
        ordered = sorted(entries, key=lambda item: item.offset)
        requests = []
        for entry in ordered:
            stored_size = (entry.compressed_size if entry.is_compressed
                           else entry.size)
            probe_size = min(stored_size, 4096 if entry.is_compressed else limit)
            requests.append((entry, entry.offset, entry.offset + probe_size))

        result = {}
        position = 0
        while position < len(requests):
            first_entry, range_start, range_end = requests[position]
            group_end = position + 1
            while group_end < len(requests):
                _entry, start, end = requests[group_end]
                if start - range_end > merge_gap or end - range_start > max_range:
                    break
                range_end = max(range_end, end)
                group_end += 1
            block = self._range(range_start, range_end - range_start)
            for entry, start, end in requests[position:group_end]:
                sample = block[start - range_start:end - range_start]
                try:
                    if not entry.is_compressed:
                        payload = sample[:limit]
                        complete = payload if entry.size <= limit else None
                    else:
                        inflater = zlib.decompressobj(
                            -zlib.MAX_WBITS if entry.raw_deflate
                            else zlib.MAX_WBITS)
                        # One MiB is enough for every path-bearing text format,
                        # while preventing malformed ratio bombs from growing.
                        expanded = inflater.decompress(sample, min(entry.size, 1024 * 1024) + 1)
                        payload = expanded[:limit]
                        complete = (expanded if inflater.eof and
                                    len(expanded) == entry.size and
                                    entry.size <= 1024 * 1024 else None)
                    result[id(entry)] = (payload, complete, None)
                except zlib.error as exc:
                    result[id(entry)] = (b'', None, HashFsError(
                        f'Invalid compressed entry {entry.hash:016x}'))
            position = group_end
        return result

    def read(self, path: str) -> bytes:
        entry = self.get_entry(path)
        if entry is None or entry.is_directory:
            raise FileNotFoundError(path)
        # A packed-texture entry represents both a generated TOBJ and its DDS.
        # Callers asking for the TOBJ itself (notably the deep path scanner)
        # must not pay the cost of rebuilding/decompressing the DDS as well.
        if entry.kind == 1:
            assert entry.texture is not None
            return make_tobj(entry.texture, path)
        return self.read_files(path)[path]

    def read_files(self, path: str) -> dict[str, bytes]:
        entry = self.get_entry(path)
        if entry is None or entry.is_directory:
            raise FileNotFoundError(path)
        return self.read_files_entry(entry, path)

    def read_files_entry(self, entry: Entry, path: str) -> dict[str, bytes]:
        if entry.kind == 1:
            assert entry.texture is not None
            size = entry.compressed_size if entry.is_compressed else entry.size
            payload = self._range(entry.offset, size)
            dds_path = str(Path(path).with_suffix('.dds')).replace('\\', '/')
            return {path: make_tobj(entry.texture, path),
                    dds_path: make_dds(entry.texture, payload, entry.is_compressed)}
        return {path: self.read_entry(entry)}

    def listing(self, path: str) -> tuple[list[str], list[str]]:
        entry = self.get_entry(path)
        if entry is None or not entry.is_directory:
            raise FileNotFoundError(path)
        data = b''.join(self.read_entry(part) for part in
                        [entry, *self.duplicate_directories.get(entry.hash, [])])
        if self.version == 1:
            names = data.decode('utf-8').splitlines()
            directories = [name[1:] for name in names if name.startswith('*')]
            files = [name for name in names if name and not name.startswith('*')]
        else:
            if len(data) < 4:
                raise HashFsError('Truncated directory listing')
            count = unpack_from('<I', data)[0]
            lengths = data[4:4 + count]
            if len(lengths) != count:
                raise HashFsError('Truncated directory listing')
            position = 4 + count
            directories, files = [], []
            for length in lengths:
                name = data[position:position + length].decode('utf-8')
                if len(data[position:position + length]) != length:
                    raise HashFsError('Truncated directory listing')
                position += length
                (directories if name.startswith('/') else files).append(name.lstrip('/') if name.startswith('/') else name)
        prefix = path.rstrip('/')
        join = lambda name: (prefix + '/' + name) if prefix else ('/' + name)
        return [join(name) for name in directories], [join(name) for name in files]

    def walk(self, starts: list[str] | None = None) -> list[str]:
        pending = list(starts or ['/'])
        visited: set[str] = set()
        files: list[str] = []
        while pending:
            path = pending.pop()
            if path in visited:
                continue
            visited.add(path)
            entry = self.get_entry(path)
            if entry is None:
                continue
            if entry.is_directory:
                directories, children = self.listing(path)
                pending.extend(reversed(directories))
                files.extend(child for child in children if self.get_entry(child) is not None)
            else:
                files.append(path)
        return sorted(set(files))

    # IFileSystem-compatible API
    directory_separator = '/'
    def entry_exists(self, path: str):
        entry=self.get_entry(path); return EntryType.NOT_FOUND if entry is None else (EntryType.DIRECTORY if entry.is_directory else EntryType.FILE)
    def file_exists(self,path): return self.entry_exists(path)==EntryType.FILE
    def directory_exists(self,path): return self.entry_exists(path)==EntryType.DIRECTORY
    def get_files(self,path): return self.listing(path)[1]
    def get_parent(self,path):
        if path=='/': return None
        return path.rstrip('/').rsplit('/',1)[0] or '/'
    def read_all_bytes(self,path): return self.read(path)
    def read_all_text(self,path,encoding='utf-8'): return self.read(path).decode(encoding)
    def open(self,path): return BytesIO(self.read(path))
    def extract(self,path): return list(self.read_files(path).values())
    def extract_to_file(self,entry_path,output_path):
        target=Path(output_path); target.parent.mkdir(parents=True,exist_ok=True); target.write_bytes(self.read(entry_path))

class EntryType(IntEnum): NOT_FOUND=0; FILE=1; DIRECTORY=2

@dataclass
class DirectoryListing:
    subdirectories:list[str]; files:list[str]

@dataclass
class HashFsReaderOptions:
    force_v1_entry_table_at_end:bool=False
    skip_invalid_entries:bool=True
    validate_zlib_headers:bool=True
    max_compression_ratio:float=100.0
    compression_bomb_min_size:int=0
    def validate(self):
        if self.max_compression_ratio < 0 or self.max_compression_ratio != self.max_compression_ratio or self.max_compression_ratio == float('inf'): raise ValueError('Invalid compression ratio')
        return self

def nearest_multiple(value:int,multiple:int)->int: return value if multiple==0 else (value+multiple-1)//multiple*multiple

class AssetLoader:
    directory_separator='/'
    def __init__(self,file_systems):
        if not file_systems: raise ValueError('file_systems must not be empty')
        self.file_systems=list(file_systems)
    def file_exists(self,path): return any(fs.file_exists(path) for fs in self.file_systems)
    def directory_exists(self,path): return any(fs.directory_exists(path) for fs in self.file_systems)
    def get_files(self,path):
        found=False; result=[]
        for fs in self.file_systems:
            if fs.directory_exists(path): found=True; result.extend(fs.get_files(path))
        if not found: raise FileNotFoundError(path)
        return list(dict.fromkeys(result))
    def get_parent(self,path): return None if path=='/' else path.rstrip('/').rsplit('/',1)[0] or '/'
    def _fs(self,path):
        for fs in self.file_systems:
            if fs.file_exists(path): return fs
        raise FileNotFoundError(path)
    def open(self,path): return self._fs(path).open(path)
    def read_all_bytes(self,path): return self._fs(path).read_all_bytes(path)
    def read_all_text(self,path,encoding='utf-8'): return self._fs(path).read_all_text(path,encoding)

class HashFsWriterBase:
    def __init__(self,salt=0,compression_threshold=64,compression_level=9):
        self.salt=salt; self.compression_threshold=compression_threshold; self.compression_level=compression_level; self.files={}
    def add(self,source,archive_path):
        path='/' + archive_path.strip('/')
        if path=='/': raise ValueError('archive path may not be root')
        if any(len(x)>255 for x in path.split('/')): raise ValueError('path component exceeds 255 characters')
        if hasattr(source,'read'):
            old=source.tell() if hasattr(source,'tell') else None; data=source.read()
            if old is not None: source.seek(old)
        elif isinstance(source,(bytes,bytearray,memoryview)): data=bytes(source)
        else:
            disk=Path(source)
            if not disk.is_file(): raise FileNotFoundError(source)
            data=disk.read_bytes()
        self.files[path]=data
    def _directories(self):
        dirs={'/':([],[])}
        for path in self.files:
            parts=path.strip('/').split('/')
            for i in range(len(parts)):
                parent='/'+'/'.join(parts[:i]) if i else '/'; child=parts[i]
                ds,fs=dirs.setdefault(parent,([],[]))
                if i==len(parts)-1:
                    if child not in fs: fs.append(child)
                else:
                    if child not in ds: ds.append(child)
                    dirs.setdefault('/'+'/'.join(parts[:i+1]),([],[]))
        return dirs
    @staticmethod
    def _output(destination): return (Path(destination).open('w+b'),True) if isinstance(destination,(str,Path)) else (destination,False)

class HashFsV1Writer(HashFsWriterBase):
    def __init__(self,*a,compute_checksums=False,**kw): super().__init__(*a,**kw); self.compute_checksums=compute_checksums
    def save(self,destination):
        stream,close=self._output(destination)
        try:
            stream.seek(4096); rows=[]; all_files=dict(self.files)
            for path,(dirs,files) in self._directories().items(): all_files[path]=(''.join('*'+x+'\n' for x in dirs)+''.join(x+'\n' for x in files)).encode()
            directories=set(self._directories())
            for path,data in all_files.items():
                start=stream.tell(); compressed=len(data)>self.compression_threshold and self.compression_level!=0; stored=zlib.compress(data,self.compression_level) if compressed else data; stream.write(stored)
                flags=(1 if path in directories else 0)|(2 if compressed else 0); crc=zlib.crc32(data)&0xffffffff if self.compute_checksums else 0
                rows.append((hash_path(path,self.salt),start,flags,crc,len(data),len(stored)))
            table=stream.tell()
            for row in sorted(rows): stream.write(pack('<QQIIII',*row))
            stream.write(b'TruckLib.HashFs Python')
            end=stream.tell(); stream.seek(0); stream.write(pack('<4sHH4sIQ',b'SCS#',1,self.salt,b'CITY',len(rows),table)); stream.truncate(end)
        finally:
            if close: stream.close()

class HashFsV2Writer(HashFsWriterBase):
    """HashFS v2 writer for regular files and directory listings."""
    def save(self,destination):
        stream,close=self._output(destination)
        try:
            stream.seek(4096); rows=[]; metadata=bytearray(); all_files={k:v for k,v in self.files.items() if not k.lower().endswith('.dds')}
            dirs=self._directories()
            for path,(children,files) in dirs.items():
                names=[('/'+x).encode() for x in children]+[x.encode() for x in files if not x.lower().endswith('.dds')]
                all_files[path]=pack('<I',len(names))+bytes(map(len,names))+b''.join(names)
            for path,data in all_files.items():
                if path.lower().endswith('.tobj'):
                    try:
                        length=unpack_from('<Q',data,40)[0]; dds_path=data[48:48+length].decode('utf-8')
                        if not dds_path.startswith('/'): dds_path=path.rsplit('/',1)[0]+'/'+dds_path
                        dds_data=self.files[dds_path]
                        texture,surface=texture_metadata(data,dds_data); start=nearest_multiple(stream.tell(),16); stream.seek(start); stream.write(surface)
                        meta_index=len(metadata)//4; target=meta_index+3
                        metadata += bytes((target&255,(target>>8)&255,(target>>16)&255,1)); target+=2
                        metadata += bytes((target&255,(target>>8)&255,(target>>16)&255,2)); target+=1
                        metadata += bytes((target&255,(target>>8)&255,(target>>16)&255,132))
                        metadata += pack('<HHII',texture.width-1,texture.height-1,texture.image_flags,texture.sample_flags)
                        metadata += pack('<IIII',len(surface),len(surface)|0x30000000,0,start//16)
                        rows.append((hash_path(path,self.salt),meta_index,3,0)); continue
                    except (KeyError,ValueError,UnicodeError,EOFError) as exc:
                        raise TexturePackingException(f'{path}: {exc}') from exc
                start=nearest_multiple(stream.tell(),16); stream.seek(start); compressed=len(data)>self.compression_threshold and self.compression_level!=0; stored=zlib.compress(data,self.compression_level) if compressed else data; stream.write(stored)
                meta_index=len(metadata)//4; kind=129 if path in dirs else 128; metadata += bytes((meta_index+1,0,0,kind))
                cf=len(stored)|(0x10000000 if compressed else 0); metadata += pack('<IIII',cf,len(data),0,start//16)
                rows.append((hash_path(path,self.salt),meta_index,1,1 if path in dirs else 0))
            entry_raw=b''.join(pack('<QIHH',*r) for r in sorted(rows)); entry_data=zlib.compress(entry_raw,9); entry_start=stream.tell(); stream.write(entry_data)
            meta_start=nearest_multiple(stream.tell(),16); stream.seek(meta_start); meta_data=zlib.compress(bytes(metadata),9); stream.write(meta_data); stream.write(b'TruckLib.HashFs Python')
            end=stream.tell(); stream.seek(0); stream.write(pack('<4sHH4sIIIIQQQB',b'SCS#',2,self.salt,b'CITY',len(rows),len(entry_data),len(metadata)//4,len(meta_data),entry_start,meta_start,0,0)); stream.truncate(end)
        finally:
            if close: stream.close()

HashFsV1Reader = HashFsReader
HashFsV2Reader = HashFsReader


__all__ = [name for name in globals() if not name.startswith('_')]
