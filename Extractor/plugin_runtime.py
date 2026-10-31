"""Plugin discovery, persistent state, and execution for PYD/DLL plugins."""
from __future__ import annotations

import ctypes
from dataclasses import dataclass
import importlib.util
import json
import os
from pathlib import Path
import shutil
import sys
from typing import Any

PLUGIN_API_VERSION = 1
STATE_FILENAME = 'plugins.json'
SUPPORTED_SUFFIXES = {'.pyd', '.dll'}
_LOADED_NATIVE_LIBRARIES: list[Any] = []


@dataclass
class LoadedPlugin:
    path: Path
    info: dict[str, Any]
    execute_function: Any
    backend: Any = None
    kind: str = 'pyd'

    def execute(self, event: str, context: dict[str, Any]) -> dict[str, Any]:
        if self.kind == 'dll':
            encoded = json.dumps(context, ensure_ascii=False).encode('utf-8')
            raw = self.execute_function(event.encode('utf-8'), encoded)
            if raw is None:
                return {}
            result = json.loads(raw.decode('utf-8'))
        else:
            result = self.execute_function(event, context)
            if result is None:
                return {}
        if not isinstance(result, dict):
            raise ValueError('Plugin result must be a JSON object/dictionary')
        return result


class PluginManager:
    def __init__(self, directory: str | Path):
        self.directory = Path(directory).resolve()
        self.state_path = self.directory / STATE_FILENAME
        self.plugins: list[LoadedPlugin] = []
        self.errors: list[dict[str, str]] = []
        self._dll_directories: list[Any] = []
        self.state = self._read_state()
        self._delete_pending()
        self._discover()

    def _record_error(self, path: Path, exc: BaseException) -> None:
        self.errors.append({'path': str(path), 'message': str(exc)})

    def _read_state(self) -> dict[str, Any]:
        if not self.state_path.is_file():
            return {'plugins': {}}
        try:
            value = json.loads(self.state_path.read_text(encoding='utf-8-sig'))
            if isinstance(value, dict) and isinstance(value.get('plugins'), dict):
                return value
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            self._record_error(self.state_path, exc)
        return {'plugins': {}}

    def save_state(self) -> None:
        self.state_path.write_text(
            json.dumps(self.state, ensure_ascii=False, indent=2) + '\n',
            encoding='utf-8')

    def _delete_pending(self) -> None:
        pending = self.state.get('pending_delete', [])
        if not isinstance(pending, list):
            pending = []
        remaining = []
        for value in pending:
            path = Path(value)
            try:
                if path.is_file():
                    path.unlink()
            except OSError:
                remaining.append(str(path))
        self.state['pending_delete'] = remaining
        if pending != remaining:
            self.save_state()

    def _candidates(self) -> list[tuple[str, Path]]:
        result: list[tuple[str, Path]] = []
        for suffix, kind in (('.pyd', 'pyd'), ('.dll', 'dll')):
            result.extend((kind, path) for path in sorted(self.directory.glob('*' + suffix)))
        extra = self.directory / 'Plugins'
        if extra.is_dir():
            for suffix, kind in (('.pyd', 'pyd'), ('.dll', 'dll')):
                result.extend((kind, path) for path in sorted(extra.rglob('*' + suffix)))
        pending = {str(Path(value).resolve()).casefold()
                   for value in self.state.get('pending_delete', [])}
        return [(kind, path) for kind, path in result
                if str(path.resolve()).casefold() not in pending]

    def _discover(self) -> None:
        loaded_ids: set[str] = set()
        for kind, path in self._candidates():
            try:
                plugin = self._load_pyd(path) if kind == 'pyd' else self._load_dll(path)
                if plugin is None:
                    continue
                plugin_id = plugin.info['id'].casefold()
                if plugin_id in loaded_ids:
                    continue
                loaded_ids.add(plugin_id)
                self.plugins.append(plugin)
            except Exception as exc:
                self._record_error(path, exc)

    @staticmethod
    def _validate_info(info: Any) -> dict[str, Any]:
        if not isinstance(info, dict):
            raise ValueError('Plugin information must be a dictionary/JSON object')
        if info.get('api_version') != PLUGIN_API_VERSION:
            raise ValueError(
                f"Unsupported plugin API {info.get('api_version')!r}; "
                f'expected {PLUGIN_API_VERSION}')
        for field in ('id', 'name', 'version'):
            if not isinstance(info.get(field), str) or not info[field].strip():
                raise ValueError(f'Missing plugin information field: {field}')
        if not isinstance(info.get('settings', {}), dict):
            raise ValueError('Plugin settings metadata must be an object')
        return dict(info)

    def _load_pyd(self, path: Path) -> LoadedPlugin | None:
        module_name = path.name.split('.', 1)[0]
        module = sys.modules.get(module_name)
        if module is None:
            spec = importlib.util.spec_from_file_location(module_name, path)
            if spec is None or spec.loader is None:
                raise ValueError('Unable to create PYD plugin module')
            module = importlib.util.module_from_spec(spec)
            sys.modules[module_name] = module
            try:
                spec.loader.exec_module(module)
            except Exception:
                sys.modules.pop(module_name, None)
                raise
        info = getattr(module, 'PLUGIN_INFO', None)
        if info is None:
            return None
        execute = getattr(module, 'execute', None)
        if not callable(execute):
            raise ValueError('PYD plugin must export execute(event, context)')
        return LoadedPlugin(path, self._validate_info(info), execute,
                            backend=module, kind='pyd')

    def _load_dll(self, path: Path) -> LoadedPlugin | None:
        if os.name == 'nt' and hasattr(os, 'add_dll_directory'):
            self._dll_directories.append(os.add_dll_directory(str(path.parent.resolve())))
        library = ctypes.CDLL(str(path.resolve()))
        _LOADED_NATIVE_LIBRARIES.append(library)
        try:
            info_function = library.use_plugin_info
        except AttributeError:
            return None
        info_function.argtypes = []
        info_function.restype = ctypes.c_char_p
        execute = library.use_plugin_execute
        execute.argtypes = [ctypes.c_char_p, ctypes.c_char_p]
        execute.restype = ctypes.c_char_p
        raw_info = info_function()
        if raw_info is None:
            raise ValueError('use_plugin_info returned NULL')
        info = self._validate_info(json.loads(raw_info.decode('utf-8')))
        return LoadedPlugin(path, info, execute, backend=library, kind='dll')

    def _state_for(self, plugin: LoadedPlugin) -> dict[str, Any]:
        plugins = self.state.setdefault('plugins', {})
        current = plugins.setdefault(plugin.info['id'], {})
        if not isinstance(current, dict):
            current = {}
            plugins[plugin.info['id']] = current
        return current

    def is_active(self, plugin: LoadedPlugin) -> bool:
        current = self._state_for(plugin)
        return bool(current.get('enabled', plugin.info.get('active_by_default', False)))

    def settings_for(self, plugin: LoadedPlugin) -> dict[str, Any]:
        schema = plugin.info.get('settings', {})
        current = self._state_for(plugin).get('settings', {})
        current = current if isinstance(current, dict) else {}
        return {key: current.get(key, definition.get('default'))
                for key, definition in schema.items() if isinstance(definition, dict)}

    def set_settings(self, plugin: LoadedPlugin, settings: dict[str, Any]) -> None:
        self._state_for(plugin)['settings'] = dict(settings)
        self.save_state()

    @staticmethod
    def _selectors(values: list[str] | tuple[str, ...] | str) -> list[str]:
        values = [values] if isinstance(values, str) else values
        return [item.strip() for value in values for item in value.split(',') if item.strip()]

    def matching(self, selectors: list[str] | tuple[str, ...] | str
                 ) -> list[LoadedPlugin]:
        wanted = {value.casefold() for value in self._selectors(selectors)}
        if 'all' in wanted:
            return list(self.plugins)
        return [plugin for plugin in self.plugins if wanted.intersection({
            plugin.info['id'].casefold(), plugin.info['name'].casefold(),
            plugin.path.name.casefold(), plugin.path.stem.casefold()})]

    def set_active(self, selectors: list[str] | tuple[str, ...] | str,
                   active: bool) -> list[LoadedPlugin]:
        matched = self.matching(selectors)
        for plugin in matched:
            self._state_for(plugin)['enabled'] = bool(active)
        if matched:
            self.save_state()
        return matched

    def install(self, filenames: list[str] | tuple[str, ...] | str) -> list[Path]:
        filenames = [filenames] if isinstance(filenames, str) else filenames
        installed = []
        for value in filenames:
            source = Path(value).resolve()
            if source.suffix.lower() not in SUPPORTED_SUFFIXES:
                raise ValueError(f'Unsupported plugin format: {source.suffix}')
            if not source.is_file():
                raise FileNotFoundError(source)
            target = self.directory / source.name
            if source != target.resolve():
                shutil.copy2(source, target)
            installed.append(target)
        return installed

    def delete(self, plugin: LoadedPlugin) -> bool:
        deleted = True
        try:
            plugin.path.unlink()
        except OSError:
            deleted = False
            pending = self.state.setdefault('pending_delete', [])
            value = str(plugin.path.resolve())
            if value not in pending:
                pending.append(value)
        self.plugins = [item for item in self.plugins if item is not plugin]
        self.state.setdefault('plugins', {}).pop(plugin.info['id'], None)
        self.save_state()
        return deleted

    def execute(self, event: str, context: dict[str, Any] | None = None,
                *, include_inactive: bool = False) -> list[dict[str, Any]]:
        results = []
        for plugin in self.plugins:
            if not include_inactive and not self.is_active(plugin):
                continue
            try:
                plugin_context = dict(context or {})
                plugin_context['settings'] = self.settings_for(plugin)
                result = plugin.execute(event, plugin_context)
                results.append({'plugin': plugin, 'result': result})
            except Exception as exc:
                self._record_error(plugin.path, exc)
        return results


def gui_symbols(results: list[dict[str, Any]]) -> list[dict[str, str]]:
    symbols = []
    for item in results:
        plugin, result = item.get('plugin'), item.get('result')
        if not isinstance(plugin, LoadedPlugin) or not isinstance(result, dict):
            continue
        gui = result.get('gui')
        rows = gui.get('symbols') if isinstance(gui, dict) else None
        if not isinstance(rows, list):
            continue
        for row in rows:
            if isinstance(row, dict) and isinstance(row.get('text'), str):
                symbols.append({
                    'text': row['text'][:16],
                    'foreground': str(row.get('foreground') or '#fbbf24'),
                    'tooltip': str(row.get('tooltip') or plugin.info.get('description') or ''),
                    'plugin_name': str(plugin.info['name']),
                    'plugin_version': str(plugin.info['version']),
                })
    return symbols


__all__ = ['PLUGIN_API_VERSION', 'LoadedPlugin', 'PluginManager',
           'SUPPORTED_SUFFIXES', 'gui_symbols']
