"""Opt-in Discord-backed sharing of recovered SCS paths.

Only normalised virtual archive paths are transferred.  Archive names, local
folders and extracted file contents never leave the computer.
"""
from __future__ import annotations

import gzip
import base64
import ctypes
import json
import os
from pathlib import Path
import re
import tempfile
import sys
from threading import Event
from urllib.error import URLError
from urllib.request import Request, urlopen
import uuid


FILE_NAME = 'universal_scs_paths.txt.gz'
_WEBHOOK = re.compile(r'^https://(?:discord\.com|discordapp\.com)/api/webhooks/[^/]+/[^/]+/?$')


def _build_defaults() -> dict[str, object]:
    """Read optional values generated only for a configured release build."""
    try:
        from community_defaults import load_defaults
    except ImportError:
        return {}
    DEFAULT_WEBHOOK_URL, DEFAULT_STORAGE_MESSAGE_ID = load_defaults()
    if valid_webhook(DEFAULT_WEBHOOK_URL) and str(DEFAULT_STORAGE_MESSAGE_ID).isdecimal():
        return {'webhook_url': DEFAULT_WEBHOOK_URL,
                'message_id': str(DEFAULT_STORAGE_MESSAGE_ID)}
    return {}


def has_embedded_defaults() -> bool:
    """Whether this executable was built with a preconfigured endpoint."""
    return bool(_build_defaults())


def data_directory() -> Path:
    if getattr(sys, 'frozen', False):
        target = Path(sys.executable).resolve().parent
    else:
        target = Path(__file__).resolve().parent.parent
    target.mkdir(parents=True, exist_ok=True)
    return target


def config_path() -> Path:
    return data_directory() / 'community_paths.json'


def pending_path() -> Path:
    return data_directory() / 'pending_community_paths.txt'


def _legacy_path(filename: str) -> Path:
    root = Path(os.environ.get('LOCALAPPDATA', Path.home() / 'AppData' / 'Local'))
    return root / 'UniversalSCSExtractor' / filename


def load_config() -> dict[str, object]:
    data: dict[str, object]
    defaults = _build_defaults()
    try:
        target = config_path()
        if not target.exists():
            target = _legacy_path('community_paths.json')
        data = json.loads(target.read_text(encoding='utf-8'))
        if not isinstance(data, dict):
            data = {}
        protected = data.pop('webhook_protected', '')
        if protected and not defaults:
            try:
                data['webhook_url'] = _unprotect(str(protected))
            except OSError:
                # A copied profile is intentionally not usable under another
                # Windows account.  Ask for the webhook again instead.
                data.pop('webhook_url', None)
    except (OSError, ValueError):
        data = {}
    # Local DPAPI-protected configuration always wins.  Build defaults are a
    # convenience for a publisher's preconfigured release, not a replacement
    # for a user's own endpoint.
    # A configured release owns its endpoint.  Do not retain a second copy in
    # %LOCALAPPDATA%; only consent/preferences are stored there.
    if defaults:
        data.pop('webhook_url', None)
        data.pop('message_id', None)
    for key, value in defaults.items():
        data.setdefault(key, value)
    return data


def save_config(values: dict[str, object]) -> None:
    values = dict(values)
    webhook = str(values.pop('webhook_url', '') or '')
    values.pop('webhook_protected', None)
    if webhook and not has_embedded_defaults():
        try:
            values['webhook_protected'] = _protect(webhook)
        except OSError:
            # Non-Windows development builds retain functionality.  Release
            # builds target Windows and use DPAPI above.
            values['webhook_url'] = webhook
    target = config_path()
    temp = target.with_suffix('.tmp')
    temp.write_text(json.dumps(values, ensure_ascii=False, indent=2), encoding='utf-8')
    temp.replace(target)


class _DataBlob(ctypes.Structure):
    _fields_ = [('cbData', ctypes.c_uint32), ('pbData', ctypes.POINTER(ctypes.c_byte))]


def _dpapi(value: bytes, unprotect: bool = False) -> bytes:
    if os.name != 'nt':
        raise OSError('Windows DPAPI is unavailable')
    buffer = ctypes.create_string_buffer(value)
    source = _DataBlob(len(value), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_byte)))
    result = _DataBlob()
    crypt32 = ctypes.windll.crypt32
    kernel32 = ctypes.windll.kernel32
    if unprotect:
        ok = crypt32.CryptUnprotectData(ctypes.byref(source), None, None, None, None, 0,
                                        ctypes.byref(result))
    else:
        ok = crypt32.CryptProtectData(ctypes.byref(source), None, None, None, None, 0,
                                      ctypes.byref(result))
    if not ok:
        raise OSError(ctypes.get_last_error(), 'Windows DPAPI failed')
    try:
        return ctypes.string_at(result.pbData, result.cbData)
    finally:
        kernel32.LocalFree(result.pbData)


def _protect(value: str) -> str:
    return base64.b64encode(_dpapi(value.encode('utf-8'))).decode('ascii')


def _unprotect(value: str) -> str:
    return _dpapi(base64.b64decode(value), unprotect=True).decode('utf-8')


def valid_webhook(url: str) -> bool:
    return bool(_WEBHOOK.fullmatch(url.strip()))


class CommunityTransferCancelled(OSError):
    """Raised when the user cancels a community-path transfer."""


def _request(url: str, *, method: str = 'GET', data: bytes | None = None,
             headers: dict[str, str] | None = None,
             cancel_event: Event | None = None,
             response_holder: dict[str, object] | None = None) -> bytes:
    if cancel_event is not None and cancel_event.is_set():
        raise CommunityTransferCancelled('Community transfer cancelled')
    request = Request(url, data=data, method=method, headers={
        'User-Agent': 'UniversalSCSExtractor/1.0', **(headers or {})})
    with urlopen(request, timeout=20) as response:  # nosec B310: URL is validated webhook/Discord CDN
        if response_holder is not None:
            response_holder['response'] = response
        chunks: list[bytes] = []
        try:
            while True:
                if cancel_event is not None and cancel_event.is_set():
                    raise CommunityTransferCancelled('Community transfer cancelled')
                chunk = response.read(256 * 1024)
                if not chunk:
                    break
                chunks.append(chunk)
        finally:
            if response_holder is not None:
                response_holder.pop('response', None)
        return b''.join(chunks)


def _decode_paths(payload: bytes, filename: str = '') -> set[str]:
    if filename.lower().endswith('.gz') or payload.startswith(b'\x1f\x8b'):
        payload = gzip.decompress(payload)
    from trucklib_core import normalize_path
    result: set[str] = set()
    for line in payload.decode('utf-8-sig').splitlines():
        line = line.strip()
        if not line or line.startswith('#'):
            continue
        try:
            result.add(normalize_path(line))
        except ValueError:
            continue
    return result


def fetch_paths(webhook_url: str, message_id: str,
                cancel_event: Event | None = None,
                response_holder: dict[str, object] | None = None) -> set[str]:
    """Return the attached shared list from the single configured message."""
    if not valid_webhook(webhook_url) or not message_id.isdecimal():
        raise ValueError('Webhook URL or storage message ID is invalid')
    message = json.loads(_request(
        webhook_url.rstrip('/') + '/messages/' + message_id,
        cancel_event=cancel_event, response_holder=response_holder).decode('utf-8'))
    attachments = message.get('attachments', [])
    if not attachments:
        return set()
    # During recovery from an interrupted edit Discord can temporarily retain
    # an older attachment as well.  Prefer the newest path-list attachment,
    # not whichever item happens to be first in the JSON array.
    path_attachments = [item for item in attachments
                        if str(item.get('filename', '')) == FILE_NAME]
    attachment = (path_attachments or attachments)[-1]
    return _decode_paths(_request(str(attachment['url']), cancel_event=cancel_event,
                                  response_holder=response_holder),
                         str(attachment.get('filename', '')))


def _multipart(filename: str, payload: bytes, content: str = '') -> tuple[bytes, str]:
    # Older callers supplied a technical English status line.  Normalize it
    # here so both new and existing creation paths render the same clean,
    # user-facing message without Debug-style metadata.
    if filename == FILE_NAME and 'Universal SCS Extractor' in content:
        count_match = re.search(r'`([\d,]+)`', content)
        count = int(count_match.group(1).replace(',', '')) if count_match else 0
        content = _storage_message_content(count)
    boundary = '----UniversalSCS' + uuid.uuid4().hex
    parts = [
        f'--{boundary}\r\nContent-Disposition: form-data; name="payload_json"\r\n\r\n'.encode(),
        json.dumps({'content': content, 'attachments': [{'id': 0, 'filename': filename}]}).encode(), b'\r\n',
        (f'--{boundary}\r\nContent-Disposition: form-data; name="files[0]"; '
         f'filename="{filename}"\r\nContent-Type: application/gzip\r\n\r\n').encode(),
        payload, b'\r\n', f'--{boundary}--\r\n'.encode(),
    ]
    return b''.join(parts), boundary


def _storage_message_content(path_count: int) -> str:
    """Human-facing text for the public Discord storage message."""
    return (
        '# \U0001f5c2\ufe0f Universal SCS Extractor — Path Database\n'
        '> A shared list of verified paths from SCS archives.\n\n'
        f'**Verified paths:** `{path_count:,}`\n'
        f'**Database file:** `{FILE_NAME}`')


def publish_paths(webhook_url: str, message_id: str, paths: set[str],
                  cancel_event: Event | None = None,
                  response_holder: dict[str, object] | None = None) -> None:
    """Atomically replace the attachment while retaining just one message."""
    if not valid_webhook(webhook_url) or not message_id.isdecimal():
        raise ValueError('Webhook URL or storage message ID is invalid')
    text = ('# Universal SCS Extractor community paths\n' +
            ''.join(path + '\n' for path in sorted(paths))).encode('utf-8')
    packed = gzip.compress(text, compresslevel=9, mtime=0)
    body, boundary = _multipart(
        FILE_NAME, packed,
        f'## 🗂️ Universal SCS Extractor — Public Paths\n'
        f'**Verified paths:** `{len(paths):,}`\n'
        'This attachment is updated in place by the extractor. Do not delete this message.')
    response = _request(webhook_url.rstrip('/') + '/messages/' + message_id, method='PATCH', data=body,
                        headers={'Content-Type': f'multipart/form-data; boundary={boundary}'},
                        cancel_event=cancel_event, response_holder=response_holder)
    try:
        attachments = json.loads(response.decode('utf-8')).get('attachments', [])
    except (UnicodeDecodeError, ValueError) as exc:
        raise OSError('Discord returned an invalid message after path publication') from exc
    if len(attachments) != 1 or attachments[0].get('filename') != FILE_NAME:
        raise OSError('Discord did not replace the shared path-list attachment')


def create_storage(webhook_url: str, paths: set[str] | None = None) -> str:
    """Create an initial single-message storage object and return its ID."""
    if not valid_webhook(webhook_url):
        raise ValueError('Webhook URL is invalid')
    paths = paths or set()
    text = ('# Universal SCS Extractor public path database\n'
            '# Verified virtual SCS paths only; generated by Universal SCS Extractor.\n' +
            ''.join(path + '\n' for path in sorted(paths))).encode('utf-8')
    packed = gzip.compress(text, compresslevel=9, mtime=0)
    body, boundary = _multipart(
        FILE_NAME, packed,
        f'## 🗂️ Universal SCS Extractor — Public Paths\n'
        f'**Verified paths:** `{len(paths):,}`\n'
        'This attachment is updated in place by the extractor. Do not delete this message.')
    response = _request(webhook_url.rstrip('/') + '?wait=true', method='POST', data=body,
                        headers={'Content-Type': f'multipart/form-data; boundary={boundary}'})
    return str(json.loads(response.decode('utf-8'))['id'])


def delete_message(webhook_url: str, message_id: str) -> None:
    if not valid_webhook(webhook_url) or not message_id.isdecimal():
        raise ValueError('Webhook URL or storage message ID is invalid')
    _request(webhook_url.rstrip('/') + '/messages/' + message_id, method='DELETE')


def read_pending() -> set[str]:
    target = pending_path()
    if not target.exists():
        target = _legacy_path('pending_community_paths.txt')
    return _decode_paths(target.read_bytes(), target.name) if target.exists() else set()


def read_local_paths(filename: str) -> set[str]:
    """Read a user-supplied list with the same tolerant rules as the CLI."""
    target = Path(filename)
    return _decode_paths(target.read_bytes(), target.name) if target.exists() else set()


def write_pending(paths: set[str]) -> None:
    target = pending_path()
    if not paths:
        target.unlink(missing_ok=True)
        return
    target.write_text(''.join(path + '\n' for path in sorted(paths)), encoding='utf-8')


def temporary_path_list(paths: set[str]) -> Path:
    handle, name = tempfile.mkstemp(prefix='usex-community-', suffix='.txt')
    with os.fdopen(handle, 'w', encoding='utf-8', newline='\n') as stream:
        stream.writelines(path + '\n' for path in sorted(paths))
    return Path(name)
