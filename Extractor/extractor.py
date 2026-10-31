"""Command-line Python port of ExtractorFork's archive extraction workflow."""
from __future__ import annotations

import argparse
from collections import Counter, deque
from concurrent.futures import (FIRST_COMPLETED, ProcessPoolExecutor,
                                ThreadPoolExecutor, wait)
from dataclasses import dataclass
import fnmatch
import gzip
import logging
import multiprocessing
import os
from pathlib import Path, PurePosixPath
import re
import shlex
import signal
import subprocess
import sys
from threading import Lock
import time
from datetime import datetime

ROOT = Path(__file__).resolve().parent.parent
for package in ('TruckLib.Core', 'TruckLib.Models', 'TruckLib.Sii', 'GisDeflate', 'TruckLib.HashFs'):
    sys.path.insert(0, str(ROOT / package))

from trucklib_core import normalize_path
from trucklib_hashfs import HashFsReader, hash_path
from trucklib_models import Tobj, find_model_paths
from trucklib_sii import (MatFile, decode as decode_sii,
                          find_paths as find_sii_paths, find_mat_paths,
                          find_self_path_candidates as find_sii_self_paths)
from zip_reader import ZipReader
from cityhash import cityhash64
from file_types import FileType, file_type_to_extension, infer
from gisdeflate import configure_executor as configure_gdeflate_executor
from plugin_runtime import PluginManager
from archive_tools import (compress_hashfs_v1, edit_manifest,
                           parse_replacements)
from community_paths import (fetch_paths as fetch_community_paths,
                             load_config as load_community_config,
                             read_local_paths as read_community_local_paths,
                             temporary_path_list as temporary_community_path_list,
                             valid_webhook as valid_community_webhook)
from app_settings import configured_workers

# Markers used only by the packer's deliberately unnamed entertainment
# records.  They must be defined before the __main__ dispatcher: a frozen
# executable exits through that dispatcher and otherwise never reaches module
# constants placed at the end of this file.
ENTERTAINMENT_EXTENSIONS = (
    '.sii', '.sui', '.pmd', '.pma', '.ppd', '.pmg', '.pmc', '.mat',
    '.tobj', '.dds', '.soundref', '.png', '.jpg', '.bank', '.bank.guids',
    '.txt',
)
ENTERTAINMENT_EXTENSION_MARKER = b'SCSX'

# Library logging must stay silent unless --log is enabled.  Without a root
# handler, Python's lastResort handler prints WARNING records to stderr after
# the extractor's own console output, which made messages appear after the
# final success line.
logging.getLogger().addHandler(logging.NullHandler())


def application_directory() -> Path:
    """Directory containing the source application or bundled executable."""
    if getattr(sys, 'frozen', False):
        return Path(sys.executable).resolve().parent
    return ROOT

_path_hash_executor = None
_path_hash_workers = 1
_community_path_lock = Lock()


def parallel_worker_count() -> int:
    """Leave four logical CPUs for the game, GUI and the operating system."""
    cpu_count = (getattr(os, 'process_cpu_count', os.cpu_count)() or 1)
    return configured_workers(min(32, max(1, cpu_count - 4)))


def _safe_print(*values, file=None, **kwargs) -> bool:
    """Write a console message without turning a closed pipe into a failure.

    The GUI terminates the child process tree when the user presses Stop.  A
    worker can be between two output lines at that moment; Windows then raises
    ``OSError(22)`` while writing to the inherited pipe.  Reporting that
    secondary error used to replace the real extraction result with a
    traceback and could make ``ThreadPoolExecutor.map`` look hung.
    """
    stream = sys.stdout if file is None else file
    try:
        print(*values, file=stream, **kwargs)
        return True
    except (BrokenPipeError, OSError, ValueError):
        return False


def _ignore_worker_interrupt() -> None:
    """Let the parent coordinate Ctrl+C without worker traceback storms."""
    signal.signal(signal.SIGINT, signal.SIG_IGN)


def configure_path_hash_executor(executor, workers: int | None = None) -> None:
    global _path_hash_executor, _path_hash_workers
    _path_hash_executor = executor
    _path_hash_workers = max(1, workers or 1)


def _filter_salted_path_batch(job) -> list[str]:
    """Return only dictionary paths whose salted hashes exist in the archive."""
    paths, targets_by_salt = job
    matched = []
    for path in paths:
        normalized = normalize_path(path)
        if any(hash_path(normalized, salt) in wanted_hashes
               for salt, wanted_hashes in targets_by_salt.items()):
            matched.append(normalized)
    return matched


def _filter_package_root_path_batch(job) -> list[str]:
    """Match known paths after mounting them below base/non-map DLC roots."""
    paths, targets_by_salt = job
    matched: list[str] = []
    one_salt = (next(iter(targets_by_salt.items()))
                if len(targets_by_salt) == 1 else None)
    for path in paths:
        # A package mount prefix is meaningful for files.  Directory entries
        # are subsequently discovered through the matched file's parents, so
        # skipping them keeps this final probe bounded even for huge archives.
        if '.' not in path.rsplit('/', 1)[-1]:
            continue
        first_component = path.lstrip('/').partition('/')[0].casefold()
        if first_component in PACKAGE_ROOT_PREFIX_SET:
            continue
        for root in PACKAGE_ROOT_PREFIXES:
            candidate = f'/{root}{path}'
            if ((one_salt is not None
                 and hash_path(candidate, one_salt[0]) in one_salt[1])
                    or (one_salt is None and any(
                        hash_path(candidate, salt) in wanted_hashes
                        for salt, wanted_hashes in targets_by_salt.items()))):
                matched.append(candidate)
    return matched


def _analyze_named_path_payload(job) -> set[str]:
    """CPU-bound reference parsing executed outside the parent GIL."""
    data, path = job
    return expand_candidates(references(data, path))


def _analyze_named_path_payload_batch(jobs):
    return [_analyze_named_path_payload(job) for job in jobs]


def _analyze_unknown_path_payload(data: bytes):
    """Infer an unnamed payload and its references in a worker process."""
    inferred_kind = infer(data)
    alias_candidate = None
    material_texture_paths = ()
    relative_reference_paths = ()
    try:
        if inferred_kind == FileType.MATERIAL and len(data) <= 256 * 1024:
            material = MatFile.load(data)
            material_texture_paths = tuple(material.texture_paths)
            relative_reference_paths = tuple(
                path for path in material_texture_paths
                if path and not str(path).replace('\\', '/').startswith('/'))
            if material.effect.lower() not in ('', 'x'):
                content_hash = f'{cityhash64(data):016x}'
                alias_candidate = f'/automat/{content_hash[:2]}/{content_hash}.mat'
        elif inferred_kind == FileType.TOBJ:
            raw_texture_path = Tobj.load(data).texture_path.replace('\\', '/')
            if not raw_texture_path.startswith('/'):
                relative_reference_paths = (raw_texture_path,)
            texture_path = normalize_path(raw_texture_path)
            alias_candidate = str(PurePosixPath(texture_path).with_suffix('.tobj'))
        elif inferred_kind == FileType.PMD:
            relative_reference_paths = tuple(
                path for path in find_model_paths(data)
                if path and not str(path).replace('\\', '/').startswith('/'))
    except (ValueError, UnicodeError, EOFError, TypeError):
        alias_candidate = None
    return (inferred_kind, inferred_references(data), alias_candidate,
            material_texture_paths, relative_reference_paths)


def _analyze_unknown_path_payload_batch(payloads):
    """Analyze many tiny unnamed payloads in a single IPC call."""
    return [_analyze_unknown_path_payload(data) for data in payloads]


def _looks_like_path_text_prefix(data: bytes) -> bool:
    """Cheaply reject binary UNKNOWN payloads before a complete read."""
    if data.startswith((b'\xff\xfe', b'\xfe\xff')):
        return True
    sample = data.lstrip(b'\xef\xbb\xbf')
    if sample.lower().startswith(b'[localizedfilenames]'):
        return True
    if not sample:
        return False
    textual = sum(byte in (9, 10, 13) or 32 <= byte < 127 for byte in sample)
    return textual / len(sample) >= 0.85


def print_path_list(paths: list[str]) -> None:
    """Print arbitrary archive names without failing on the console code page."""
    value = '\n'.join(paths)
    encoding = getattr(sys.stdout, 'encoding', None) or 'utf-8'
    print(value.encode(encoding, errors='replace').decode(encoding))


# Some protected HashFS packs append this marker to otherwise valid payloads.
# The game treats it as an out-of-band protection trailer; keep it in --raw
# mode, but remove it from files produced by normal extraction.
HASHFS_PROTECTION_TRAILER = bytes.fromhex(
    '00 47 4d 43 57 4d 01 10 63 34 78 36 75 68 70 67 '
    '6a 7a 6d 72 36 6c 36 74 fe 2f 6c 15')
HASHFS_PROTECTION_PREFIX = b'\x00GMCWM\x01\x10'
HASHFS_PROTECTION_TRAILER_SIZE = 28


def strip_hashfs_protection_trailer(data: bytes) -> bytes:
    # The 20 bytes following GMCWM vary between archives.  The marker and
    # total trailer length are stable, so recognize the framing instead of
    # hard-coding a token/checksum combination from any particular archive.
    if (len(data) >= HASHFS_PROTECTION_TRAILER_SIZE
            and data[-HASHFS_PROTECTION_TRAILER_SIZE:
                     -HASHFS_PROTECTION_TRAILER_SIZE + len(HASHFS_PROTECTION_PREFIX)]
            == HASHFS_PROTECTION_PREFIX):
        return data[:-HASHFS_PROTECTION_TRAILER_SIZE]
    return data


@dataclass
class ExtractionStats:
    extracted: int = 0
    renamed: int = 0
    modified: int = 0
    dumped: int = 0
    skipped: int = 0
    not_found: int = 0
    junk: int = 0
    failed: int = 0


def merge_stats(target: ExtractionStats, source: ExtractionStats) -> None:
    target.extracted += source.extracted
    target.renamed += source.renamed
    target.modified += source.modified
    target.dumped += source.dumped
    target.skipped += source.skipped
    target.not_found += source.not_found
    target.junk += source.junk
    target.failed += source.failed


class ExtractorLogFormatter(logging.Formatter):
    LEVELS = {
        logging.DEBUG: 'DBG', logging.INFO: 'INF', logging.WARNING: 'WRN',
        logging.ERROR: 'ERR', logging.CRITICAL: 'FTL',
    }

    def format(self, record: logging.LogRecord) -> str:
        timestamp = datetime.fromtimestamp(record.created).strftime('%H:%M:%S:')
        timestamp += f'{int(record.msecs / 10):02d}'
        level = self.LEVELS.get(record.levelno, record.levelname[:3].upper())
        message = f'[{timestamp} {level}] [{record.name}] {record.getMessage()}'
        if record.exc_info:
            message += '\n' + self.formatException(record.exc_info)
        return message


def configure_logging(filename: str, arguments: list[str] | None = None) -> None:
    # Serilog's file sink used by ExtractorFork appends by default.  Keep prior
    # runs and mark each new invocation with its arguments for diagnostics.
    handler = logging.FileHandler(filename, mode='a', encoding='utf-8')
    handler.setFormatter(ExtractorLogFormatter())
    root_logger = logging.getLogger()
    root_logger.handlers.clear()
    root_logger.setLevel(logging.DEBUG)
    root_logger.addHandler(handler)
    start_message = ('New Universal SCS Extractor process started'
                     if os.environ.get('UNIVERSAL_SCS_EXTRACTOR_NEW_PROCESS') == '1'
                     else 'Universal SCS Extractor started')
    logging.getLogger('Program').info(start_message)
    logging.getLogger('Program').info('Args: %s', arguments or [])


class CombinedHashFs:
    """Verified entry index spanning every part of a split HashFS mod."""
    def __init__(self, readers: list[HashFsReader]):
        self.readers = readers
        # ``entries`` is the union used for the O(1) seed-hash rejection.  Do
        # not use it for traversal: the same path hash can legitimately occur
        # in several archive parts with different contents and references.
        self.entries = {}
        self.entries_by_hash: dict[int, list[tuple[HashFsReader, object]]] = {}
        self.all_entries = []
        self._entry_owners: dict[int, HashFsReader] = {}
        for reader in readers:
            for entry_hash, entry in reader.entries.items():
                self.entries.setdefault(entry_hash, entry)
                self.entries_by_hash.setdefault(entry_hash, []).append((reader, entry))
                self.all_entries.append(entry)
                self._entry_owners[id(entry)] = reader
        self.salt = readers[0].salt if readers and all(
            reader.salt == readers[0].salt for reader in readers) else None

    def entries_for_path(self, path: str) -> list[object]:
        result = []
        seen: set[int] = set()
        for reader in self.readers:
            entry = reader.get_entry(path)
            if entry is not None and id(entry) not in seen:
                seen.add(id(entry))
                result.append(entry)
        return result

    def owner_of(self, entry) -> HashFsReader | None:
        return self._entry_owners.get(id(entry))

    def _owner(self, path: str) -> HashFsReader | None:
        return next((reader for reader in self.readers if reader.get_entry(path)), None)

    def get_entry(self, path: str):
        owner = self._owner(path)
        return owner.get_entry(path) if owner else None

    def add_path_alias(self, path: str, entry) -> bool:
        for reader in self.readers:
            owned = reader.entries.get(entry.hash)
            if owned is entry:
                return reader.add_path_alias(path, entry)
        return False

    def read(self, path: str) -> bytes:
        owner = self._owner(path)
        if owner is None:
            raise FileNotFoundError(path)
        return owner.read(path)

    def read_entry(self, entry) -> bytes:
        reader = self.owner_of(entry)
        if reader is not None:
            return reader.read_entry(entry)
        raise FileNotFoundError(f'{entry.hash:016x}')

    def read_entry_prefix(self, entry, limit: int = 64 * 1024) -> bytes:
        owner = self.owner_of(entry)
        if owner is not None:
            return owner.read_entry_prefix(entry, limit)
        raise FileNotFoundError(f'{entry.hash:016x}')

    def read_entry_probes(self, entries, limit: int = 256):
        by_owner = {}
        for entry in entries:
            owner = self.owner_of(entry)
            if owner is not None:
                by_owner.setdefault(owner, []).append(entry)
        result = {}
        for owner in self.readers:
            owned = by_owner.get(owner)
            if owned:
                result.update(owner.read_entry_probes(owned, limit))
        return result

    def read_path_entry(self, entry, path: str) -> bytes:
        reader = self.owner_of(entry)
        if reader is None:
            raise FileNotFoundError(path)
        # Packed textures require their recovered path to synthesize TOBJ.
        return reader.read(path) if entry.kind == 1 else reader.read_entry(entry)

    def listing(self, path: str) -> tuple[list[str], list[str]]:
        directories, files = set(), set()
        for reader in self.readers:
            entry = reader.get_entry(path)
            if entry and entry.is_directory:
                children = reader.listing(path)
                directories.update(children[0])
                files.update(children[1])
        return sorted(directories), sorted(files)


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        add_help=False,
        usage=r'.\UniversalSCSExtractor.exe path... [options]',
        description='Extract SCS HashFS v1/v2 and ZIP archives.',
        formatter_class=argparse.RawTextHelpFormatter,
        epilog='Examples:\n'
               '  .\\UniversalSCSExtractor.exe .\\base.scs\n'
               '  .\\UniversalSCSExtractor.exe .\\protected.scs --deep -d extracted\n'
               '  .\\UniversalSCSExtractor.exe .\\base.scs -p=/def,/map\n'
               '  .\\UniversalSCSExtractor.exe .\\base.scs -f="*.sii,*.mat"\n'
               '  .\\UniversalSCSExtractor.exe .\\mods_folder --all --separate\n'
               '  .\\UniversalSCSExtractor.exe .\\archive.scs --deep --dry-run --quiet\n'
               '  .\\UniversalSCSExtractor.exe .\\archive.scs --list-all --list-output=paths.txt\n'
               '  .\\UniversalSCSExtractor.exe .\\archive.scs --list-all --force-extract\n\n'
               '  .\\UniversalSCSExtractor.exe .\\addon.scs --reference .\\base.scs .\\def.scs\n\n'
               '  .\\UniversalSCSExtractor.exe .\\mod.scs --manifest\n'
               '  .\\UniversalSCSExtractor.exe .\\mod.scs --compress\n\n'
               'HashFS-specific options have no effect on ordinary ZIP archives.')
    p.add_argument('paths', nargs='*', metavar='path',
                   help='Archive paths, or directories when --all is used.')
    p.add_argument('--gui-child', action='store_true', help=argparse.SUPPRESS)
    p.add_argument('--console-session', action='store_true', help=argparse.SUPPRESS)
    p.add_argument('--ui-language', choices=('ru', 'en'), default='en',
                   help=argparse.SUPPRESS)
    p.add_argument('-a', '--all', action='store_true',
                   help='Treat every input path as a directory and process all *.scs files\n'
                        'found directly inside it.')
    p.add_argument('-D', '--deep', action='store_true',
                   help='[HashFS] Search archive contents and references for hashed paths\n'
                        'before extraction. Use for archives without a usable root listing.\n'
                        'Entries whose paths cannot be recovered are dumped to _unknown.')
    p.add_argument('-d', '--dest', default='extracted', metavar='DIR',
                   help='Output directory. Default: .\\extracted')
    p.add_argument('--additional', metavar='FILE',
                   help='[HashFS] Add newline-separated candidate paths to --deep search.')
    # GUI-only transport: it never appears in normal help or command previews.
    p.add_argument('--community-new-paths-output', metavar='FILE',
                   help=argparse.SUPPRESS)
    p.add_argument('-ref', '--reference', action='append', nargs='+', default=[],
                   metavar='ARCHIVE',
                   help='Use dependency archives only as path-discovery references.\n'
                        'Their tables and path-bearing files are scanned, but reference\n'
                        'archives are not extracted. Implies --deep.')
    p.add_argument('--plugin-load', action='append', default=[], metavar='FILE',
                   help='Install a .pyd or native .dll plugin beside the application.\n'
                        'May be specified more than once.')
    p.add_argument('--plugin-disable', action='append', default=[], metavar='PLUGIN',
                   help='Deactivate plugins by ID, name, or filename. Comma-separated;\n'
                        'ALL deactivates every available plugin.')
    p.add_argument('--plugin-activate', action='append', default=[], metavar='PLUGIN',
                   help='Activate plugins by ID, name, or filename. Comma-separated;\n'
                        'ALL activates every available plugin.')
    p.add_argument('--plugin-list', action='store_true',
                   help='List active and available plugins, then exit.')
    p.add_argument('--manifest', action='store_true',
                   help='[HashFS v1/v2, ZIP, AEM] Remove compatible_versions[] from manifest.sii.\n'
                        'The archive is rebuilt safely and replaced atomically.')
    p.add_argument('--manifest-replace', action='append', default=[], metavar='OLD=NEW',
                   help='[HashFS v1/v2, ZIP, AEM] Replace text in manifest.sii after removing its\n'
                        'compatible_versions[] entries. Repeat the option or separate\n'
                        'multiple replacements with semicolons. Implies --manifest.')
    p.add_argument('--edit', action='store_true',
                   help='Prompt for interactive manifest replacements in the original\n'
                        '"old text" "new text" format. Implies --manifest.')
    p.add_argument('--compress', action='store_true',
                   help='[HashFS v1] Compress uncompressed file entries with zlib,\n'
                        'compact the archive and atomically replace the original.')
    p.add_argument('-jc', '--jc', dest='just_compress', action='store_true',
                   help='[HashFS v1] Compress entry data without compacting unused archive\n'
                        'space. Implies --compress.')
    p.add_argument('--dry-run', action='store_true',
                   help='Perform parsing, path discovery and validation without writing files.')
    p.add_argument('-f', '--filter', action='append', default=[], metavar='PATTERNS',
                   help='Extract/list only matching paths. Accepts comma-separated wildcards,\n'
                        'for example "*.sii,/def/*", or regex syntax r/expression/.\n'
                        'May be specified more than once.')
    p.add_argument('--list', action='store_true',
                   help='List paths contained in the archive instead of extracting.')
    p.add_argument('--list-all', action='store_true',
                   help='List contained and referenced paths. For HashFS this also enables\n'
                        'deep path discovery. Use --list-output to save the result.')
    p.add_argument('--list-output', metavar='FILE',
                   help='Write --list-all results to FILE instead of printing every path.\n'
                        'Existing files are extended without duplicates. When extraction\n'
                        'is also requested, FILE is updated only after extraction finishes.')
    p.add_argument('--list-entries', action='store_true',
                   help='[HashFS] Print raw entry hashes, offsets, sizes and entry types.')
    p.add_argument('--log', nargs='?', const='_extraction.log', metavar='FILE',
                   help='Enable logging. Without a filename, writes .\\_extraction.log.')
    p.add_argument('--no-update', action='store_true',
                   help='Do not update references when invalid path characters force a rename.')
    p.add_argument('--force-extract', action='store_true',
                   help='Extract files even when a listing or tree option is also enabled.\n'
                        'Extraction is also forced by explicitly specifying --dest,\n'
                        '--separate, --skip-existing, --raw, --paths, --partial, or\n'
                        '--no-update.')
    p.add_argument('-p', '--partial', metavar='PATHS',
                   help='Extract only comma-separated files/directories, for example:\n'
                        '  -p=/locale\n  -p=/def,/map\n  -p=/def/world/road.sii')
    p.add_argument('-P', '--paths', dest='path_file', metavar='FILE',
                   help='Like --partial, but read one start path per line from FILE.')
    p.add_argument('-q', '--quiet', action='store_true',
                   help='Do not print a line for every extracted file. Summaries remain visible.')
    p.add_argument('-r', '--raw', action='store_true',
                   help='[HashFS] Dump entries under their hexadecimal hashes instead of\n'
                        'traversing the directory tree.')
    p.add_argument('--salt', type=int, metavar='NUMBER',
                   help='[HashFS] Override the archive path-hashing salt (0..65535).')
    p.add_argument('-S', '--separate', action='store_true',
                   help='When processing multiple archives, create a separate output\n'
                        'directory for each archive.')
    p.add_argument('-s', '--skip-existing', action='store_true',
                   help='Do not overwrite files that already exist in the destination.')
    p.add_argument('--table-at-end', action='store_true',
                   help='[HashFS v1] Read the entry table from the end of the archive,\n'
                        'ignoring the table offset stored in its header.')
    p.add_argument('--tree', action='store_true',
                   help='Print the archive directory tree instead of extracting.')
    p.add_argument('-?', '-h', '--help', dest='show_help', action='store_true',
                   help='Print this detailed usage information and exit.')
    return p


def print_usage(p: argparse.ArgumentParser) -> None:
    print('Universal SCS Extractor (Python port)\n')
    p.print_help()


def pause_if_standalone() -> None:
    """Keep a console opened by Explorer visible, without blocking shell usage."""
    # GUI runs the unchanged CLI in a hidden child process and captures its
    # output.  Such a process has no interactive console and must never wait
    # for a key after completing extraction.
    if (sys.platform != 'win32'
            or os.environ.get('UNIVERSAL_SCS_EXTRACTOR_GUI_CHILD') == '1'):
        return
    try:
        import ctypes
        process_ids = (ctypes.c_uint * 2)()
        if ctypes.windll.kernel32.GetConsoleProcessList(process_ids, 2) <= 1:
            import msvcrt
            print('\nPress any key to continue...', end='', flush=True)
            msvcrt.getwch()
    except (EOFError, OSError):
        pass


def read_path_list(filename: str) -> list[str]:
    raw = Path(filename).read_bytes()
    if raw.startswith(b'\xff\xfe\x00\x00'):
        text = raw.decode('utf-32')
    elif raw.startswith(b'\x00\x00\xfe\xff'):
        text = raw.decode('utf-32')
    elif raw.startswith((b'\xff\xfe', b'\xfe\xff')):
        text = raw.decode('utf-16')
    else:
        text = raw.decode('utf-8-sig')
    return [normalize_path(line.strip()) for line in text.splitlines()
            if line.strip() and not line.lstrip().startswith('#')]


def update_path_list(filename: str, paths: list[str], dry_run: bool = False) -> None:
    """Create or append to a path list without rewriting it or adding duplicates."""
    target = Path(filename)
    raw = target.read_bytes() if target.exists() else b''
    if raw.startswith(b'\xff\xfe\x00\x00'):
        encoding, bom_size = 'utf-32-le', 4
    elif raw.startswith(b'\x00\x00\xfe\xff'):
        encoding, bom_size = 'utf-32-be', 4
    elif raw.startswith(b'\xff\xfe'):
        encoding, bom_size = 'utf-16-le', 2
    elif raw.startswith(b'\xfe\xff'):
        encoding, bom_size = 'utf-16-be', 2
    elif raw.startswith(b'\xef\xbb\xbf'):
        encoding, bom_size = 'utf-8', 3
    else:
        encoding, bom_size = 'utf-8', 0
    try:
        existing_text = raw[bom_size:].decode(encoding) if raw else ''
    except UnicodeDecodeError:
        existing_text = raw.decode('utf-8', 'replace')
        encoding = 'utf-8'
    existing = {line.strip() for line in existing_text.splitlines() if line.strip()}
    additions = [path for path in paths if path not in existing]
    total = len(existing) + len(additions)
    if dry_run:
        action = 'create' if not target.exists() else 'update'
        print(f'Would {action} path list "{target}" ({len(additions)} new, {total} total)',
              file=sys.stderr)
        return
    if not target.exists():
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(('\n'.join(additions) + ('\n' if additions else '')).encode('utf-8'))
        print(f'Created path list "{target}" ({total} paths)', file=sys.stderr)
        return
    if additions:
        separator = '' if not existing_text or existing_text.endswith(('\n', '\r')) else '\n'
        appended = separator + '\n'.join(additions) + '\n'
        with target.open('ab') as stream:
            stream.write(appended.encode(encoding))
        print(f'Updated path list "{target}" ({len(additions)} added, {total} total)',
              file=sys.stderr)
    else:
        print(f'Path list "{target}" already contains all {total} paths', file=sys.stderr)


def record_community_paths(filename: str | None, paths: list[str]) -> None:
    """Append verified HashFS paths and their directories for opt-in sharing.

    This deliberately has no console output: it is an internal child-process
    hand-off, not a user-requested --list-output file.
    """
    if not filename:
        return
    target = Path(filename)
    with _community_path_lock:
        try:
            existing = set(read_path_list(str(target))) if target.exists() else set()
            # Directory hashes are useful search candidates in their own right:
            # finding one lets the extractor traverse another directory table.
            # Expand here, at the single publication boundary, so extraction,
            # list, tree and entry-table operations all behave identically.
            shareable = include_parent_directories(set(paths))
            additions = sorted(shareable - existing)
            if not additions:
                return
            target.parent.mkdir(parents=True, exist_ok=True)
            with target.open('a', encoding='utf-8', newline='\n') as stream:
                stream.writelines(path + '\n' for path in additions)
        except (OSError, UnicodeError, ValueError):
            # Community sharing must never affect extraction.
            logging.getLogger('CommunityPaths').debug('Could not record community paths',
                                                     exc_info=True)


def emit_path_listing(args, paths: list[str]) -> None:
    """Print a path listing or persist it when --list-output was requested."""
    if args.list_output:
        update_path_list(args.list_output, paths, args.dry_run)
    else:
        gui_child = os.environ.get('UNIVERSAL_SCS_EXTRACTOR_GUI_CHILD') == '1'
        if gui_child:
            print('__USEX_GUI_PATH_LIST_BEGIN__')
        print_path_list(paths)
        if gui_child:
            print('__USEX_GUI_PATH_LIST_END__')


def start_paths(args) -> list[str]:
    if args.path_file:
        return read_path_list(args.path_file)
    if args.partial:
        return [normalize_path(part.strip()) for part in args.partial.split(',') if part.strip()]
    return ['/']


def matches(path: str, patterns: list[str]) -> bool:
    if not patterns:
        return True
    for group in patterns:
        for pattern in group.split(','):
            if pattern.startswith('r/') and pattern.endswith('/'):
                if re.search(pattern[2:-1], path):
                    return True
            elif fnmatch.fnmatchcase(path, pattern):
                return True
    return False


def safe_target(root: Path, archive_path: str) -> Path:
    name = normalize_path(archive_path).lstrip('/')
    if not name:
        raise ValueError('Empty archive file path')
    # Windows rejects names that Unix accepts. Match the C# sanitization intent.
    parts = [re.sub(r'[<>:"\\|?*\x00-\x1f]', '_', part).rstrip(' .') or '_'
             for part in name.split('/')]
    candidate = root.joinpath(*parts)
    # Do not call Path.resolve() here.  Extraction workers create neighbouring
    # directories concurrently and Windows' filesystem-aware resolution can
    # observe different intermediate states for ``root`` and ``candidate``,
    # producing a false escape result for perfectly ordinary paths.  The
    # archive path has already been normalized (including rejection of a
    # leading ``..``), so a lexical absolute/common-path check is both safe and
    # deterministic.
    root_absolute = os.path.normcase(os.path.abspath(root))
    candidate_absolute = os.path.normcase(os.path.abspath(candidate))
    try:
        contained = os.path.commonpath((root_absolute, candidate_absolute)) == root_absolute
    except ValueError:  # Different Windows drives.
        contained = False
    if not contained:
        raise ValueError('Archive path escapes output directory')
    return candidate


def relative_output_path(archive_path: str) -> str:
    return '/'.join(re.sub(r'[<>:"\\|?*\x00-\x1f]', '_', part).rstrip(' .') or '_'
                    for part in normalize_path(archive_path).lstrip('/').split('/'))


def prepare_dumped_payload(data: bytes) -> tuple[bytes, str]:
    """Remove archive protection and decode unnamed SII payloads safely."""
    data = strip_hashfs_protection_trailer(data)
    marker = data[:64].find(ENTERTAINMENT_EXTENSION_MARKER)
    if marker >= 0 and marker + 4 < len(data):
        extension_index = data[marker + 4]
        if extension_index < len(ENTERTAINMENT_EXTENSIONS):
            return data, ENTERTAINMENT_EXTENSIONS[extension_index]
    inferred_kind = infer(data)
    extension = file_type_to_extension(inferred_kind)
    if inferred_kind == FileType.SII and data.startswith((b'3nK', b'ScsC')):
        try:
            data = decode_sii(data)
        except (ValueError, EOFError):
            # An undecodable original is still more useful than losing the
            # only copy merely because its path could not be recovered.
            pass
    return data, extension


def unique_hashfs_paths(reader: HashFsReader, paths: list[str]) -> list[str]:
    """Choose one output path for each physical HashFS entry.

    Content analysis can attach a useful alias before the archive's real
    hashed path is discovered.  If both survive, extracting both creates two
    identical files and inflates the summary.  Prefer the path whose CityHash
    is actually stored in the archive; use an alias only when no verified path
    for that physical entry is known.
    """
    selected: dict[int, tuple[bool, str]] = {}
    for path in sorted(set(paths)):
        entry = reader.get_entry(path)
        if entry is None or entry.is_directory:
            continue
        verified = hash_path(path, reader.salt) == entry.hash
        key = id(entry)
        current = selected.get(key)
        if current is None or (verified and not current[0]):
            selected[key] = (verified, path)
    return sorted(path for _verified, path in selected.values())


def substitute_references(data: bytes, path: str, replacements: dict[str, str]) -> bytes:
    if not replacements:
        return data
    suffix = Path(path).suffix.lower()
    if suffix == '.tobj':
        texture = Tobj.load(data)
        texture.texture_path = replacements.get(texture.texture_path, texture.texture_path)
        return texture.serialize()
    if suffix not in ('.sii', '.sui', '.mat'):
        return data
    text = data.decode('utf-8', 'replace')
    for old, new in sorted(replacements.items(), key=lambda pair: len(pair[0]), reverse=True):
        text = text.replace(old, new)
    return text.encode('utf-8')


def resolve_reference_path(reference: str, source_path: str) -> str:
    """Resolve an asset reference using Prism3D's path conventions."""
    reference = reference.strip().replace('\\', '/')
    if not reference:
        raise ValueError('Empty referenced path')
    # PMD look tables commonly store generated material names without a path.
    # These are CityHash-based automat materials, not siblings of the model.
    if ('/' not in reference
            and re.fullmatch(r'[0-9A-Fa-f]{16}\.mat', reference)):
        return normalize_path(f'/automat/{reference[:2]}/{reference}')
    if reference.startswith('/'):
        return normalize_path(reference)
    parent = PurePosixPath(normalize_path(source_path)).parent
    return normalize_path(str(parent / reference))


# Package-relative paths occur in a number of SII definitions.  The game may
# address an asset through the mount name of base.scs or of a non-map DLC.
# These are intentionally *not* normalised while parsing references: the
# mounted form is only probed in the final targeted HashFS lookup.
PACKAGE_ROOT_PREFIXES = (
    'base',
    'dlc_actros_tuning', 'dlc_bobcat', 'dlc_daf_2021',
    'dlc_daf_tuning_pack', 'dlc_daf_xd', 'dlc_daf_xf_electric',
    'dlc_dragon', 'dlc_dutch_style', 'dlc_east', 'dlc_flags', 'dlc_fr',
    'dlc_goodyear', 'dlc_heavy_cargo', 'dlc_hs_schoch', 'dlc_italia',
    'dlc_kassbohrer', 'dlc_krone', 'dlc_michelin', 'dlc_mighty_griffin',
    'dlc_north', 'dlc_oversize', 'dlc_pink_ribbon', 'dlc_prehistoric',
    'dlc_raven', 'dlc_renault_t', 'dlc_schwarzmuller', 'dlc_spain',
    'dlc_special_transport', 'dlc_toys', 'dlc_trailers', 'dlc_tuning',
    'dlc_vikings', 'dlc_volvo_tuning_pack', 'dlc_west', 'dlc_xmas',
    'dlc_zoos',
)
PACKAGE_ROOT_PREFIX_SET = frozenset(PACKAGE_ROOT_PREFIXES)


def resolve_reference_paths(paths: set[str], source_path: str) -> set[str]:
    resolved = set()
    for path in paths:
        try:
            value = str(path).strip().replace('\\', '/')
            resolved.add(resolve_reference_path(value, source_path))
            # A bare 64-bit material name is commonly generated below
            # /automat/, but PMD exporters also use the same spelling for a
            # sibling material beside the model.  Keep both candidates; the
            # archive hash lookup decides which convention is real.
            if (not value.startswith('/')
                    and re.fullmatch(r'[0-9A-Fa-f]{16}\.mat', value)):
                parent = PurePosixPath(normalize_path(source_path)).parent
                resolved.add(normalize_path(str(parent / value)))
        except (TypeError, ValueError):
            continue
    return resolved


def find_tobj_paths(data: bytes) -> set[str]:
    """Return every texture path stored in a TOBJ, including cube maps."""
    result = set()
    try:
        result.add(Tobj.load(data).texture_path)
    except (ValueError, EOFError, TypeError):
        pass
    # Legacy cube-map TOBJs contain several length-prefixed absolute DDS
    # strings; the high-level parser exposes only the primary one.
    result.update(match.decode('utf-8', 'ignore') for match in re.findall(
        rb'/[A-Za-z0-9_ +./\-]+\.(?:dds|tga|png)\b', data,
        flags=re.IGNORECASE))
    return {path for path in result if path}


def references(data: bytes, path: str) -> set[str]:
    data = strip_hashfs_protection_trailer(data)
    suffix = Path(path).suffix.lower()
    try:
        if suffix in ('.sii', '.sui'):
            result = find_sii_paths(data, path)
            text = decode_sii(data).decode('utf-8-sig', 'replace')
            for key in ('icon', 'description_file'):
                match = re.search(rf'(?m)^\s*{key}\s*:\s*"?([^"\s]+)', text)
                if match:
                    result.add(match.group(1))
            return resolve_reference_paths(result, path)
        if suffix == '.tobj':
            return resolve_reference_paths(find_tobj_paths(data), path)
        if suffix == '.pmd':
            return resolve_reference_paths(find_model_paths(data), path)
        if suffix == '.mat':
            return resolve_reference_paths(find_mat_paths(data, path), path)
        if suffix in ('.mat', '.font', '.soundref'):
            result = set(re.findall(r'/[A-Za-z0-9_+./\-]+\.[A-Za-z0-9_]+',
                                    data.decode('utf-8', 'replace')))
            return resolve_reference_paths(result, path)
    except (ValueError, UnicodeError, EOFError) as exc:
        logging.getLogger('HashFsPathFinder').warning('Cannot scan %s: %s', path, exc)
    return set()


def inferred_references(data: bytes) -> set[str]:
    """Find references in an entry whose filename has not yet been recovered."""
    data = strip_hashfs_protection_trailer(data)
    kind = infer(data)
    try:
        if kind == FileType.SII:
            result = find_sii_paths(data, '/')
            text = decode_sii(data).decode('utf-8-sig', 'replace')
            for key in ('icon', 'description_file'):
                match = re.search(rf'(?m)^\s*{key}\s*:\s*"?([^"\s]+)', text)
                if match:
                    result.add(match.group(1))
            return resolve_reference_paths(result, '/')
        if kind == FileType.TOBJ:
            return resolve_reference_paths(find_tobj_paths(data), '/')
        if kind == FileType.PMD:
            return resolve_reference_paths(find_model_paths(data), '/')
        if kind == FileType.MATERIAL:
            result = find_mat_paths(data, '/')
            content_hash = cityhash64(data)
            hash_text = f'{content_hash:016x}'
            result.add(f'/automat/{hash_text[:2]}/{hash_text}.mat')
            return resolve_reference_paths(result, '/')
        if kind == FileType.FONT:
            result = set(re.findall(r'image:(.*?),', data.decode('utf-8', 'replace')))
            return resolve_reference_paths(result, '/')
        if kind == FileType.SOUND_REF:
            result = set()
            for line in data.decode('utf-8', 'replace').strip('\ufeff\0').splitlines():
                if line.startswith('source=') and '"' in line and '#' in line:
                    result.add(line[line.index('"') + 1:line.index('#')])
            return resolve_reference_paths(result, '/')
    except (ValueError, UnicodeError, EOFError):
        pass
    return set()


def expand_candidates(paths: set[str]) -> set[str]:
    result = set(paths)
    model_family = ('.pmd', '.pmg', '.pmc', '.ppd', '.pma',
                    '.mat', '.tobj', '.dds')
    sector_family = ('.base', '.aux', '.snd', '.data', '.desc', '.layer')
    variants = {extension: tuple(item for item in model_family if item != extension)
                for extension in model_family}
    variants.update({extension: tuple(item for item in sector_family if item != extension)
                     for extension in sector_family})
    variants.update({'.bank': ('.bank.guids',), '.guids': ('.bank',)})
    for path in paths:
        archive_path = PurePosixPath(path)
        suffix = archive_path.suffix.lower()
        # Automat names are content hashes and represent materials only; a
        # synthetic PMD/TOBJ/DDS beside them would only create false probes.
        is_automat = str(archive_path).lower().startswith('/automat/')
        extensions = () if is_automat and suffix in model_family else variants.get(suffix, ())
        for extension in extensions:
            if suffix == '.guids' and archive_path.name.lower().endswith('.bank.guids'):
                result.add(str(archive_path.with_suffix('')))
            else:
                result.add(str(archive_path.with_suffix(extension)))
        parent = archive_path.parent
        while str(parent) not in ('/', '.', ''):
            result.add(str(parent))
            parent = parent.parent
    return result


def include_parent_directories(paths: set[str]) -> set[str]:
    """Return files/references together with every non-root parent path."""
    result = set(paths)
    for path in tuple(paths):
        parent = PurePosixPath(path).parent
        while str(parent) not in ('/', '.', ''):
            result.add(str(parent))
            parent = parent.parent
    return result


def localized_file_path(data: bytes, known_paths: set[str]) -> str | None:
    """Infer desktop.ini's directory from the localized sibling names.

    Windows writes these files with no absolute path in their contents.  We
    only accept a unique directory supported by at least two already verified
    siblings; the caller still verifies the resulting salted HashFS lookup.
    """
    try:
        text = data.decode('utf-8-sig')
    except UnicodeError:
        try:
            text = data.decode('utf-16')
        except UnicodeError:
            return None
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    if not lines or lines[0].lower() != '[localizedfilenames]':
        return None
    names = {
        line.split('=', 1)[0].strip().replace('\\', '/').rsplit('/', 1)[-1].lower()
        for line in lines[1:] if '=' in line and line.split('=', 1)[0].strip()
    }
    if len(names) < 2:
        return None
    parents = Counter(
        str(PurePosixPath(path).parent)
        for path in known_paths
        if PurePosixPath(path).name.lower() in names
    )
    if not parents:
        return None
    ranked = parents.most_common(2)
    parent, matches = ranked[0]
    if matches < 2 or (len(ranked) > 1 and ranked[1][1] == matches):
        return None
    return str(PurePosixPath(parent) / 'desktop.ini')


def deep_paths(reader, starts: list[str], additional: str | None,
               candidates: set[str] | None = None, *,
               include_references: bool = False) -> tuple[list[str], set[str]]:
    pending = [*starts]
    alias_readers = reader.readers if isinstance(reader, CombinedHashFs) else [reader]
    for alias_reader in alias_readers:
        pending.extend(alias_reader.path_aliases)
    if candidates:
        pending.extend(candidates)
    if additional:
        pending.extend(read_path_list(additional))
    found: set[str] = set()
    referenced: set[str] = set()
    visited: set[str] = set()
    visited_entries: set[int] = set()
    # Only these named formats can contribute paths in references().  Reading
    # every known payload first is particularly expensive for model archives:
    # large PMG/DDS files were decompressed even though references() immediately
    # ignored them based on their extension.
    reference_suffixes = {'.sii', '.sui', '.tobj', '.pmd', '.mat',
                          '.font', '.soundref'}
    content_alias_owners: set[HashFsReader] = set()
    deferred_reference_scans: list[tuple[object, str]] = []
    owner_positions = {id(owner): index for index, owner in enumerate(alias_readers)}
    phase_started = time.perf_counter()

    def log_phase(name: str) -> None:
        nonlocal phase_started
        now = time.perf_counter()
        logging.getLogger('HashFsPathFinder').info(
            '%s completed in %.2f s (%d paths, %d referenced)',
            name, now - phase_started, len(found), len(referenced))
        phase_started = now

    def entry_owner(entry) -> HashFsReader | None:
        if isinstance(reader, CombinedHashFs):
            return reader.owner_of(entry)
        return reader if reader.entries.get(entry.hash) is entry else None

    def physical_entry_key(entry) -> tuple[int, int]:
        owner = entry_owner(entry)
        return owner_positions.get(id(owner), len(owner_positions)), entry.offset

    def enqueue_references(paths: set[str]) -> None:
        """Probe referenced files and each of their possible parent listings.

        A model may name a sibling material relatively.  Once it resolves to
        an absolute virtual path, parent directory records can reveal further
        siblings even when the top-level listing was intentionally hidden.
        """
        for value in paths:
            try:
                path = normalize_path(value)
            except ValueError:
                continue
            if path not in visited:
                pending.append(path)
            parent = PurePosixPath(path).parent
            while str(parent) not in ('/', '.'):
                parent_path = str(parent)
                if parent_path not in visited:
                    pending.append(parent_path)
                parent = parent.parent

    reference_workers = parallel_worker_count()
    logging.getLogger('HashFsPathFinder').debug(
        'Using %d parallel path-analysis workers', reference_workers)

    def scan_named_reference(job):
        entry, path = job
        try:
            data = (reader.read_path_entry(entry, path)
                    if isinstance(reader, CombinedHashFs) else reader.read(path))
            return data, path
        except (ValueError, EOFError) as exc:
            logging.getLogger('HashFsPathFinder').warning(
                'Cannot scan %s: %s', path, exc)
            return None

    def scan_named_references(jobs):
        """Analyze independent named payloads concurrently, in input order."""
        if not jobs:
            return
        workers = min(reference_workers, len(jobs))
        loaded = []
        with ThreadPoolExecutor(max_workers=workers,
                                thread_name_prefix='scs-reference-scan') as pool:
            iterator = iter(jobs)
            futures = set()
            for _ in range(min(len(jobs), workers * 2)):
                futures.add(pool.submit(scan_named_reference, next(iterator)))
            while futures:
                completed, futures = wait(futures, return_when=FIRST_COMPLETED)
                for future in completed:
                    item = future.result()
                    if item is not None:
                        loaded.append(item)
                    try:
                        futures.add(pool.submit(scan_named_reference, next(iterator)))
                    except StopIteration:
                        pass
        batch_size = 512
        batches = [loaded[index:index + batch_size]
                   for index in range(0, len(loaded), batch_size)]
        if _path_hash_executor is None:
            analyzed_batches = map(_analyze_named_path_payload_batch, batches)
        else:
            analyzed_batches = _path_hash_executor.map(
                _analyze_named_path_payload_batch, batches, chunksize=1)
        for results in analyzed_batches:
            yield from results

    def explore(scan_references: bool = True) -> None:
        while True:
            reference_jobs = []
            while pending:
                path = pending.pop()
                try:
                    path = normalize_path(path)
                except ValueError:
                    continue
                if path in visited:
                    continue
                visited.add(path)
                entries = (reader.entries_for_path(path) if isinstance(reader, CombinedHashFs)
                           else [entry] if (entry := reader.get_entry(path)) is not None else [])
                if not entries:
                    continue
                if any(entry.is_directory for entry in entries):
                    try:
                        directories, files = reader.listing(path)
                        pending.extend(directories + files)
                    except (ValueError, EOFError) as exc:
                        logging.getLogger('HashFsPathFinder').warning(
                            'Cannot read directory %s: %s', path, exc)
                regular_entries = [entry for entry in entries if not entry.is_directory]
                for entry in entries:
                    visited_entries.add(id(entry))
                if not regular_entries:
                    continue
                found.add(path)
                if PurePosixPath(path).suffix.lower() not in reference_suffixes:
                    continue
                jobs = [(entry, path) for entry in regular_entries]
                if scan_references:
                    reference_jobs.extend(jobs)
                else:
                    deferred_reference_scans.extend(jobs)
            if not scan_references or not reference_jobs:
                break
            reference_jobs.sort(key=lambda job: physical_entry_key(job[0]))
            # Scan every archive's version of every path.  New references are
            # queued for another traversal wave after the whole batch finishes.
            for expanded in scan_named_references(reference_jobs):
                referenced.update(expanded)
                enqueue_references(expanded)
    # Fast first phase: verify candidate hashes and traverse directory tables,
    # but do not decompress any named file merely to inspect references.
    explore(scan_references=False)
    log_phase('Candidate verification')

    # Paths already present in ZIP archives are the cheapest and often the
    # most complete dictionary for a companion HashFS archive.  Try those,
    # root listings and user-supplied paths before reading the much larger
    # bundled path database.  If they identify every file, the database is not
    # opened at all.
    all_entries = list(reader.all_entries if isinstance(reader, CombinedHashFs)
                       else reader.entries.values())
    unresolved_entries = [
        entry for entry in all_entries
        if not entry.is_directory and id(entry) not in visited_entries
    ]
    # The user/community dictionaries are deliberately the first phase.  If
    # they resolve every physical file, avoid opening the bundled database and
    #—more importantly—avoid decompressing unknown payloads for inference.
    if not unresolved_entries and not include_references:
        return sorted(found), referenced

    # At least one file is still unknown, so the full analysis is now useful.
    # Start with references from the already named files before opening the
    # large bundled dictionary or probing unnamed payloads.
    for expanded in scan_named_references(deferred_reference_scans):
        referenced.update(expanded)
        enqueue_references(expanded)
    explore()
    log_phase('Known-payload reference analysis')
    if not any(entry for entry in all_entries
               if not entry.is_directory and id(entry) not in visited_entries):
        return sorted(found), referenced
    # Directory paths in the bundled database are valuable entry points when
    # a protected archive deliberately omits its root and standard parents.
    # Match unvisited directory hashes too, then let explore() traverse the
    # retained leaf listings (for example company/editor and locale trees).
    lookup_entries = [
        entry for entry in all_entries if id(entry) not in visited_entries
    ]
    if lookup_entries:
        resource = Path(__file__).with_name('DeepStartPaths.txt.gz')
        targets_by_salt: dict[int, set[int]] = {}
        for entry in lookup_entries:
            owner = reader.owner_of(entry) if isinstance(reader, CombinedHashFs) else reader
            if owner is not None:
                targets_by_salt.setdefault(owner.salt, set()).add(entry.hash)
        unsalted_hashes = targets_by_salt.pop(0, set())
        salted_batch: list[str] = []
        salted_futures = deque()

        def submit_salted_batch(batch: list[str]) -> None:
            # Keep only a bounded number of jobs alive.  Passing the entire
            # archive hash table with hundreds of queued batches made the
            # executor pickle many identical copies and could exhaust memory
            # on large combined archives.
            salted_futures.append(_path_hash_executor.submit(
                _filter_salted_path_batch, (batch, targets_by_salt)))
            if len(salted_futures) >= max(2, _path_hash_workers):
                pending.extend(salted_futures.popleft().result())

        with gzip.open(resource, 'rt', encoding='utf-8') as stream:
            for line in stream:
                line = line.strip()
                if not line:
                    continue
                hash_text, separator, candidate = line.partition('\t')
                if separator and len(hash_text) == 16:
                    if int(hash_text, 16) in unsalted_hashes:
                        pending.append(normalize_path(candidate))
                    if targets_by_salt and _path_hash_executor is not None:
                        salted_batch.append(candidate)
                        if len(salted_batch) >= 32768:
                            submit_salted_batch(salted_batch)
                            salted_batch = []
                    elif targets_by_salt:
                        pending.append(normalize_path(candidate))
                else:
                    pending.append(normalize_path(line))
        if salted_batch:
            submit_salted_batch(salted_batch)
        # Salted tables cannot use the precomputed unsalted hashes stored
        # beside the path database.  Return only the handful that occur in
        # this archive, without retaining the whole decompressed database.
        while salted_futures:
            pending.extend(salted_futures.popleft().result())
        explore()
    log_phase('Bundled path dictionary lookup')

    # ExtractorFork's second phase scans every still-unknown payload.  Those
    # payloads often reveal the paths needed to identify themselves or peers.
    unknown_scan_entries = [
        entry for entry in all_entries
        if id(entry) not in visited_entries and not entry.is_directory and entry.kind != 1
    ]
    unknown_scan_entries.sort(key=physical_entry_key)

    # Coalesce tens of thousands of tiny random reads into sequential ranges.
    # Besides being much faster on HDDs, this also caches complete small text
    # payloads which would otherwise be read again immediately below.
    unknown_probes = reader.read_entry_probes(unknown_scan_entries, 256)

    def scan_unknown_entry(entry):
        try:
            prefix, cached_payload, probe_error = unknown_probes[id(entry)]
            if probe_error is not None:
                raise probe_error
            inferred_kind = infer(prefix)
            path_bearing_kinds = {
                FileType.SII, FileType.SUI, FileType.TOBJ, FileType.PMD,
                FileType.MATERIAL, FileType.FONT, FileType.SOUND_REF,
            }
            unresolved = id(entry) not in visited_entries
            needs_full_payload = (
                inferred_kind in (FileType.MATERIAL, FileType.TOBJ)
                or (unresolved and inferred_kind in path_bearing_kinds)
                or (unresolved and inferred_kind == FileType.UNKNOWN
                                      and entry.size <= 1024 * 1024
                                      and _looks_like_path_text_prefix(prefix)))
            if not needs_full_payload:
                return entry, prefix, inferred_kind, set(), False
            clean_data = (strip_hashfs_protection_trailer(cached_payload)
                          if cached_payload is not None
                          else strip_hashfs_protection_trailer(
                              reader.read_entry(entry)))
            return entry, clean_data, inferred_kind, None, True
        except (ValueError, EOFError):
            return None

    def parallel_unknown_scans():
        """Read/inspect unknown payloads concurrently with bounded buffering."""
        if not unknown_scan_entries:
            return
        workers = parallel_worker_count()
        iterator = iter(unknown_scan_entries)
        loaded = []
        with ThreadPoolExecutor(max_workers=workers,
                                thread_name_prefix='scs-path-scan') as scan_pool:
            futures = set()
            for _ in range(min(len(unknown_scan_entries), workers * 2)):
                futures.add(scan_pool.submit(scan_unknown_entry, next(iterator)))
            while futures:
                completed, futures = wait(futures, return_when=FIRST_COMPLETED)
                for future in completed:
                    loaded.append(future.result())
                    try:
                        futures.add(scan_pool.submit(scan_unknown_entry, next(iterator)))
                    except StopIteration:
                        pass

        for item in loaded:
            if item is not None and not item[4]:
                yield item[:4]

        analysis = [item for item in loaded if item is not None and item[4]]
        batch_size = 512
        batches = [analysis[index:index + batch_size]
                   for index in range(0, len(analysis), batch_size)]
        payload_batches = ([item[1] for item in batch] for batch in batches)
        if _path_hash_executor is None:
            analyzed_batches = map(_analyze_unknown_path_payload_batch,
                                   payload_batches)
        else:
            analyzed_batches = _path_hash_executor.map(
                _analyze_unknown_path_payload_batch, payload_batches,
                chunksize=1)
        for batch, results in zip(batches, analyzed_batches):
            for item, result in zip(batch, results):
                (inferred_kind, discovered, alias_candidate, texture_paths,
                 relative_paths) = result
                yield (item[0], item[1], inferred_kind, discovered,
                       alias_candidate, texture_paths, relative_paths)

    alias_scan_cache = {}
    unnamed_relative_references: set[str] = set()
    for scanned in parallel_unknown_scans():
        if scanned is None:
            continue
        if len(scanned) == 4:
            entry, clean_data, inferred_kind, discovered = scanned
            alias_candidate = None
            material_texture_paths = ()
            relative_reference_paths = ()
        else:
            (entry, clean_data, inferred_kind, discovered, alias_candidate,
             material_texture_paths, relative_reference_paths) = scanned
        unnamed_relative_references.update(
            str(path).replace('\\', '/') for path in relative_reference_paths
            if path)
        owner_for_cache = entry_owner(entry)
        if (owner_for_cache is not None and len(clean_data) <= 1024 * 1024
                and inferred_kind in (FileType.MATERIAL, FileType.SII,
                                      FileType.JPEG, FileType.UNKNOWN)):
            alias_scan_cache.setdefault(owner_for_cache, []).append(
                (entry, clean_data, inferred_kind, material_texture_paths))
        try:
            # Generated automat materials are named after CityHash64 of their
            # complete contents.  A protected pack may replace the table hash,
            # but the canonical filename can still be reconstructed exactly.
            # Reject oversized/obvious decoy materials before binding an alias.
            if inferred_kind == FileType.MATERIAL and alias_candidate:
                    owner = entry_owner(entry)
                    if (owner is not None and owner.get_entry(alias_candidate) is None
                            and owner.add_path_alias(alias_candidate, entry)):
                        content_alias_owners.add(owner)
                        visited.discard(alias_candidate)
                        pending.append(alias_candidate)
            # A regular (non-packed) TOBJ contains the absolute path of its
            # DDS.  Protected archives sometimes destroy the TOBJ's table hash
            # but retain that path.  SCS convention stores both files beside
            # each other with the same stem, which lets us bind the real entry
            # to a useful path without guessing from its hash.
            if inferred_kind == FileType.TOBJ and alias_candidate:
                owner = entry_owner(entry)
                if (owner is not None and owner.get_entry(alias_candidate) is None
                        and owner.add_path_alias(alias_candidate, entry)):
                    content_alias_owners.add(owner)
                    # The seed pass may already have tested this valid path
                    # before this protected archive part was recoverable.
                    visited.discard(alias_candidate)
                    pending.append(alias_candidate)
            localized_path = (localized_file_path(clean_data, found | referenced)
                              if inferred_kind in (FileType.SII, FileType.UNKNOWN)
                              else None)
            if localized_path:
                matching_entries = (
                    reader.entries_for_path(localized_path)
                    if isinstance(reader, CombinedHashFs)
                    else ([candidate] if (candidate := reader.get_entry(localized_path))
                          is not None else [])
                )
                if any(candidate is entry for candidate in matching_entries):
                    visited.discard(localized_path)
                    pending.append(localized_path)
            if inferred_kind == FileType.SII:
                owner = entry_owner(entry)
                if owner is not None:
                    # Unlike references inside the file, a definition's own
                    # filename is usually absent from its payload.  Accept a
                    # convention-derived candidate only when its real HashFS
                    # lookup resolves to this exact physical entry.
                    for self_path in find_sii_self_paths(clean_data):
                        if owner.get_entry(self_path) is entry:
                            visited.discard(self_path)
                            pending.append(self_path)
                            break
        except (ValueError, UnicodeError, EOFError, TypeError):
            continue
        expanded = expand_candidates(discovered)
        referenced.update(expanded)
        enqueue_references(expanded)
    log_phase('Unknown-payload analysis')

    # An unnamed PMD/MAT/TOBJ still exposes relative child names, but resolving
    # them against '/' discards the only useful part of the relationship.  In
    # protected archives it is common for an entire model family to be hidden
    # while another, already recovered file reveals the containing directory.
    # Probe every relative child below verified parent directories and accept
    # only exact salted-hash matches from the table.  This is a bounded hash
    # join, not a guessed path: a candidate is never published unless the
    # archive itself verifies it.
    if unnamed_relative_references:
        tested_relative_candidates: set[str] = set()
        tested_parents: set[str] = set()
        for _round in range(6):
            parents = {
                str(PurePosixPath(path).parent)
                for path in found
                if PurePosixPath(path).suffix
            } - tested_parents
            if not parents:
                break
            tested_parents.update(parents)
            unresolved = [
                entry for entry in all_entries
                if not entry.is_directory and id(entry) not in visited_entries
            ]
            targets_by_salt: dict[int, set[int]] = {}
            for entry in unresolved:
                owner = entry_owner(entry)
                if owner is not None:
                    targets_by_salt.setdefault(owner.salt, set()).add(entry.hash)
            if not targets_by_salt:
                break

            matched: list[str] = []
            batch: list[str] = []
            futures = deque()

            def submit_relative_batch() -> None:
                nonlocal batch
                if not batch:
                    return
                current = batch
                batch = []
                if _path_hash_executor is None:
                    matched.extend(_filter_salted_path_batch(
                        (current, targets_by_salt)))
                    return
                futures.append(_path_hash_executor.submit(
                    _filter_salted_path_batch, (current, targets_by_salt)))
                if len(futures) >= max(2, _path_hash_workers):
                    matched.extend(futures.popleft().result())

            for parent in parents:
                source = str(PurePosixPath(parent) / '__unnamed_source__')
                for relative in unnamed_relative_references:
                    try:
                        candidate = resolve_reference_path(relative, source)
                    except (TypeError, ValueError):
                        continue
                    if candidate in tested_relative_candidates:
                        continue
                    tested_relative_candidates.add(candidate)
                    batch.append(candidate)
                    if len(batch) >= 32768:
                        submit_relative_batch()
            submit_relative_batch()
            while futures:
                matched.extend(futures.popleft().result())
            if not matched:
                continue
            pending.extend(matched)
            explore()
        log_phase('Relative-reference parent lookup')

    # Legacy MAT files normally use a TOBJ with the same stem.  Once protected
    # TOBJs have supplied their own paths, use those relative texture names to
    # recover the corresponding material entries as well.  This pass is
    # Exact table-hash matches are also useful for regular GDeflate TOBJs,
    # which cannot expose a filename until a path has already been recovered.
    # Protected aliases remain supported, while normal entries are accepted
    # only when ``get_entry(candidate) is entry`` below.
    for owner in content_alias_owners | set(alias_scan_cache):
        tobj_names: dict[str, list[str]] = {}
        for path in found:
            if path.lower().endswith('.tobj'):
                candidate_owner = (reader.owner_of(owner.get_entry(path))
                                   if isinstance(reader, CombinedHashFs)
                                   and owner.get_entry(path) is not None
                                   else owner)
                if candidate_owner is owner:
                    tobj_names.setdefault(
                        PurePosixPath(path).name.lower(), []).append(path)
        for alias in owner.path_aliases:
            if alias.lower().endswith('.tobj'):
                tobj_names.setdefault(PurePosixPath(alias).name.lower(), []).append(alias)
        cached_alias_entries = alias_scan_cache.get(owner, ())
        aliased_entry_ids = {id(value) for value in owner.path_aliases.values()}
        for entry, material_data, cached_kind, texture_paths in cached_alias_entries:
            if cached_kind != FileType.MATERIAL or id(entry) in aliased_entry_ids:
                continue
            candidates: list[str] = []
            for texture_path in texture_paths:
                name = PurePosixPath(str(texture_path).replace('\\', '/')).name.lower()
                candidates.extend(str(PurePosixPath(path).with_suffix('.mat'))
                                  for path in tobj_names.get(name, ()))
            for candidate in dict.fromkeys(candidates):
                existing = owner.get_entry(candidate)
                if existing is entry:
                    aliased_entry_ids.add(id(entry))
                    visited.discard(candidate)
                    pending.append(candidate)
                    break
                if existing is None and owner.add_path_alias(candidate, entry):
                    aliased_entry_ids.add(id(entry))
                    visited.discard(candidate)
                    pending.append(candidate)
                    break

        # Recover the small mod-manager/world-definition files which identify
        # themselves through their contents even when their table hashes were
        # replaced.  This deliberately runs only for the same protected owner
        # detected above.
        icon_paths: list[str] = []
        description_paths: list[str] = []
        remaining: list[tuple[object, bytes, FileType]] = []
        for entry, data, kind, _texture_paths in cached_alias_entries:
            if id(entry) in aliased_entry_ids:
                continue
            remaining.append((entry, data, kind))
            if kind != FileType.SII:
                continue
            try:
                text = decode_sii(data).decode('utf-8-sig', 'replace')
            except (ValueError, UnicodeError, EOFError):
                continue
            candidate = None
            if re.search(r'\bmod_package\s*:', text):
                candidate = '/manifest.sii'
                icon_paths.extend('/' + value.lstrip('/') for value in
                                  re.findall(r'(?m)^\s*icon\s*:\s*"?([^"\s]+)', text))
                description_paths.extend('/' + value.lstrip('/') for value in
                                         re.findall(r'(?m)^\s*description_file\s*:\s*"?([^"\s]+)', text))
            else:
                include = re.search(r'@include\s+"([^"/]+)"', text)
                if include:
                    included = PurePosixPath(include.group(1))
                    candidate = '/def/world/' + included.name.replace(
                        'traffic_rules_limit.', 'traffic_rules.').replace('.sui', '.sii')
            if candidate and owner.get_entry(candidate) is None and owner.add_path_alias(candidate, entry):
                aliased_entry_ids.add(id(entry))
                visited.discard(candidate)
                pending.append(candidate)

        for entry, data, kind in remaining:
            candidate = None
            if kind == FileType.JPEG and len(icon_paths) == 1:
                candidate = icon_paths[0]
            elif kind == FileType.UNKNOWN:
                text = data.decode('utf-8-sig', 'ignore')
                suffix = re.search(r'traffic_rule\.limit_\d+([a-z]+)', text)
                if suffix:
                    variant = suffix.group(1)
                    candidate = f'/def/world/traffic_rules_limit.{variant}.sui'
                elif (len(description_paths) == 1 and len(text) >= 20
                      and any(char.isspace() for char in text)):
                    candidate = description_paths[0]
            if candidate and owner.get_entry(candidate) is None and owner.add_path_alias(candidate, entry):
                visited.discard(candidate)
                pending.append(candidate)
    explore()
    log_phase('Protected-content alias recovery')

    # Verify same-stem package members in one bounded pass.  Doing this from
    # inside explore() recursively re-analyzed every newly found model and was
    # disproportionately expensive on archives containing tens of thousands
    # of materials.
    model_family = ('.pmd', '.pmg', '.pmc', '.ppd', '.pma',
                    '.mat', '.tobj', '.dds')
    sector_family = ('.base', '.aux', '.snd', '.data', '.desc', '.layer')
    sibling_candidates = set()
    expanded_stems = set()
    for path in tuple(found):
        archive_path = PurePosixPath(path)
        suffix = archive_path.suffix.lower()
        family = (model_family if suffix in model_family
                  else sector_family if suffix in sector_family else ())
        if not family or (suffix in model_family and
                          path.lower().startswith('/automat/')):
            continue
        stem = str(archive_path.with_suffix('')).lower()
        family_name = 'model' if suffix in model_family else 'sector'
        if (family_name, stem) in expanded_stems:
            continue
        expanded_stems.add((family_name, stem))
        sibling_candidates.update(
            str(archive_path.with_suffix(extension)) for extension in family)
    verified_siblings = []
    for candidate in sibling_candidates:
        entries = (reader.entries_for_path(candidate)
                   if isinstance(reader, CombinedHashFs)
                   else [entry] if (entry := reader.get_entry(candidate)) is not None
                   else [])
        if any(not entry.is_directory for entry in entries):
            verified_siblings.append(candidate)
    # These candidates used to be inserted directly into ``found``.  That
    # recovered the sibling itself but never inspected a newly recovered PMD,
    # MAT or TOBJ, so a perfectly ordinary PMD -> MAT -> TOBJ -> DDS chain
    # stopped at the first member discovered by this late pass.  Feed verified
    # candidates through the normal traversal instead.  Hash lookup above
    # still prevents speculative names from entering the result.
    pending.extend(verified_siblings)
    explore()
    log_phase('Same-stem package lookup')

    # Cube maps and reflection sets conventionally differ only in their final
    # numeric face index.  TOBJ references are occasionally incomplete, so
    # verify the neighbouring digits directly against the table hashes.
    numbered_candidates = set()
    for path in tuple(found):
        archive_path = PurePosixPath(path)
        match = re.match(r'^(.*?)(\d)(\.[^.]+)$', archive_path.name)
        if not match:
            continue
        prefix, _digit, suffix = match.groups()
        if suffix.lower() not in ('.dds', '.tobj', '.mat'):
            continue
        numbered_candidates.update(
            str(archive_path.with_name(f'{prefix}{digit}{suffix}'))
            for digit in range(10))
    verified_numbered = []
    for candidate in numbered_candidates:
        entries = (reader.entries_for_path(candidate)
                   if isinstance(reader, CombinedHashFs)
                   else [entry] if (entry := reader.get_entry(candidate)) is not None
                   else [])
        if any(not entry.is_directory for entry in entries):
            verified_numbered.append(candidate)
    # A numbered MAT/TOBJ can itself reveal another real archive path.
    pending.extend(verified_numbered)
    explore()
    log_phase('Numbered texture-family lookup')

    # Map sector names are generated rather than referenced by other assets.
    generated_sector_candidates = []
    for map_name in ('europe', 'usa'):
        for z in range(-70, 71):
            for x in range(-70, 71):
                base = f'/map/{map_name}/sec{x:+05d}{z:+05d}.base'
                if reader.get_entry(base) is not None:
                    generated_sector_candidates.append(base)
                    for extension in ('aux', 'snd', 'data', 'desc', 'layer'):
                        sibling = str(PurePosixPath(base).with_suffix('.' + extension))
                        if reader.get_entry(sibling) is not None:
                            generated_sector_candidates.append(sibling)
    pending.extend(generated_sector_candidates)
    explore()
    log_phase('Generated map-sector lookup')

    # A few definitions use the mounted package name in their resource path:
    # /base/def/... or /dlc_oversize/def/..., for example.  Do not rewrite
    # references globally — both spellings can be real — but, as the final
    # low-cost recovery phase, try every already verified file under known
    # non-map package roots against only the still-unresolved HashFS hashes.
    package_unresolved = [
        entry for entry in all_entries
        if not entry.is_directory and id(entry) not in visited_entries
    ]
    package_base_paths = sorted({
        normalize_path(path) for path in found
        if PurePosixPath(path).suffix
        and path.lstrip('/').partition('/')[0].casefold() not in PACKAGE_ROOT_PREFIX_SET
    })
    if package_unresolved and package_base_paths:
        package_targets_by_salt: dict[int, set[int]] = {}
        for entry in package_unresolved:
            owner = reader.owner_of(entry) if isinstance(reader, CombinedHashFs) else reader
            if owner is not None:
                package_targets_by_salt.setdefault(owner.salt, set()).add(entry.hash)
        if package_targets_by_salt:
            logging.getLogger('HashFsPathFinder').info(
                'Checking %d known paths below %d package roots for %d unresolved entries',
                len(package_base_paths), len(PACKAGE_ROOT_PREFIXES),
                len(package_unresolved))
            batch_size = 4096
            batches = [package_base_paths[index:index + batch_size]
                       for index in range(0, len(package_base_paths), batch_size)]
            if _path_hash_executor is None:
                for batch in batches:
                    pending.extend(_filter_package_root_path_batch(
                        (batch, package_targets_by_salt)))
            else:
                futures = deque()
                for batch in batches:
                    futures.append(_path_hash_executor.submit(
                        _filter_package_root_path_batch,
                        (batch, package_targets_by_salt)))
                    if len(futures) >= max(2, _path_hash_workers):
                        pending.extend(futures.popleft().result())
                while futures:
                    pending.extend(futures.popleft().result())
            explore()
            log_phase('Mounted package-root lookup')
    return sorted(found), referenced


def show_tree(paths: list[str]) -> None:
    tree: dict = {}
    for path in paths:
        node = tree
        for part in path.strip('/').split('/'):
            node = node.setdefault(part, {})

    gui_child = os.environ.get('UNIVERSAL_SCS_EXTRACTOR_GUI_CHILD') == '1'
    if gui_child:
        print('__USEX_GUI_PATH_TREE_BEGIN__')

    def render(node: dict, depth: int, ancestors: tuple[bool, ...] = (),
               parents: tuple[str, ...] = ()) -> None:
        items = sorted(node.items())
        for position, (name, child) in enumerate(items):
            is_last = position == len(items) - 1
            value = name + ('/' if child else '')
            if gui_child:
                # Keep the child-process protocol ASCII-only: a GUI can set
                # UTF-8 explicitly, but a manually invoked --gui-child must
                # still be safe in an old Windows console code page.
                flags = ''.join('1' if has_sibling else '0' for has_sibling in ancestors)
                print('__USEX_GUI_PATH_TREE_NODE__'
                      + flags + '\t' + ('1' if is_last else '0')
                      + '\t' + ('1' if child else '0')
                      + '\t/' + '/'.join(parents + (name,)) + '\t' + value)
                render(child, depth + 1, ancestors + (not is_last,),
                       parents + (name,))
            else:
                print('  ' * depth + value)
                render(child, depth + 1, parents=parents + (name,))
    render(tree, 0)
    if gui_child:
        print('__USEX_GUI_PATH_TREE_END__')


def open_archive(archive: Path, args, *, is_reference: bool = False):
    try:
        with archive.open('rb') as stream:
            magic = stream.read(4)
        if magic == b'SCS#':
            reader = HashFsReader(archive, salt=args.salt, table_at_end=args.table_at_end)
            dir_count = sum(entry.is_directory for entry in reader.entries.values())
            skipped_count = max(0, reader.entry_count - len(reader.entries))
            skipped_text = f', {skipped_count} rejected' if skipped_count else ''
            opened_message = (f'Opened {archive.name}: HashFS v{reader.version} archive; '
                              f'{reader.entry_count} entries '
                              f'({len(reader.entries) - dir_count} files, '
                              f'{dir_count} directory listings{skipped_text})')
            if not is_reference:
                print(opened_message, file=sys.stderr)
                logging.getLogger('HashFsReader').info(opened_message)
        else:
            reader = ZipReader(archive)
            opened_message = (f'Opened {archive.name}: ZIP archive; '
                              f'{len(reader.entries)} entries')
            if not is_reference:
                print(opened_message, file=sys.stderr)
                logging.getLogger('ZipReader').info(opened_message)
    except (OSError, ValueError, EOFError) as exc:
        print(f'{archive}: {exc}', file=sys.stderr)
        logging.getLogger('ArchiveReader').error(
            '[%s] Unable to open archive: %s', archive.name, exc)
        return None
    if (not is_reference and isinstance(reader, HashFsReader)
            and reader.rejected_entries):
        reasons = Counter(reason for _, reason in reader.rejected_entries)
        reason_order = ('DuplicateHash', 'MetadataOutOfBounds', 'InvalidMetadata',
                        'PayloadOutOfBounds', 'PayloadOverlapsTable',
                        'InvalidZlibHeader', 'SuspiciousCompressionRatio')
        reason_summary = ', '.join(f'{reason}: {reasons[reason]}' for reason in reason_order
                                   if reasons[reason])
        print(f'WARN: {len(reader.rejected_entries)} malformed or suspicious entries '
              f'were classified as junk ({reason_summary})', file=sys.stderr)
        logging.getLogger('HashFsReader').warning(
            '[%s] Classified %d malformed or suspicious entries as junk (%s)',
            archive.name, len(reader.rejected_entries), reason_summary)
    if (not is_reference and isinstance(reader, HashFsReader)
            and reader.unimplemented_entries):
        print(f'WARN: {len(reader.unimplemented_entries)} entries with unknown chunk type '
              f'have been skipped; some files may be missing', file=sys.stderr)
        logging.getLogger('HashFsReader').warning(
            '[%s] Skipped %d entries with unknown chunk type; some files may be missing',
            archive.name, len(reader.unimplemented_entries))
    return reader


def process(archive: Path, args, starts: list[str], shared_discovery=None,
            reader_override=None, paths_override: list[str] | None = None,
            defer_result: bool = False, listing_done: bool = False):
    reader = reader_override if reader_override is not None else open_archive(archive, args)
    if reader is None:
        return False
    raw_mode = args.raw and isinstance(reader, HashFsReader)
    deep_mode = (args.deep or bool(args.list_all)) and isinstance(reader, HashFsReader)
    if args.list_entries:
        if not isinstance(reader, HashFsReader):
            print('--list-entries requires HashFS', file=sys.stderr)
            return False
        # The raw table itself contains hashes rather than recoverable names,
        # but paths exposed by its directory listings are still verified and
        # should participate in the opt-in community database.
        try:
            known_table_paths = reader.walk(starts)
        except (ValueError, EOFError):
            known_table_paths = []
        record_community_paths(
            args.community_new_paths_output,
            [path for path in known_table_paths if matches(path, args.filter)])
        gui_child = os.environ.get('UNIVERSAL_SCS_EXTRACTOR_GUI_CHILD') == '1'
        if gui_child:
            print('__USEX_GUI_ENTRY_LIST_BEGIN__')
        print(f'ARCHIVE: {archive.name}')
        header = (f'{"HASH (path)":<16}  {"OFFSET (bytes)":>14}  '
                  f'{"SIZE (bytes)":>12}  {"STORED (bytes)":>14}  '
                  f'{"TYPE":<9}  STORAGE')
        print(header)
        print('-' * len(header))
        for entry in reader.entries.values():
            print(f'{entry.hash:016x}  {entry.offset:>14}  {entry.size:>12}  '
                  f'{entry.compressed_size:>14}  '
                  f'{"directory" if entry.is_directory else "file":<9}  '
                  f'{"compressed" if entry.is_compressed else "stored"}')
        if gui_child:
            print('__USEX_GUI_ENTRY_LIST_END__')
        if not args.force_extract:
            return True
    referenced: set[str] = set()
    if paths_override is not None:
        paths = paths_override
    elif isinstance(reader, HashFsReader) and shared_discovery is not None:
        shared_paths, referenced = shared_discovery
        paths = [path for path in shared_paths if (entry := reader.get_entry(path)) and not entry.is_directory]
    elif isinstance(reader, HashFsReader) and deep_mode:
        print('Searching for paths ...', file=sys.stderr)
        logging.getLogger('HashFsPathFinder').info(
            '[%s] Searching for paths', archive.name)
        paths, referenced = deep_paths(
            reader, starts, args.additional,
            include_references=bool(args.list_all))
        logging.getLogger('HashFsPathFinder').info(
            '[%s] Found %d file paths and %d referenced paths',
            archive.name, len(paths), len(referenced))
    elif raw_mode:
        paths = [f'{entry.hash:016x}' for entry in reader.entries.values() if not entry.is_directory]
    else:
        try:
            paths = reader.walk(starts)
        except (ValueError, EOFError) as exc:
            print(f'{archive}: {exc}', file=sys.stderr)
            return False
    paths = [path for path in paths if matches(path, args.filter)]
    # A list/tree operation also produces verified archive paths.  Share them
    # just like extracted paths; referenced-but-unverified candidates are not
    # present in ``paths`` and therefore never leave the computer.
    record_community_paths(args.community_new_paths_output, paths)
    deferred_listing: list[str] | None = None
    if (args.list or args.list_all or args.tree) and not listing_done:
        displayed_paths = set(paths) | (referenced if args.list_all else set())
        if args.list_all:
            displayed_paths = include_parent_directories(displayed_paths)
        displayed = sorted(displayed_paths)
        record_community_paths(args.community_new_paths_output, displayed)
        if args.tree:
            show_tree(displayed)
        elif args.force_extract:
            deferred_listing = displayed
        else:
            emit_path_listing(args, displayed)
        if not args.force_extract:
            return True
        if raw_mode:
            paths = [f'{entry.hash:016x}' for entry in reader.entries.values()
                     if not entry.is_directory]
    if isinstance(reader, HashFsReader) and not raw_mode:
        paths = unique_hashfs_paths(reader, paths)
    unresolved_table_entries = 0
    if (isinstance(reader, HashFsReader) and not deep_mode and not raw_mode
            and starts == ['/'] and not args.filter):
        # Without --deep the root listing names only a subset of the physical
        # table.  Make that loss visible instead of quietly reporting a clean
        # extraction: aliases collapse to the same Entry and count once.
        resolved_entries = {
            id(entry) for path in paths
            if (entry := reader.get_entry(path)) is not None and not entry.is_directory
        }
        unresolved_table_entries = sum(
            1 for entry in reader.entries.values()
            if not entry.is_directory and id(entry) not in resolved_entries)
    stats = ExtractionStats()
    if isinstance(reader, HashFsReader):
        # Rows rejected while parsing never reach extraction, but they still
        # belong in the final accounting instead of disappearing as "skipped".
        stats.junk = (len(reader.rejected_entries)
                      + len(reader.unimplemented_entries))
        stats.not_found += unresolved_table_entries
    if not (deep_mode or raw_mode) and isinstance(reader, HashFsReader):
        if starts == ['/'] and reader.get_entry('/') is None:
            print('Top level directory is missing; use --deep to scan contents for paths '
                  'before extraction or --partial to extract known paths', file=sys.stderr)
            logging.getLogger('HashFsExtractor').error(
                '[%s] Top level directory is missing', archive.name)
            # This is an archive-layout error, not a missing requested file.
            # Do not create an empty extraction group or per-archive summary;
            # the GUI will still render the operation-wide Total 0 line.
            return True
        for start in starts:
            entry = reader.get_entry(start)
            if entry is None:
                stats.not_found += 1
                if not args.quiet:
                    print(f'Path {start} was not found', file=sys.stderr)
            elif start == '/' and starts == ['/'] and entry.is_directory:
                try:
                    directories, files = reader.listing('/')
                    if not directories and not files:
                        print('Top level directory is empty; use --deep to scan contents for paths '
                              'before extraction or --partial to extract known paths', file=sys.stderr)
                except (ValueError, EOFError):
                    pass
    target_root = Path(args.dest) / archive.stem if args.separate else Path(args.dest)
    if raw_mode:
        target_root /= archive.name
    print(f'Extracting {archive.name} ...', file=sys.stderr)
    replacements = {}
    if not args.no_update and not raw_mode:
        for path in paths:
            renamed = '/' + relative_output_path(path)
            if renamed != path:
                replacements[path] = renamed
                replacements[path.lstrip('/')] = renamed.lstrip('/')
    extraction_logger = ('HashFsDeepExtractor'
                         if deep_mode
                         else 'HashFsExtractor' if isinstance(reader, HashFsReader)
                         else 'ZipExtractor')
    logging.getLogger(extraction_logger).info(
        '[%s] Extracting %d files%s', archive.name, len(paths),
        ' with known paths' if deep_mode else '')
    output_lock = Lock()

    def extract_one(path: str) -> ExtractionStats:
        result = ExtractionStats()
        try:
            was_modified = False
            if raw_mode:
                entry = reader.entries[int(path, 16)]
                files = reader.read_files_entry(entry, path)
            else:
                files = (reader.read_files(path) if isinstance(reader, HashFsReader)
                         else {path: reader.read(path)})
                if isinstance(reader, HashFsReader):
                    files = {name: strip_hashfs_protection_trailer(data)
                             for name, data in files.items()}
                if path.lower().endswith('.sii'):
                    files[path] = decode_sii(files[path])
                updated_files = {}
                for name, data in files.items():
                    updated = substitute_references(data, name, replacements)
                    was_modified |= updated != data
                    updated_files[name] = updated
                files = updated_files
            destinations = [(safe_target(target_root, file_path), data)
                            for file_path, data in files.items()]
            display_paths = [f'{archive.name}{normalize_path(file_path)}'
                             for file_path in files]
            if (args.skip_existing
                    and all(destination.exists() for destination, _ in destinations)):
                result.skipped = 1
                if not args.quiet:
                    with output_lock:
                        for display_path in display_paths:
                            _safe_print(f'Skipped {display_path}', file=sys.stderr,
                                        flush=True)
                return result
            if not args.dry_run:
                for destination, data in destinations:
                    if args.skip_existing and destination.exists():
                        continue
                    # Older path lists could leave an empty directory whose
                    # name is actually a file (for example ``texture.dds/``).
                    # A later, better candidate then failed with EACCES.  Only
                    # remove an empty exact-path directory; never discard any
                    # directory that contains user or extracted data.
                    if destination.is_dir():
                        try:
                            destination.rmdir()
                        except OSError as exc:
                            raise IsADirectoryError(
                                f'Output file path is a non-empty directory: '
                                f'{destination}') from exc
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    destination.write_bytes(data)
            # A regular archive entry produces one output file, while a packed
            # HashFS v2 GDeflate texture entry restores both TOBJ and DDS.
            # Count physical output files rather than source table entries.
            result.extracted = len(files)
            if relative_output_path(path) != normalize_path(path).lstrip('/'):
                result.renamed = 1
            if not raw_mode and was_modified:
                result.modified = 1
            if not args.quiet:
                action = 'Validated' if args.dry_run else 'Extracted'
                with output_lock:
                    for display_path in display_paths:
                        _safe_print(f'{action} {display_path}', file=sys.stderr,
                                    flush=True)
        except (OSError, ValueError, EOFError, KeyError) as exc:
            result.failed = 1
            with output_lock:
                _safe_print(f'Unable to extract {path}:', file=sys.stderr)
                _safe_print(str(exc), file=sys.stderr)
            logging.getLogger('Extractor').error(
                '[%s] Unable to extract %s: %s', archive.name, path, exc)
        return result

    def dump_one(entry) -> ExtractionStats:
        result = ExtractionStats()
        hash_text = f'{entry.hash:016x}'
        try:
            raw = reader.read_entry(entry) if entry.kind != 1 else next(
                iter(reader.read_files_entry(entry, '/' + hash_text).values()))
            raw, extension = prepare_dumped_payload(raw)
            filename = hash_text + extension
            destination = target_root / '_unknown' / filename
            if args.skip_existing and destination.exists():
                result.skipped = 1
                if not args.quiet:
                    with output_lock:
                        _safe_print(f'Skipped {archive.name}/_unknown/{filename}',
                                    file=sys.stderr, flush=True)
                return result
            if not args.dry_run:
                destination.parent.mkdir(parents=True, exist_ok=True)
                destination.write_bytes(raw)
            result.extracted = 1
            result.dumped = 1
            if not args.quiet:
                with output_lock:
                    _safe_print(f'Dumped {archive.name}/_unknown/{filename}',
                                file=sys.stderr, flush=True)
        except (OSError, ValueError, EOFError, KeyError) as exc:
            result.failed = 1
            with output_lock:
                _safe_print(f'Unable to dump {hash_text}: {exc}', file=sys.stderr)
            logging.getLogger('HashFsDeepExtractor').error(
                '[%s] Unable to dump %s: %s', archive.name, hash_text, exc)
        return result

    extraction_workers = parallel_worker_count()
    logging.getLogger(extraction_logger).info(
        '[%s] Using %d parallel extraction threads', archive.name, extraction_workers)
    with ThreadPoolExecutor(max_workers=extraction_workers,
                            thread_name_prefix='scs-extract') as file_pool:
        for partial_stats in file_pool.map(extract_one, paths):
            merge_stats(stats, partial_stats)

        if deep_mode and starts == ['/'] and not args.filter and not raw_mode:
            recovered = {reader.get_entry(path).hash for path in paths
                         if reader.get_entry(path) is not None}
            unknown_entries = [entry for entry in
                               sorted(reader.entries.values(), key=lambda item: item.hash)
                               if not entry.is_directory and entry.hash not in recovered]
            for partial_stats in file_pool.map(dump_one, unknown_entries):
                merge_stats(stats, partial_stats)

    if raw_mode:
        summary = (f'{stats.extracted} extracted, {stats.skipped} skipped, '
                   f'{stats.junk} junk, {stats.failed} failed')
    elif deep_mode:
        summary = (f'{stats.extracted} extracted ({stats.renamed} renamed, '
                   f'{stats.modified} modified, {stats.dumped} dumped), '
                   f'{stats.skipped} skipped, {stats.junk} junk, {stats.failed} failed')
    elif isinstance(reader, HashFsReader):
        summary = (f'{stats.extracted} extracted ({stats.renamed} renamed, '
                   f'{stats.modified} modified), {stats.skipped} skipped, '
                   f'{stats.not_found} not found, {stats.junk} junk, {stats.failed} failed')
    else:
        summary = (f'{stats.extracted} extracted ({stats.renamed} renamed, '
                   f'{stats.modified} modified), {stats.skipped} skipped, {stats.failed} failed')
    logging.getLogger(extraction_logger).info('[%s] %s', archive.name, summary)
    if defer_result:
        return summary
    print(summary, file=sys.stderr)
    if deferred_listing is not None:
        emit_path_listing(args, deferred_listing)
    # ExtractorFork reports per-file failures in the summary but considers the
    # archive itself processed successfully and continues with later archives.
    return True


def process_multiple_deep(archives: list[Path], args, starts: list[str],
                          reference_archives: list[Path] | None = None) -> int:
    """Mirror ExtractorFork's open/search/extract/report multi-archive phases."""
    opened = []
    for archive in archives:
        reader = open_archive(archive, args)
        if reader is not None:
            opened.append((archive, reader))
    if not opened:
        return 1

    reference_opened = []
    seen_paths = {path.resolve() for path, _reader in opened}
    for archive in reference_archives or []:
        try:
            resolved = archive.resolve()
        except OSError:
            resolved = archive
        if resolved in seen_paths:
            continue
        seen_paths.add(resolved)
        reader = open_archive(archive, args, is_reference=True)
        if reader is not None:
            reference_opened.append((archive, reader))

    actual_paths: set[str] = set()
    referenced_paths: set[str] = set()
    archive_references: set[str] = set()
    hash_readers = [reader for _archive, reader in opened + reference_opened
                    if isinstance(reader, HashFsReader)]
    combined = CombinedHashFs(hash_readers) if hash_readers else None
    perform_deep_search = bool(args.deep or args.list_all)

    total_files = 0
    total_directories = 0
    for _archive, reader in opened:
        if isinstance(reader, HashFsReader):
            total_directories += sum(entry.is_directory for entry in reader.entries.values())
            total_files += sum(not entry.is_directory for entry in reader.entries.values())
        else:
            total_directories += sum(name.endswith('/') for name in reader.entries)
            total_files += sum(not name.endswith('/') for name in reader.entries)
    total_entries = total_files + total_directories
    total_message = (f'Totally opened {total_entries} entries '
                     f'({total_files} files, {total_directories} directories found)')
    print(total_message, file=sys.stderr)
    logging.getLogger('Program').info(total_message)
    if reference_opened:
        reference_names = ', '.join(archive.name for archive, _reader in reference_opened)
        reference_message = f'Scanning reference archives: {reference_names}'
        print(reference_message, file=sys.stderr)
        logging.getLogger('HashFsPathFinder').debug(reference_message)
    if combined is not None and perform_deep_search:
        print('Searching for paths ...', file=sys.stderr)
        logging.getLogger('HashFsPathFinder').info(
            'Searching for paths across %d extraction archives and %d reference archives',
            len(opened), len(reference_opened))

    for _archive, reader in opened + reference_opened:
        if isinstance(reader, HashFsReader):
            if not perform_deep_search:
                try:
                    actual_paths.update(reader.walk(starts))
                except (ValueError, EOFError):
                    pass
            continue
        # ZIP/AEM entries already have real names.  If no HashFS archive is
        # present, collecting those names is the entire operation: do not scan
        # their contents or display a path-search phase.  In a mixed set they
        # also act as dependency archives for HashFS, so inspect only formats
        # known to contain useful references.
        contained = reader.walk(['/'])
        actual_paths.update(contained)
        if combined is None or not perform_deep_search:
            continue
        for path in contained:
            if PurePosixPath(path).suffix.lower() not in {
                    '.sii', '.sui', '.tobj', '.pmd', '.mat', '.font', '.soundref'}:
                continue
            try:
                archive_references.update(
                    expand_candidates(references(reader.read(path), path)))
            except (OSError, ValueError, EOFError):
                continue
    referenced_paths.update(archive_references)

    if combined is not None and perform_deep_search:
        try:
            # Every path exposed by the ZIP parts is a strong candidate for
            # the HashFS table as well.  Supplying only references forced the
            # deep scanner to rediscover ordinary same-name files through the
            # large built-in dictionary or by decompressing unknown payloads.
            zip_candidates = actual_paths | referenced_paths
            found, referenced = deep_paths(
                combined, starts, args.additional, zip_candidates,
                include_references=bool(args.list_all))
            logging.getLogger('HashFsPathFinder').info(
                'Found %d file paths and %d referenced paths across the combined entry table',
                len(found), len(referenced))
        except (OSError, UnicodeError, ValueError) as exc:
            print(f'Unable to read deep-search paths: {exc}', file=sys.stderr)
            return 1
        actual_paths.update(found)
        referenced_paths.update(referenced)

    deferred_listing: list[str] | None = None
    if args.list or args.list_all or args.tree:
        displayed = actual_paths | (referenced_paths if args.list_all else set())
        if args.list_all:
            displayed = include_parent_directories(displayed)
        displayed = sorted(path for path in displayed if matches(path, args.filter))
        record_community_paths(args.community_new_paths_output, displayed)
        if args.tree:
            show_tree(displayed)
        elif args.force_extract:
            deferred_listing = displayed
        else:
            emit_path_listing(args, displayed)
        if not args.force_extract:
            return 0

    everything = actual_paths | referenced_paths

    for archive, reader in opened:
        if isinstance(reader, HashFsReader):
            paths = sorted(path for path in everything
                           if (entry := reader.get_entry(path)) is not None
                           and not entry.is_directory)
        else:
            # ZipExtractor always extracts its contained entries in multi mode;
            # the shared path set is used to discover HashFS paths.
            paths = reader.walk(starts)
        result = process(archive, args, starts, reader_override=reader,
                         paths_override=paths, defer_result=True,
                         listing_done=True)
        if isinstance(result, str):
            # Report an archive as soon as it is complete.  The GUI uses this
            # boundary to fold that archive's detailed output, while the file
            # logger keeps its original messages and ordering.
            print(f'{archive.name}: {result}', file=sys.stderr)
    if deferred_listing is not None:
        emit_path_listing(args, deferred_listing)
    return 0


def print_plugins(manager: PluginManager) -> None:
    active = [plugin for plugin in manager.plugins if manager.is_active(plugin)]
    inactive = [plugin for plugin in manager.plugins if not manager.is_active(plugin)]
    print('Active plugins:')
    if active:
        for plugin in active:
            print(f'  [active] {plugin.info["name"]} {plugin.info["version"]} '
                  f'({plugin.info["id"]}, {plugin.kind.upper()})')
    else:
        print('  (none)')
    print('Available for activation:')
    if inactive:
        for plugin in inactive:
            print(f'  [disabled] {plugin.info["name"]} {plugin.info["version"]} '
                  f'({plugin.info["id"]}, {plugin.kind.upper()})')
    else:
        print('  (none)')
    if manager.errors:
        print('Plugin errors:')
        for error in manager.errors:
            print(f'  {error["path"]}: {error["message"]}')


def configure_plugins(args, raw_args: list[str]) -> tuple[PluginManager, bool]:
    """Apply CLI management actions and return (manager, handled_only)."""
    manager = PluginManager(application_directory())
    if args.plugin_load:
        installed = manager.install(args.plugin_load)
        for path in installed:
            print(f'Plugin installed: {path.name}', file=sys.stderr)
        manager = PluginManager(application_directory())
    for values, active, verb in ((args.plugin_disable, False, 'deactivated'),
                                 (args.plugin_activate, True, 'activated')):
        if not values:
            continue
        matched = manager.set_active(values, active)
        if matched:
            for plugin in matched:
                print(f'Plugin {verb}: {plugin.info["name"]}', file=sys.stderr)
        else:
            print(f'No plugins matched: {", ".join(values)}', file=sys.stderr)
    if args.plugin_list:
        print_plugins(manager)
        return manager, True
    manager.execute('console.initialize', {
        'arguments': raw_args, 'working_directory': str(Path.cwd())})
    return manager, False


def prompt_manifest_replacements() -> list[tuple[str, str]]:
    print('Enter manifest replacements as "old text" "new text" or OLD=NEW.')
    print('Press Enter on an empty line to finish.')
    replacements: list[tuple[str, str]] = []
    while True:
        try:
            line = input('manifest> ').strip()
        except EOFError:
            break
        if not line:
            break
        if '=' in line and not line.startswith(('"', "'")):
            replacements.extend(parse_replacements([line]))
            continue
        try:
            parts = shlex.split(line)
        except ValueError as exc:
            print(f'Invalid replacement: {exc}', file=sys.stderr)
            continue
        if len(parts) != 2 or not parts[0]:
            print('Invalid replacement; expected "old text" "new text"',
                  file=sys.stderr)
            continue
        replacements.append((parts[0], parts[1]))
    return replacements


def format_kilobytes(size: int) -> str:
    """Present archive sizes compactly and consistently for users."""
    return f'{size / 1024:.2f} KB'


def is_hashfs_v1_archive(path: Path) -> bool:
    """Return whether a file is specifically the format supported by --compress."""
    try:
        with path.open('rb') as stream:
            header = stream.read(6)
    except OSError:
        return False
    return len(header) == 6 and header[:4] == b'SCS#' and header[4:6] == b'\x01\x00'


def main(argv: list[str] | None = None) -> int:
    raw_args = list(sys.argv[1:] if argv is None else argv)
    argument_parser = parser()
    args = argument_parser.parse_args(raw_args)
    if args.show_help or not raw_args:
        print_usage(argument_parser)
        return 0
    if args.salt is not None and not 0 <= args.salt <= 65535:
        argument_parser.error('the salt must be a number between 0 and 65535')
    if args.list_output and not (args.list or args.list_all):
        argument_parser.error('--list-output requires --list or --list-all')
    if args.log:
        configure_logging(args.log, raw_args)
    try:
        plugin_manager, plugin_listed = configure_plugins(args, raw_args)
    except (OSError, ValueError) as exc:
        print(f'Plugin operation failed: {exc}', file=sys.stderr)
        return 2
    if plugin_listed:
        return 0
    plugin_management_only = bool(
        args.plugin_load or args.plugin_disable or args.plugin_activate)
    if plugin_management_only and not args.paths:
        return 0
    force_options = ('--dest', '--separate', '--skip-existing', '--raw',
                     '--paths', '--partial', '--no-update')
    force_short_options = ('-d', '-S', '-s', '-r', '-P', '-p')
    explicitly_forced = any(
        argument == option or argument.startswith(option + '=')
        for argument in raw_args for option in force_options
    ) or any(
        argument == option or (argument.startswith(option) and not argument.startswith('--'))
        for argument in raw_args for option in force_short_options
    )
    modes = (args.list or args.list_all, args.list_entries, args.tree)
    if sum(bool(mode) for mode in modes) > 1:
        argument_parser.error('--list/--list-all, --list-entries and --tree cannot be combined')
    # A listing mode is informational by default.  Its destination and other
    # extraction-related settings may still be configured in the GUI, but they
    # must not silently turn a file-list/tree/table request into extraction.
    # --force-extract remains the explicit opt-in for the combined operation.
    listing_mode = any(bool(mode) for mode in modes)
    args.force_extract = args.force_extract or (explicitly_forced and not listing_mode)
    starts = start_paths(args)
    archives: list[Path] = []
    for raw in args.paths:
        path = Path(raw)
        if args.all and path.is_dir():
            archives.extend(sorted(path.glob('*.scs')))
        else:
            archives.append(path)
    reference_archives = [Path(raw) for group in args.reference for raw in group]
    if reference_archives:
        args.deep = True
    if not archives:
        print('No .scs archives found', file=sys.stderr)
        return 2
    if (args.manifest or args.manifest_replace or args.edit
            or args.compress or args.just_compress):
        try:
            replacement_pairs = parse_replacements(args.manifest_replace)
        except ValueError as exc:
            argument_parser.error(str(exc))
        if args.edit:
            replacement_pairs.extend(prompt_manifest_replacements())
            if not replacement_pairs:
                print('Manifest editing cancelled: no replacements were entered.',
                      file=sys.stderr)
                return 0
            try:
                confirmed = input('Apply these manifest changes? [y/N] ').strip().lower()
            except EOFError:
                confirmed = ''
            if confirmed not in ('y', 'yes'):
                print('Manifest editing cancelled.', file=sys.stderr)
                return 0
        succeeded = True
        for archive in archives:
            try:
                if args.manifest or args.manifest_replace or args.edit:
                    print(f'Updating manifest in {archive.name} ...', file=sys.stderr)
                    result = edit_manifest(archive, replacement_pairs)
                    print(
                        f'SUCCESS: {archive.name}: manifest updated '
                        f'({result.removed_versions} compatible versions removed, '
                        f'{result.replacements} replacements)', file=sys.stderr)
                    logging.getLogger('ManifestEditor').info(
                        '[%s] Manifest updated: %d compatible versions removed, '
                        '%d replacements', archive.name, result.removed_versions,
                        result.replacements)
                if args.compress or args.just_compress:
                    if not is_hashfs_v1_archive(archive):
                        succeeded = False
                        print(
                            f'ERROR: {archive.name}: --compress supports only HashFS v1 archives; '
                            'the selected archive has a different format.',
                            file=sys.stderr)
                        logging.getLogger('HashFsV1Compressor').error(
                            '[%s] --compress requires HashFS v1', archive.name)
                        continue
                    print(f'Compressing {archive.name} ...', file=sys.stderr)
                    result = compress_hashfs_v1(
                        archive, compact=not args.just_compress)
                    saved = result.old_size - result.new_size
                    old_size = format_kilobytes(result.old_size)
                    new_size = format_kilobytes(result.new_size)
                    saved_size = format_kilobytes(saved)
                    print(
                        f'SUCCESS: {archive.name}: {result.changed_entries} entries '
                        f'compressed, {old_size} -> {new_size} '
                        f'({saved_size} saved)'
                        + (' without compaction' if args.just_compress else ''),
                        file=sys.stderr)
                    logging.getLogger('HashFsV1Compressor').info(
                        '[%s] %d entries compressed; %s -> %s (%s saved)', archive.name,
                        result.changed_entries, old_size, new_size, saved_size)
            except (OSError, EOFError, ValueError, zlib.error) as exc:
                succeeded = False
                print(f'ERROR: {archive.name}: {exc}', file=sys.stderr)
                logging.getLogger('ArchiveTools').error('[%s] %s', archive.name, exc)
        exit_code = 0 if succeeded else 1
        plugin_manager.execute('console.complete', {
            'arguments': raw_args, 'exit_code': exit_code,
            'working_directory': str(Path.cwd())})
        return exit_code
    community_candidate_file: Path | None = None
    gui_child = os.environ.get('UNIVERSAL_SCS_EXTRACTOR_GUI_CHILD') == '1'
    if not gui_child and (args.deep or args.list_all):
        has_hashfs = False
        for archive in archives + reference_archives:
            try:
                with archive.open('rb') as stream:
                    if stream.read(4) == b'SCS#':
                        has_hashfs = True
                        break
            except OSError:
                continue
        if has_hashfs:
            community_config = load_community_config()
            webhook_url = str(community_config.get('webhook_url', '')).strip()
            message_id = str(community_config.get('message_id', '')).strip()
            if (community_config.get('use') and valid_community_webhook(webhook_url)
                    and message_id.isdecimal()):
                try:
                    print('Loading public paths ...', file=sys.stderr)
                    public_paths = fetch_community_paths(webhook_url, message_id)
                    if args.additional:
                        public_paths.update(
                            read_community_local_paths(args.additional))
                    community_candidate_file = temporary_community_path_list(
                        public_paths)
                    args.additional = str(community_candidate_file)
                    logging.getLogger('HashFsPathFinder').info(
                        'Loaded %d public path candidates', len(public_paths))
                except (OSError, UnicodeError, ValueError) as exc:
                    print(f'WARNING: Unable to load public paths: {exc}',
                          file=sys.stderr)
    # GDeflate consists of independent 64 KiB tiles.  Keep one small worker
    # pool for the whole run so large packed textures do not block on a single
    # Python core.  ProcessPoolExecutor starts workers lazily, so ZIP-only runs
    # and archives without packed textures do not pay its startup cost.
    worker_count = parallel_worker_count()
    pool = ProcessPoolExecutor(max_workers=worker_count,
                               initializer=_ignore_worker_interrupt)
    configure_gdeflate_executor(pool, worker_count)
    configure_path_hash_executor(pool, worker_count)
    interrupted = False
    try:
        combined_processing = (
            (len(archives) > 1 or reference_archives)
            and (args.deep or args.list_all or args.tree)
            and not (args.list_entries and not args.force_extract))
        if combined_processing:
            exit_code = process_multiple_deep(archives, args, starts, reference_archives)
        else:
            results = [process(path, args, starts) for path in archives]
            exit_code = 0 if all(results) else 1
        plugin_manager.execute('console.complete', {
            'arguments': raw_args, 'exit_code': exit_code,
            'working_directory': str(Path.cwd())})
        return exit_code
    except KeyboardInterrupt:
        interrupted = True
        # Running ProcessPool tasks do not reliably receive Ctrl+C on Windows.
        # Stop them explicitly so they cannot outlive the CLI process and keep
        # writing output after the command has been cancelled.
        for worker in list(getattr(pool, '_processes', {}).values()):
            try:
                worker.terminate()
            except (AttributeError, OSError):
                pass
        raise
    finally:
        configure_path_hash_executor(None)
        configure_gdeflate_executor(None)
        pool.shutdown(wait=not interrupted, cancel_futures=True)
        if community_candidate_file is not None:
            community_candidate_file.unlink(missing_ok=True)


def should_launch_gui(arguments: list[str], executable: str | None = None) -> bool:
    """Use the GUI for Explorer-style launches: no options, paths only."""
    # A run started by the GUI is the actual extraction worker.  In
    # particular, one selected archive produces only one positional argument,
    # which would otherwise be mistaken for another Explorer/file-association
    # launch and recursively open a second GUI window.
    if os.environ.get('UNIVERSAL_SCS_EXTRACTOR_GUI_CHILD') == '1':
        return False
    return not any(argument.startswith('-') for argument in arguments)


def ensure_cli_console() -> None:
    """Create standard streams only after the windowed EXE enters CLI mode."""
    if sys.platform != 'win32' or not getattr(sys, 'frozen', False):
        return
    try:
        import ctypes
        import msvcrt
        from ctypes import wintypes
        kernel32 = ctypes.windll.kernel32
        kernel32.GetStdHandle.restype = ctypes.c_void_p
        if os.environ.get('UNIVERSAL_SCS_EXTRACTOR_GUI_CHILD') == '1':
            # Popen supplied an anonymous output pipe.  A windowed PyInstaller
            # build intentionally leaves sys.stdout unset, so wrap the Windows
            # standard handle directly without allocating a console.
            output_handle = kernel32.GetStdHandle(-11)  # STD_OUTPUT_HANDLE
            if output_handle in (None, 0, ctypes.c_void_p(-1).value):
                return
            descriptor = msvcrt.open_osfhandle(output_handle, os.O_WRONLY)
            stream = open(descriptor, 'w', encoding='utf-8', errors='replace',
                          buffering=1, closefd=False)
            sys.stdout = stream
            sys.stderr = stream
            return

        if '--console-session' in sys.argv:
            # A windowed PE started with CREATE_NEW_CONSOLE is not guaranteed
            # to receive Python text streams (notably in one-file PyInstaller
            # mode).  Bind CONIN/CONOUT explicitly before translation or input.
            kernel32.GetConsoleWindow.restype = wintypes.HWND
            if not kernel32.GetConsoleWindow() and not kernel32.AllocConsole():
                raise OSError('Unable to allocate an interactive console')
            kernel32.SetConsoleCP(65001)
            kernel32.SetConsoleOutputCP(65001)
            sys.stdin = open('CONIN$', 'r', encoding='utf-8', errors='replace')
            sys.stdout = open('CONOUT$', 'w', encoding='utf-8', errors='replace',
                              buffering=1)
            sys.stderr = open('CONOUT$', 'w', encoding='utf-8', errors='replace',
                              buffering=1)
            return

        # The dedicated CLI build is a real console-subsystem executable.
        # PyInstaller has already connected its streams to the caller, so do
        # not detach/reopen them; doing so breaks Ctrl+C and redirected output.
        if sys.stdout is not None and sys.stderr is not None:
            kernel32.SetConsoleCP(65001)
            kernel32.SetConsoleOutputCP(65001)
            for stream in (sys.stdout, sys.stderr):
                try:
                    stream.reconfigure(encoding='utf-8', errors='replace',
                                       line_buffering=True)
                except (AttributeError, OSError):
                    pass
            return

        # A one-file PyInstaller application has two processes. The Python
        # process therefore sees the windowed bootloader as its direct parent,
        # not PowerShell/cmd. Walk the ancestor chain and attach to the first
        # console found there instead of opening a second console window.
        class PROCESSENTRY32W(ctypes.Structure):
            _fields_ = (
                ('dwSize', wintypes.DWORD), ('cntUsage', wintypes.DWORD),
                ('th32ProcessID', wintypes.DWORD),
                ('th32DefaultHeapID', ctypes.c_size_t),
                ('th32ModuleID', wintypes.DWORD), ('cntThreads', wintypes.DWORD),
                ('th32ParentProcessID', wintypes.DWORD),
                ('pcPriClassBase', wintypes.LONG), ('dwFlags', wintypes.DWORD),
                ('szExeFile', wintypes.WCHAR * 260),
            )

        parent_by_pid: dict[int, int] = {}
        kernel32.CreateToolhelp32Snapshot.argtypes = (wintypes.DWORD, wintypes.DWORD)
        kernel32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
        kernel32.Process32FirstW.argtypes = (
            wintypes.HANDLE, ctypes.POINTER(PROCESSENTRY32W))
        kernel32.Process32NextW.argtypes = (
            wintypes.HANDLE, ctypes.POINTER(PROCESSENTRY32W))
        kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)
        snapshot = kernel32.CreateToolhelp32Snapshot(0x00000002, 0)
        invalid_handle = ctypes.c_void_p(-1).value
        if snapshot not in (None, 0, invalid_handle):
            entry = PROCESSENTRY32W()
            entry.dwSize = ctypes.sizeof(entry)
            if kernel32.Process32FirstW(snapshot, ctypes.byref(entry)):
                while True:
                    parent_by_pid[int(entry.th32ProcessID)] = int(
                        entry.th32ParentProcessID)
                    if not kernel32.Process32NextW(snapshot, ctypes.byref(entry)):
                        break
            kernel32.CloseHandle(snapshot)

        kernel32.GetConsoleWindow.restype = wintypes.HWND
        attached = bool(kernel32.GetConsoleWindow())
        process_id = int(kernel32.GetCurrentProcessId())
        visited = {process_id}
        for _ in range(16):
            process_id = parent_by_pid.get(process_id, 0)
            if not process_id or process_id in visited:
                break
            visited.add(process_id)
            if kernel32.AttachConsole(process_id):
                attached = True
                break
        if not attached:
            kernel32.AllocConsole()
        kernel32.SetConsoleCP(65001)
        kernel32.SetConsoleOutputCP(65001)
        sys.stdin = open('CONIN$', 'r', encoding='utf-8', errors='replace')
        sys.stdout = open('CONOUT$', 'w', encoding='utf-8', errors='replace', buffering=1)
        sys.stderr = open('CONOUT$', 'w', encoding='utf-8', errors='replace', buffering=1)
    except (OSError, AttributeError):
        pass


class _TranslatedConsoleStream:
    """Line-buffered live translation for the interactive console session."""

    def __init__(self, stream, language: str):
        self.stream = stream
        self.language = language
        self.pending = ''

    @property
    def encoding(self):
        return getattr(self.stream, 'encoding', 'utf-8')

    def write(self, value: str) -> int:
        from gui import translate_activity_message
        self.pending += value
        while '\n' in self.pending:
            line, self.pending = self.pending.split('\n', 1)
            ending = '\n'
            if line.endswith('\r'):
                line, ending = line[:-1], '\r\n'
            self.stream.write(translate_activity_message(line, self.language) + ending)
        return len(value)

    def flush(self) -> None:
        if self.pending:
            from gui import translate_activity_message
            self.stream.write(translate_activity_message(
                self.pending, self.language))
            self.pending = ''
        self.stream.flush()

    def __getattr__(self, name):
        return getattr(self.stream, name)


def _split_console_arguments(value: str) -> list[str]:
    """Split an interactive parameter line according to Windows quoting."""
    if sys.platform != 'win32':
        return shlex.split(value)
    try:
        import ctypes
        from ctypes import wintypes
        argc = ctypes.c_int()
        shell32 = ctypes.windll.shell32
        shell32.CommandLineToArgvW.argtypes = (
            wintypes.LPCWSTR, ctypes.POINTER(ctypes.c_int))
        shell32.CommandLineToArgvW.restype = ctypes.POINTER(wintypes.LPWSTR)
        argv = shell32.CommandLineToArgvW(value, ctypes.byref(argc))
        if not argv:
            raise OSError('CommandLineToArgvW failed')
        try:
            return [argv[index] for index in range(argc.value)]
        finally:
            ctypes.windll.kernel32.LocalFree(argv)
    except (AttributeError, OSError):
        return shlex.split(value, posix=False)


class _ResilientStream:
    """Proxy a console stream and ignore writes after its pipe is closed."""

    def __init__(self, stream):
        self.stream = stream

    def write(self, value):
        try:
            return self.stream.write(value)
        except (BrokenPipeError, OSError, ValueError):
            return len(value)

    def flush(self):
        try:
            return self.stream.flush()
        except (BrokenPipeError, OSError, ValueError):
            return None

    def __getattr__(self, name):
        return getattr(self.stream, name)


def _run_cli_once(arguments: list[str]) -> int:
    exit_code = 1
    original_stderr = sys.stderr
    if original_stderr is not None and not isinstance(original_stderr, _ResilientStream):
        sys.stderr = _ResilientStream(original_stderr)
    try:
        exit_code = main(arguments)
    except KeyboardInterrupt:
        _safe_print('\nCancelled.', file=sys.stderr)
        exit_code = 130
    except Exception:
        import traceback
        try:
            traceback.print_exc(file=sys.stderr)
        except (BrokenPipeError, OSError, ValueError):
            pass
        try:
            logging.getLogger('Program').exception('Unhandled exception')
        except (BrokenPipeError, OSError, ValueError):
            pass
        exit_code = 1
    finally:
        status = 'Success' if exit_code == 0 else 'Failed'
        exit_message = ('Additional Extraction process exiting with code %s (%s)'
                        if os.environ.get('UNIVERSAL_SCS_EXTRACTOR_NEW_PROCESS') == '1'
                        else 'Exiting with code %s (%s)')
        logging.getLogger('Program').info(exit_message, exit_code, status)
        try:
            logging.shutdown()
        except (BrokenPipeError, OSError, ValueError):
            pass
        root_logger = logging.getLogger()
        root_logger.handlers.clear()
        root_logger.addHandler(logging.NullHandler())
        sys.stderr = original_stderr
    return exit_code


def interactive_console_session(arguments: list[str]) -> int:
    """Run repeatedly in one console until the user submits an empty line."""
    language = 'en'
    clean_arguments: list[str] = []
    for argument in arguments:
        if argument == '--console-session':
            continue
        if argument.startswith('--ui-language='):
            language = argument.partition('=')[2] if argument.partition('=')[2] in ('ru', 'en') else 'en'
            continue
        clean_arguments.append(argument)

    if language == 'ru':
        prompt = ('\nВведите параметры для следующего запуска '
                  '(пустая строка — закрыть консоль):\n> ')
        result_text = '\nОперация завершена с кодом {code}.'
        bad_line = 'Не удалось разобрать параметры: {error}'
    else:
        prompt = ('\nEnter parameters for another run '
                  '(empty line closes the console):\n> ')
        result_text = '\nOperation finished with code {code}.'
        bad_line = 'Could not parse parameters: {error}'

    original_stdout, original_stderr = sys.stdout, sys.stderr
    if original_stdout is None or original_stderr is None:
        # Never construct a translating wrapper around a missing PyInstaller
        # stream: that hides the original failure behind AttributeError.
        ensure_cli_console()
        original_stdout, original_stderr = sys.stdout, sys.stderr
    if original_stdout is None or original_stderr is None:
        return 1
    if language == 'ru':
        sys.stdout = _TranslatedConsoleStream(original_stdout, language)
        sys.stderr = _TranslatedConsoleStream(original_stderr, language)
    try:
        current = clean_arguments
        last_code = 0
        while True:
            if current:
                last_code = _run_cli_once(current)
                print(result_text.format(code=last_code))
            try:
                line = input(prompt)
            except (EOFError, KeyboardInterrupt):
                print()
                return last_code
            if not line.strip():
                return last_code
            try:
                current = _split_console_arguments(line)
            except ValueError as exc:
                print(bad_line.format(error=exc), file=sys.stderr)
                current = []
    finally:
        if isinstance(sys.stdout, _TranslatedConsoleStream):
            sys.stdout.flush()
        if isinstance(sys.stderr, _TranslatedConsoleStream):
            sys.stderr.flush()
        sys.stdout, sys.stderr = original_stdout, original_stderr


def launch_console_session(arguments: list[str]) -> None:
    """Start the single windowed EXE in a new interactive console."""
    language_option = ([] if any(argument.startswith('--ui-language=')
                                 for argument in arguments)
                       else ['--ui-language=ru'])
    command = [sys.executable, '--console-session', *language_option, *arguments]
    flags = (subprocess.CREATE_NEW_CONSOLE | subprocess.CREATE_NEW_PROCESS_GROUP
             if sys.platform == 'win32' else 0)
    subprocess.Popen(command, creationflags=flags, cwd=Path.cwd())


if __name__ == '__main__':
    multiprocessing.freeze_support()
    launch_args = sys.argv[1:]
    # Explorer/file-association launches contain either no arguments or only
    # archive paths.  The presence of any option keeps the exact historical
    # console behaviour, including --help.
    gui_mode = should_launch_gui(launch_args, sys.executable)
    if gui_mode:
        try:
            from gui import run_gui
            exit_code = run_gui(launch_args)
        except Exception:
            import traceback
            traceback.print_exc()
            exit_code = 1
    elif ('--gui-child' in launch_args
          or os.environ.get('UNIVERSAL_SCS_EXTRACTOR_GUI_CHILD') == '1'):
        # The GUI supplied redirected stdout/stderr.  Run extraction in this
        # exact child process so its real exit code reaches the GUI.
        ensure_cli_console()
        exit_code = _run_cli_once(launch_args)
    elif (sys.platform == 'win32' and getattr(sys, 'frozen', False)
          and '--console-session' not in launch_args):
        # A windowed PE cannot behave synchronously as a native console app.
        # Start the same file in its interactive console mode instead.
        launch_console_session(launch_args)
        exit_code = 0
    elif '--console-session' in launch_args:
        ensure_cli_console()
        exit_code = interactive_console_session(launch_args)
    else:
        exit_code = _run_cli_once(launch_args)
    raise SystemExit(exit_code)

