"""Tkinter front end for Universal SCS Extractor.

The GUI deliberately executes the normal command-line entry point in a child
process.  There is consequently only one extraction implementation, and the
command preview is the exact argument vector which will be executed.
"""
from __future__ import annotations

import os
from pathlib import Path
import queue
import codecs
import re
import subprocess
import sys
import threading
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

from plugin_runtime import PluginManager, gui_symbols
from app_settings import load_settings as load_app_settings, save_settings as save_app_settings
from community_paths import (CommunityTransferCancelled, create_storage, fetch_paths, load_config, publish_paths,
                             has_embedded_defaults, read_local_paths, read_pending, save_config,
                             temporary_path_list, valid_webhook, write_pending)



APP_NAME = 'Universal SCS Extractor'
WINDOWS_APP_ID = 'UniversalSCSExtractor.Desktop'
MAX_OUTPUT_CHUNKS_PER_TICK = 1024
MAX_OUTPUT_CHARS_PER_TICK = 128 * 1024
_NATIVE_ICON_HANDLES: list[object] = []

COLORS = {
    'window': '#0b1120', 'sidebar': '#101827', 'surface': '#151f32',
    'surface2': '#1b2940', 'border': '#2a3a55', 'text': '#f3f7ff',
    'muted': '#91a1b8', 'accent': '#22d3ee', 'accent2': '#0ea5e9',
    'danger': '#fb7185', 'console': '#070b13',
}

UI_TEXT = {
    'ru': {
        'copy_output': '\u041a\u043e\u043f\u0438\u0440\u043e\u0432\u0430\u0442\u044c \u0432\u044b\u0432\u043e\u0434',
        'select_all': '\u0412\u044b\u0434\u0435\u043b\u0438\u0442\u044c \u0432\u0441\u0451',
        'copy_selection': '\u041a\u043e\u043f\u0438\u0440\u043e\u0432\u0430\u0442\u044c',
        'text_copied': '\u0422\u0435\u043a\u0441\u0442 \u0441\u043a\u043e\u043f\u0438\u0440\u043e\u0432\u0430\u043d',
        'main': 'Архивы', 'options': 'Параметры', 'settings': 'Настройки',
        'output': 'Журнал', 'help': 'Справка',
        'subtitle': 'Распаковка HashFS v1/v2 и ZIP', 'language': 'Язык',
        'inputs': 'ИСХОДНЫЕ ФАЙЛЫ', 'add_archives': '＋  Добавить архивы',
        'add_folder': '＋  Добавить папку', 'remove': 'Удалить', 'clear': 'Очистить',
        'drop_hint': 'Добавьте один или несколько .scs/.zip архивов',
        'destination': 'ПАПКА НАЗНАЧЕНИЯ', 'browse': 'Обзор…',
        'quick': 'БЫСТРЫЕ НАСТРОЙКИ', 'deep': 'Глубокий поиск путей',
        'separate': 'Отдельная папка для каждого архива', 'mode': 'РЕЖИМ РАБОТЫ',
        'extract': 'Распаковка', 'list': 'Список файлов', 'list_all': 'Все пути',
        'list_entries': 'Таблица записей', 'tree': 'Дерево каталогов',
        'paths_filter': 'ПУТИ И ФИЛЬТРАЦИЯ', 'additional': 'Дополнительный список',
        'reference': 'Архивы-зависимости',
        'plugins': 'Плагины', 'plugin_available': 'Доступные плагины',
        'plugin_active': 'Активен', 'plugin_inactive': 'Отключён',
        'plugin_activate': 'Активировать', 'plugin_deactivate': 'Деактивировать',
        'plugin_load': 'Загрузить…', 'plugin_delete': 'Удалить',
        'plugin_details': 'Информация и настройки',
        'plugin_status': 'Статус', 'plugin_type': 'Тип', 'plugin_version': 'Версия',
        'plugin_author': 'Автор', 'plugin_path': 'Файл', 'plugin_save': 'Сохранить',
        'plugin_delete_question': 'Удалить плагин {name}?',
        'plugin_delete_error': 'Не удалось удалить плагин:\n{error}',
        'plugin_delete_pending': 'Плагин будет удалён при следующем запуске.',
        'archive_tools': 'ИНСТРУМЕНТЫ АРХИВА',
        'edit_manifest': 'Изменить manifest.sii',
        'manifest_replace': 'Замены в манифесте',
        'manifest_replace_from': 'Заменить', 'manifest_replace_to': 'На',
        'manifest_replace_add': '+  Добавить замену',
        'manifest_replace_empty': 'Нажмите «+ Добавить замену», чтобы создать пару.',
        'compress_v1': 'Сжать и уплотнить HashFS v1',
        'compress_no_compact': 'Не уплотнять архив (-jc)',
        'path_file': 'Начальные пути из файла', 'partial': 'Начальные пути',
        'filter': 'Фильтр файлов', 'list_output': 'Файл списка', 'logging': 'ЖУРНАЛ',
        'enable_log': 'Вести журнал', 'switches': 'ДОПОЛНИТЕЛЬНЫЕ ПАРАМЕТРЫ',
        'all': 'Сканировать *.scs в папках', 'dry_run': 'Проверка без записи',
        'quiet': 'Скрыть строки отдельных файлов', 'skip_existing': 'Не перезаписывать',
        'raw': 'Сырые записи по хешам', 'no_update': 'Не обновлять ссылки',
        'force_extract': 'Распаковывать вместе со списком',
        'table_at_end': 'Таблица HashFS v1 в конце архива', 'salt': 'Salt HashFS',
        'console_hint': 'События текущей операции', 'clear_output': 'Очистить',
        'command': 'КОМАНДА ЗАПУСКА', 'copy': 'Копировать', 'open_result': 'Открыть папку',
        'stop': 'Остановить', 'run': 'Запустить', 'ready': 'Готово',
        'run_new_tab': 'Запустить в новой вкладке журнала', 'log_tab': 'Журнал {number}',
        'close_tab': 'Закрыть вкладку', 'tab_running': '● Выполняется',
        'help_title': 'Справочник параметров',
        'help_intro': 'Выберите параметр слева. Поиск работает по имени и описанию.',
        'search': 'Поиск параметра…', 'syntax': 'Синтаксис', 'description': 'Описание',
        'example': 'Пример', 'running': 'Выполняется…', 'stopping': 'Остановка…',
        'success': 'Завершено успешно', 'failed': 'Завершено с кодом {code}',
        'copied': 'Команда скопирована',
        'choose_archives': 'Выберите архивы', 'archive_files': 'Архивы SCS и ZIP',
        'all_files': 'Все файлы', 'choose_folder': 'Выберите папку с архивами',
        'choose_destination': 'Выберите папку назначения', 'choose_file': 'Выберите файл',
        'text_files': 'Текстовые файлы',
        'need_input': 'Добавьте хотя бы один архив или папку.',
        'paths_conflict': 'Параметры --paths и --partial задают один набор начальных путей. Оставьте только один.',
        'bad_salt': 'Salt должен быть числом от 0 до 65535.',
        'launch_error': 'Не удалось запустить экстрактор:\n{error}',
        'stop_question': 'Остановить текущую операцию?',
        'missing_destination': 'Папка ещё не существует:\n{path}',
        'open_error': 'Не удалось открыть папку:\n{error}',
        'close_question': 'Операция ещё выполняется. Остановить её и выйти?',
        'read_error': 'Ошибка чтения вывода: {error}',
    },
    'en': {
        'main': 'Archives', 'options': 'Options', 'settings': 'Settings',
        'output': 'Activity', 'help': 'Help',
        'subtitle': 'HashFS v1/v2 and ZIP extraction', 'language': 'Language',
        'inputs': 'INPUT FILES', 'add_archives': '＋  Add archives',
        'add_folder': '＋  Add folder', 'remove': 'Remove', 'clear': 'Clear',
        'drop_hint': 'Add one or more .scs/.zip archives',
        'destination': 'DESTINATION', 'browse': 'Browse…',
        'quick': 'QUICK OPTIONS', 'deep': 'Deep path discovery',
        'separate': 'Separate directory per archive', 'mode': 'OPERATION MODE',
        'extract': 'Extract', 'list': 'File list', 'list_all': 'All paths',
        'list_entries': 'Entry table', 'tree': 'Directory tree',
        'paths_filter': 'PATHS AND FILTERING', 'additional': 'Additional path list',
        'reference': 'Reference archives',
        'plugins': 'Plugins', 'plugin_available': 'Available plugins',
        'plugin_active': 'Active', 'plugin_inactive': 'Disabled',
        'plugin_activate': 'Activate', 'plugin_deactivate': 'Deactivate',
        'plugin_load': 'Load…', 'plugin_delete': 'Delete',
        'plugin_details': 'Information and settings', 'plugin_status': 'Status',
        'plugin_type': 'Type', 'plugin_version': 'Version', 'plugin_author': 'Author',
        'plugin_path': 'File', 'plugin_save': 'Save',
        'plugin_delete_question': 'Delete plugin {name}?',
        'plugin_delete_error': 'Could not delete the plugin:\n{error}',
        'plugin_delete_pending': 'The plugin will be deleted on the next launch.',
        'archive_tools': 'ARCHIVE TOOLS',
        'edit_manifest': 'Edit manifest.sii',
        'manifest_replace': 'Manifest replacements',
        'manifest_replace_from': 'Replace', 'manifest_replace_to': 'With',
        'manifest_replace_add': '+  Add replacement',
        'manifest_replace_empty': 'Click “+ Add replacement” to create a pair.',
        'compress_v1': 'Compress and compact HashFS v1',
        'compress_no_compact': 'Do not compact the archive (-jc)',
        'path_file': 'Start paths from file', 'partial': 'Start paths',
        'filter': 'File filter', 'list_output': 'Path-list output', 'logging': 'LOGGING',
        'enable_log': 'Write log', 'switches': 'ADVANCED OPTIONS',
        'all': 'Scan *.scs in folders', 'dry_run': 'Validate without writing',
        'quiet': 'Hide individual file lines', 'skip_existing': 'Do not overwrite',
        'raw': 'Raw entries by hash', 'no_update': 'Do not update references',
        'force_extract': 'Extract together with listing',
        'table_at_end': 'HashFS v1 table is at archive end', 'salt': 'HashFS salt',
        'console_hint': 'Events from the current operation', 'clear_output': 'Clear',
        'copy_output': 'Copy output', 'select_all': 'Select all',
        'copy_selection': 'Copy', 'text_copied': 'Text copied',
        'command': 'COMMAND PREVIEW', 'copy': 'Copy', 'open_result': 'Open folder',
        'stop': 'Stop', 'run': 'Run', 'ready': 'Ready',
        'run_new_tab': 'Run in new activity tab', 'log_tab': 'Activity {number}',
        'close_tab': 'Close tab', 'tab_running': '● Running',
        'help_title': 'Command-line option reference',
        'help_intro': 'Select an option on the left. Search matches names and descriptions.',
        'search': 'Search options…', 'syntax': 'Syntax', 'description': 'Description',
        'example': 'Example', 'running': 'Running…', 'stopping': 'Stopping…',
        'success': 'Completed successfully', 'failed': 'Exited with code {code}',
        'copied': 'Command copied',
        'choose_archives': 'Choose archives', 'archive_files': 'SCS and ZIP archives',
        'all_files': 'All files', 'choose_folder': 'Choose an archive folder',
        'choose_destination': 'Choose destination folder', 'choose_file': 'Choose a file',
        'text_files': 'Text files',
        'need_input': 'Add at least one archive or folder.',
        'paths_conflict': '--paths and --partial define the same start-path set. Keep only one.',
        'bad_salt': 'Salt must be a number from 0 to 65535.',
        'launch_error': 'Could not start the extractor:\n{error}',
        'stop_question': 'Stop the current operation?',
        'missing_destination': 'The destination does not exist yet:\n{path}',
        'open_error': 'Could not open the folder:\n{error}',
        'close_question': 'An operation is still running. Stop it and exit?',
        'read_error': 'Could not read process output: {error}',
    },
}

# Kept separately so these recent strings cannot inherit legacy source-file
# encoding damage from the original Russian UI dictionary.
UI_TEXT['ru'].update({
    'community': '\u041e\u0411\u0429\u0418\u0415 \u041f\u0423\u0422\u0418',
    'community_share': '\u0421\u043e\u0433\u043b\u0430\u0448\u0430\u044e\u0441\u044c \u043e\u0442\u043f\u0440\u0430\u0432\u043b\u044f\u0442\u044c \u043f\u0443\u0442\u0438 \u0440\u0430\u0441\u043f\u0430\u043a\u043e\u0432\u0430\u043d\u043d\u044b\u0445 \u0444\u0430\u0439\u043b\u043e\u0432 \u0434\u043b\u044f \u043f\u0443\u0431\u043b\u0438\u0447\u043d\u043e\u0433\u043e \u0438\u0441\u043f\u043e\u043b\u044c\u0437\u043e\u0432\u0430\u043d\u0438\u044f',
    'community_use': '\u0418\u0441\u043f\u043e\u043b\u044c\u0437\u043e\u0432\u0430\u0442\u044c \u043f\u0443\u0431\u043b\u0438\u0447\u043d\u043e \u043e\u043f\u0443\u0431\u043b\u0438\u043a\u043e\u0432\u0430\u043d\u043d\u044b\u0435 \u043f\u0443\u0442\u0438 \u0434\u043b\u044f \u0443\u043f\u0440\u043e\u0449\u0435\u043d\u0438\u044f \u0440\u0430\u0441\u043f\u0430\u043a\u043e\u0432\u043a\u0438',
    'community_setup': '\u041d\u0430\u0441\u0442\u0440\u043e\u0438\u0442\u044c webhook\u2026',
    'performance': 'ПРОИЗВОДИТЕЛЬНОСТЬ',
    'max_threads': 'Максимальное количество потоков',
    'defaults': 'ПАРАМЕТРЫ ПО УМОЛЧАНИЮ',
    'always_deep': 'Всегда использовать --deep',
    'always_log': 'Всегда вести журнал',
    'default_log_path': 'Путь к файлу журнала',
    'save_settings': 'Сохранить настройки',
    'settings_saved': 'Настройки сохранены',
})
UI_TEXT['en'].update({
    'community': 'COMMUNITY PATHS',
    'community_share': 'I agree to send recovered paths for public use',
    'community_use': 'Use publicly published paths to simplify extraction',
    'community_setup': 'Configure webhook…',
    'performance': 'PERFORMANCE',
    'max_threads': 'Maximum thread count',
    'defaults': 'DEFAULT OPTIONS',
    'always_deep': 'Always use --deep',
    'always_log': 'Always write a log',
    'default_log_path': 'Log file path',
    'save_settings': 'Save settings',
    'settings_saved': 'Settings saved',
})

# option, Russian description, English description, example
HELP_OPTIONS = (
    ('path...', 'Один или несколько архивов. Вместе с --all здесь указываются папки.',
     'One or more archives. With --all, specify directories here.', 'map.scs models.scs'),
    ('-a, --all', 'Считать входные пути папками и обработать все .scs непосредственно в них.',
     'Treat input paths as directories and process every .scs directly inside them.', 'mods --all'),
    ('-D, --deep', 'Искать неизвестные HashFS-пути по содержимому, ссылкам и базе путей. Ненайденное попадёт в _unknown.',
     'Recover unknown HashFS paths from contents, references and the path database. Unresolved files go to _unknown.', 'mod.scs --deep'),
    ('-d, --dest DIR', 'Папка распаковки. Явное указание также принудительно включает распаковку.',
     'Extraction directory. Specifying it explicitly also forces extraction.', 'mod.scs --dest=unpacked'),
    ('--additional FILE', 'Дополнительный текстовый список кандидатов путей для глубокого поиска.',
     'Additional newline-separated candidate paths for deep discovery.', 'mod.scs --deep --additional=paths.txt'),
    ('-ref, --reference ARCHIVE...', 'Архивы-зависимости для поиска путей. Их таблицы и файлы со ссылками анализируются, но сами архивы не распаковываются. Включает --deep.',
     'Dependency archives used only for path discovery. Their tables and path-bearing files are scanned, but they are not extracted. Implies --deep.',
     'addon.scs --reference base.scs def.scs'),
    ('--dry-run', 'Полностью проверить чтение и поиск путей, но не записывать файлы.',
     'Perform parsing and path discovery without writing files.', 'mod.scs --deep --dry-run'),
    ('-f, --filter PATTERNS', 'Оставить пути по маскам через запятую или регулярному выражению r/.../.',
     'Keep paths matching comma-separated wildcards or an r/.../ regular expression.', 'mod.scs --filter="*.sii,/def/*"'),
    ('--list', 'Вывести известные пути архива вместо обычной распаковки.',
     'Print known archive paths instead of normal extraction.', 'mod.scs --list'),
    ('--list-all', 'Вывести найденные и упомянутые пути; для HashFS включает глубокий поиск.',
     'Print contained and referenced paths; enables deep discovery for HashFS.', 'mod.scs --list-all'),
    ('--list-output FILE', 'Сохранить список в файл, дополняя существующий без дубликатов. Требует --list или --list-all.',
     'Save the listing, extending an existing file without duplicates. Requires --list or --list-all.', '--list-all --list-output=paths.txt'),
    ('--list-entries', 'Показать сырые хеши, смещения, размеры и типы записей HashFS.',
     'Print raw HashFS hashes, offsets, sizes and entry types.', 'mod.scs --list-entries'),
    ('--log [FILE]', 'Записывать подробный журнал. Без имени используется _extraction.log.',
     'Write a detailed log. With no filename, uses _extraction.log.', 'mod.scs --log=extract.log'),
    ('--no-update', 'Не исправлять ссылки внутри файлов после замены недопустимых символов пути.',
     'Do not repair references after invalid path characters cause renaming.', 'mod.scs --no-update'),
    ('--force-extract', 'Выполнить распаковку даже вместе с режимом списка или дерева.',
     'Extract even when a listing or tree mode is selected.', 'mod.scs --list-all --force-extract'),
    ('--plugin-load FILE', 'Установить PYD- или DLL-плагин рядом с приложением.',
     'Install a PYD or DLL plugin beside the application.', '--plugin-load=MyPlugin.pyd'),
    ('--plugin-disable PLUGIN', 'Деактивировать плагин. ALL отключает все плагины.',
     'Deactivate a plugin. ALL deactivates every plugin.', '--plugin-disable=ALL'),
    ('--plugin-activate PLUGIN', 'Активировать плагин. ALL включает все плагины.',
     'Activate a plugin. ALL activates every plugin.', '--plugin-activate=my-plugin'),
    ('--plugin-list', 'Вывести активные и доступные для активации плагины.',
     'List active plugins and plugins available for activation.', '--plugin-list'),
    ('--manifest', 'Удалить все compatible_versions[] из manifest.sii архива HashFS v1/v2, ZIP или AEM. Изменение выполняется через безопасную атомарную замену архива.',
     'Remove every compatible_versions[] entry from a HashFS v1/v2, ZIP or AEM manifest.sii. The archive is updated and replaced atomically.', 'mod.scs --manifest'),
    ('--manifest-replace OLD=NEW', 'После удаления compatible_versions[] заменить текст в manifest.sii HashFS, ZIP или AEM. Параметр можно повторять; несколько замен разделяются точкой с запятой.',
     'Replace text in a HashFS, ZIP or AEM manifest.sii after removing compatible_versions[]. Repeat the option or separate replacements with semicolons.', 'mod.scs --manifest-replace="Old name=New name"'),
    ('--edit', 'Интерактивно вводить замены для manifest.sii в консоли в формате "старый текст" "новый текст".',
     'Prompt for manifest.sii replacements interactively using the "old text" "new text" format.', 'mod.scs --manifest --edit'),
    ('--compress', 'Сжать zlib все несжатые файловые записи HashFS v1, уплотнить данные и безопасно заменить исходный архив.',
     'Compress every uncompressed HashFS v1 file entry with zlib, compact the data and safely replace the source archive.', 'mod.scs --compress'),
    ('-jc, --jc', 'Сжать данные HashFS v1, но оставить неиспользуемые области архива без уплотнения.',
     'Compress HashFS v1 entry data but retain unused archive space instead of compacting it.', 'mod.scs --compress --jc'),
    ('-p, --partial PATHS', 'Распаковать только перечисленные через запятую файлы или каталоги.',
     'Extract only comma-separated files or directories.', 'mod.scs --partial=/def,/map'),
    ('-P, --paths FILE', 'Как --partial, но начальные пути читаются по одному из файла.',
     'Like --partial, but reads one start path per line from a file.', 'mod.scs --paths=start.txt'),
    ('-q, --quiet', 'Не печатать строку для каждого файла; итоговая статистика остаётся.',
     'Hide per-file lines while keeping final summaries.', 'mod.scs --quiet'),
    ('-r, --raw', 'Извлечь HashFS-записи под шестнадцатеричными хешами без обхода дерева.',
     'Dump HashFS entries under hexadecimal hashes without tree traversal.', 'mod.scs --raw'),
    ('--salt NUMBER', 'Принудительно использовать salt хеширования HashFS от 0 до 65535.',
     'Override the HashFS path-hashing salt with a value from 0 to 65535.', 'mod.scs --salt=42'),
    ('-S, --separate', 'Для нескольких архивов создать отдельную выходную папку каждому.',
     'Create a separate output directory for every archive.', 'a.scs b.scs --separate'),
    ('-s, --skip-existing', 'Не заменять файлы, которые уже существуют в папке назначения.',
     'Do not overwrite files already present in the destination.', 'mod.scs --skip-existing'),
    ('--table-at-end', 'Для HashFS v1 читать таблицу записей с конца, игнорируя смещение заголовка.',
     'For HashFS v1, read the entry table from the end and ignore the header offset.', 'mod.scs --table-at-end'),
    ('--tree', 'Показать дерево каталогов архива вместо распаковки.',
     'Print the archive directory tree instead of extracting.', 'mod.scs --tree'),
    ('-?, -h, --help', 'Показать полную консольную справку и завершить работу.',
     'Print the complete console usage and exit.', r'.\UniversalSCSExtractor.exe --help'),
)


def _hide_standalone_console() -> None:
    """Hide only a console allocated for this GUI process by Explorer.

    If the executable was started from PowerShell/cmd, its console is shared
    with the parent and must not be hidden.
    """
    if sys.platform != 'win32':
        return
    try:
        import ctypes
        process_ids = (ctypes.c_uint * 2)()
        count = ctypes.windll.kernel32.GetConsoleProcessList(process_ids, 2)
        if count <= 1:
            window = ctypes.windll.kernel32.GetConsoleWindow()
            if window:
                ctypes.windll.user32.ShowWindow(window, 0)
    except (AttributeError, OSError):
        pass


def _runtime_command(arguments: list[str]) -> list[str]:
    if getattr(sys, 'frozen', False):
        return [str(sys.executable), *arguments]
    return [sys.executable, str(Path(__file__).with_name('extractor.py')), *arguments]


def _application_directory() -> Path:
    """Return the directory used as the GUI command working directory."""
    if getattr(sys, 'frozen', False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent.parent


def _display_input_path(value: str | os.PathLike[str]) -> str:
    """Show local inputs using Windows dot-backslash relative paths."""
    path = Path(value)
    try:
        relative = path.resolve().relative_to(_application_directory())
    except (OSError, ValueError):
        return str(path)
    if relative == Path('.'):
        return '.\\'
    return '.\\' + str(relative).replace('/', '\\')


def translate_activity_message(message: str, language: str) -> str:
    """Translate known extractor events while preserving paths and names."""
    if language != 'ru':
        return message
    lowered = message.lower()
    if not any(word in lowered for word in (
            'running', 'searching', 'scanning', 'extracting', 'extracted', 'reading',
            'updating manifest', 'compressing', 'manifest updated', 'entries compressed',
            'validated', 'dumping', 'dumped', 'skipping', 'skipped',
            'warn:', 'totally opened', 'opened ', 'archives found',
            'cancelled', 'failed', 'junk', 'top level directory is missing',
            'updated path list', 'created path list')):
        return message
    replacements = (
        (r'^Running\.{0,3}\s+', 'Выполняется… '),
        (r'^Searching for paths \.\.\.$', 'Поиск путей…'),
        (r'^Top level directory is missing; use --deep to scan contents for paths '
         r'before extraction or --partial to extract known paths$',
         'Корневая директория отсутствует; используйте --deep для поиска путей '
         'по содержимому или --partial для распаковки известных путей'),
        (r'^Scanning reference archives: (.+)$', r'Сканирование reference-архивов: \1'),
        (r'^Extracting (.+) \.\.\.$', r'Распаковка \1…'),
        (r'^Reading (.+) \.\.\.$', r'Чтение \1…'),
        (r'^Updating manifest in (.+) \.\.\.$', r'Изменение манифеста в \1…'),
        (r'^Compressing (.+) \.\.\.$', r'Сжатие \1…'),
        (r'^SUCCESS: (.+): manifest updated \((\d+) compatible versions removed, (\d+) replacements\)$',
         r'УСПЕШНО: \1: манифест изменён (удалено версий: \2, замен: \3)'),
        (r'^SUCCESS: (.+): (\d+) entries compressed, ([\d.]+ KB) -> ([\d.]+ KB) \((-?[\d.]+ KB) saved\) without compaction$',
         r'УСПЕШНО: \1: сжато записей: \2, \3 -> \4 (сэкономлено: \5), без уплотнения'),
        (r'^SUCCESS: (.+): (\d+) entries compressed, ([\d.]+ KB) -> ([\d.]+ KB) \((-?[\d.]+ KB) saved\)$',
         r'УСПЕШНО: \1: сжато записей: \2, \3 -> \4 (сэкономлено: \5)'),
        (r'^Extracted (.+)$', r'Распакован \1'),
        (r'^Validated (.+)$', r'Проверен \1'),
        (r'^Dumping (.+) \.\.\.$', r'Создание дампа \1…'),
        (r'^Dumped (.+)$', r'Создан дамп \1'),
        (r'^Skipping (.+) \.\.\.$', r'Пропуск \1…'),
        (r'^Skipped (.+)$', r'Пропущен \1'),
        (r'^Updated path list "(.+)" \((\d+) added, (\d+) total\)$',
         r'Обновлён список путей "\1" (добавлено: \2, всего: \3)'),
        (r'^Created path list "(.+)" \((\d+) paths\)$',
         r'Создан список путей "\1" (всего: \2)'),
        (r'^(?:INFO\s+)?WARN:\s*(\d+) malformed or suspicious entries were classified as junk',
         r'ПРЕДУПРЕЖДЕНИЕ: \1 повреждённых или подозрительных записей отнесены к мусору'),
        (r'^(?:INFO\s+)?WARN:\s*(\d+) entries with unknown chunk type have been skipped; some files may be missing$',
         r'ПРЕДУПРЕЖДЕНИЕ: \1 записей с неизвестным типом блока пропущено; некоторые файлы могут отсутствовать'),
        (r'^Totally opened (\d+) entries \((\d+) files, (\d+) directories found\)$',
         r'Всего открыто \1 записей (\2 файлов, найдено каталогов: \3)'),
        (r'^Opened (.+): (.+) archive; (\d+) entries \((\d+) files, (\d+) directory listings, (\d+) rejected\)$',
         r'Открыт \1: архив \2; \3 записей (\4 файлов, \5 списков каталогов, отклонено: \6)'),
        (r'^Opened (.+): (.+) archive; (\d+) entries \((\d+) files, (\d+) directory listings\)$',
         r'Открыт \1: архив \2; \3 записей (\4 файлов, \5 списков каталогов)'),
        (r'^Opened (.+): (.+) archive; (\d+) entries$',
         r'Открыт \1: архив \2; записей: \3'),
        (r'^No \.scs archives found$', 'Архивы .scs не найдены'),
        (r'^Cancelled\.$', 'Отменено.'),
    )
    for pattern, replacement in replacements:
        translated, count = re.subn(pattern, replacement, message,
                                     flags=re.IGNORECASE)
        if count:
            return translated
    translated = re.sub(r'^Total\s+(\d+)\s+extracted\b', r'Всего \1 распаковано', message,
                        flags=re.IGNORECASE)
    translated = re.sub(r'(\d+)\s+extracted\b', r'\1 распаковано', translated,
                        flags=re.IGNORECASE)
    translated = re.sub(r'(\d+)\s+skipped\b', r'\1 пропущено', translated,
                        flags=re.IGNORECASE)
    translated = re.sub(r'(\d+)\s+failed\b', r'\1 ошибок', translated,
                        flags=re.IGNORECASE)
    translated = re.sub(r'(\d+)\s+dumped\b', r'\1 дампов', translated,
                        flags=re.IGNORECASE)
    translated = re.sub(r'(\d+)\s+junk\b', r'\1 мусора', translated,
                        flags=re.IGNORECASE)
    translated = re.sub(r'(\d+)\s+renamed\b', r'\1 переименовано', translated,
                        flags=re.IGNORECASE)
    translated = re.sub(r'(\d+)\s+modified\b', r'\1 изменено', translated,
                        flags=re.IGNORECASE)
    translated = re.sub(r'(\d+)\s+not found\b', r'\1 не найдено', translated,
                        flags=re.IGNORECASE)
    return translated


def _set_windows_app_identity() -> None:
    """Give Explorer and the taskbar a stable identity for the bundled icon."""
    if sys.platform != 'win32':
        return
    try:
        import ctypes
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(WINDOWS_APP_ID)
    except (AttributeError, OSError):
        pass


def _apply_windows_window_icon(root: tk.Tk, icon_path: Path) -> None:
    """Assign the native EXE/ICO handles to both Tk and its Windows wrapper.

    Tk's ``iconphoto`` creates its own icon handles and can make Windows fall
    back to the Python/Tk icon.  WM_SETICON avoids that conversion and uses
    the exact icon embedded in the executable.
    """
    if sys.platform != 'win32':
        return
    try:
        import ctypes
        from ctypes import wintypes

        user32 = ctypes.windll.user32
        shell32 = ctypes.windll.shell32
        source = str(sys.executable if getattr(sys, 'frozen', False) else icon_path)
        large = wintypes.HICON()
        small = wintypes.HICON()
        shell32.ExtractIconExW.argtypes = (
            wintypes.LPCWSTR, ctypes.c_int, ctypes.POINTER(wintypes.HICON),
            ctypes.POINTER(wintypes.HICON), wintypes.UINT)
        shell32.ExtractIconExW.restype = wintypes.UINT
        if not shell32.ExtractIconExW(source, 0, ctypes.byref(large),
                                      ctypes.byref(small), 1):
            return

        root.update_idletasks()
        client = wintypes.HWND(root.winfo_id())
        user32.GetParent.argtypes = (wintypes.HWND,)
        user32.GetParent.restype = wintypes.HWND
        wrapper = user32.GetParent(client)
        user32.SendMessageW.argtypes = (
            wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM)
        user32.SendMessageW.restype = ctypes.c_ssize_t
        for window in (client, wrapper):
            if window:
                if small:
                    user32.SendMessageW(window, 0x0080, 0, small.value)  # ICON_SMALL
                if large:
                    user32.SendMessageW(window, 0x0080, 1, large.value)  # ICON_BIG
        # HICON handles must stay valid for the lifetime of the window.
        _NATIVE_ICON_HANDLES.extend((large, small))
    except (AttributeError, OSError, tk.TclError):
        pass


class ExtractorGui:
    def __init__(self, root: tk.Tk, initial_paths: list[str]):
        self.root = root
        self.process: subprocess.Popen | None = None
        self._processes: dict[int, subprocess.Popen] = {}
        # Queue messages must be tied to an immutable operation, not merely
        # to a reusable journal-tab index.  A reader thread can deliver its
        # last chunk after another operation has already claimed that tab.
        self._operation_by_tab: dict[int, int] = {}
        self._cancelled_operations: set[int] = set()
        self._next_operation_id = 1
        self.output_queue: queue.Queue[tuple[str, object]] = queue.Queue()
        self._activity_tabs: list[dict[str, object]] = [
            {'records': [], 'fragment': '', 'follow_tail': True,
             'updating_view': False, 'closed': False,
             'announce_new_process': False, 'groups': [],
             'active_group': None, 'next_group_id': 0,
             'status_mark_created': False, 'aggregate': {},
             'total_rendered': False, 'operation_id': None,
             'render_operation_id': 0, 'show_extraction_total': False}]
        self._active_activity_index = 0
        self._closing = False
        self._status_notice_after: str | None = None

        root.title(APP_NAME)
        root.geometry('1080x790')
        root.minsize(880, 650)
        root.protocol('WM_DELETE_WINDOW', self.close)
        self._configure_style()

        self.mode = tk.StringVar(value='extract')
        self.destination = tk.StringVar(value='extracted')
        self.additional = tk.StringVar()
        self.reference = tk.StringVar()
        self.path_file = tk.StringVar()
        self.partial = tk.StringVar()
        self.filter_text = tk.StringVar()
        self.list_output = tk.StringVar()
        self.log_enabled = tk.BooleanVar()
        self.log_file = tk.StringVar(value='_extraction.log')
        self.salt = tk.StringVar()
        self.scan_folders = tk.BooleanVar()
        self.deep = tk.BooleanVar()
        self.dry_run = tk.BooleanVar()
        self.no_update = tk.BooleanVar()
        self.force_extract = tk.BooleanVar()
        self.quiet = tk.BooleanVar()
        self.raw = tk.BooleanVar()
        self.separate = tk.BooleanVar()
        self.skip_existing = tk.BooleanVar()
        self.table_at_end = tk.BooleanVar()
        self.edit_manifest = tk.BooleanVar()
        # Manifest replacements are kept as independent pairs.  Besides being
        # easier to edit in the GUI, emitting one command-line option per pair
        # avoids the ambiguous semicolon-separated presentation used before.
        self.manifest_replacements: list[tuple[tk.StringVar, tk.StringVar]] = []
        self.compress_v1 = tk.BooleanVar()
        self.compress_no_compact = tk.BooleanVar()
        self._app_settings = getattr(self, '_app_settings', load_app_settings())
        self.max_threads = tk.StringVar(
            value=str(self._app_settings.get('max_threads', 1)))
        self.always_deep = tk.BooleanVar(
            value=bool(self._app_settings.get('always_deep', False)))
        self.always_log = tk.BooleanVar(
            value=bool(self._app_settings.get('always_log', False)))
        self.log_file.set(str(
            self._app_settings.get('log_path', '_extraction.log')))
        if self.always_deep.get():
            self.deep.set(True)
        if self.always_log.get():
            self.log_enabled.set(True)
        self._community_config = load_config()
        self.community_share = tk.BooleanVar(value=bool(self._community_config.get('share', False)))
        self.community_use = tk.BooleanVar(value=bool(self._community_config.get('use', False)))
        self._community_runs: dict[int, dict[str, object]] = {}
        self.status = tk.StringVar(value='Готово')
        self._status_base = self.status.get()
        self.command_preview = tk.StringVar()

        self._build_ui()
        self.root.bind('<KeyPress-End>', self._jump_active_output_to_end, add='+')
        self._append_paths(initial_paths)
        self._install_traces()
        self.refresh_command()
        self.root.after(75, self._drain_output)
        self.root.after(350, self._ask_pending_community_paths)

    def t(self, key: str) -> str:
        return UI_TEXT['ru'].get(key, key)

    def _configure_style(self) -> None:
        style = ttk.Style(self.root)
        for theme in ('vista', 'clam'):
            if theme in style.theme_names():
                try:
                    style.theme_use(theme)
                    break
                except tk.TclError:
                    pass
        style.configure('Header.TLabel', font=('Segoe UI', 15, 'bold'))
        style.configure('Muted.TLabel', foreground='#555555')
        style.configure('Run.TButton', font=('Segoe UI', 10, 'bold'))

    def _build_ui(self) -> None:
        outer = ttk.Frame(self.root, padding=12)
        outer.pack(fill=tk.BOTH, expand=True)

        header = ttk.Frame(outer)
        header.pack(fill=tk.X, pady=(0, 8))
        ttk.Label(header, text=APP_NAME, style='Header.TLabel').pack(side=tk.LEFT)
        ttk.Label(header, text='HashFS v1/v2 и ZIP', style='Muted.TLabel').pack(
            side=tk.LEFT, padx=12, pady=(5, 0))

        self.notebook = ttk.Notebook(outer)
        self.notebook.pack(fill=tk.BOTH, expand=True)
        main_tab = ttk.Frame(self.notebook, padding=10)
        options_tab = ttk.Frame(self.notebook, padding=10)
        output_tab = ttk.Frame(self.notebook, padding=8)
        self.notebook.add(main_tab, text='Основное')
        self.notebook.add(options_tab, text='Все параметры')
        self.notebook.add(output_tab, text='Вывод')
        self._build_main_tab(main_tab)
        self._build_options_tab(options_tab)
        self._build_output_tab(output_tab)

        preview = ttk.LabelFrame(outer, text='Команда запуска', padding=8)
        preview.pack(fill=tk.X, pady=(9, 7))
        preview_entry = ttk.Entry(preview, textvariable=self.command_preview,
                                  state='readonly')
        preview_entry.pack(side=tk.LEFT, fill=tk.X, expand=True)
        ttk.Button(preview, text='Копировать', command=self.copy_command).pack(
            side=tk.LEFT, padx=(8, 0))

        footer = ttk.Frame(outer)
        footer.pack(fill=tk.X)
        ttk.Label(footer, textvariable=self.status).pack(side=tk.LEFT)
        ttk.Button(footer, text='Открыть результат',
                   command=self.open_destination).pack(side=tk.RIGHT)
        self.stop_button = ttk.Button(footer, text='Остановить', command=self.stop,
                                      state=tk.DISABLED)
        self.stop_button.pack(side=tk.RIGHT, padx=7)
        self.run_button = ttk.Button(footer, text='Запустить', style='Run.TButton',
                                     command=self.run)
        self.run_button.pack(side=tk.RIGHT)

    def _build_main_tab(self, parent: ttk.Frame) -> None:
        archives = ttk.LabelFrame(parent, text='Файлы и папки', padding=8)
        archives.pack(fill=tk.BOTH, expand=True)
        list_frame = ttk.Frame(archives)
        list_frame.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        self.paths = tk.Listbox(list_frame, selectmode=tk.EXTENDED, height=9,
                                activestyle='dotbox')
        scrollbar = ttk.Scrollbar(list_frame, command=self.paths.yview)
        self.paths.configure(yscrollcommand=scrollbar.set)
        self.paths.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        scrollbar.pack(side=tk.RIGHT, fill=tk.Y)
        self.paths.bind('<<ListboxSelect>>', lambda _event: self.refresh_command())
        buttons = ttk.Frame(archives)
        buttons.pack(side=tk.LEFT, fill=tk.Y, padx=(8, 0))
        ttk.Button(buttons, text='Добавить архивы…', command=self.add_archives).pack(
            fill=tk.X, pady=(0, 5))
        ttk.Button(buttons, text='Добавить папку…', command=self.add_folder).pack(
            fill=tk.X, pady=5)
        ttk.Button(buttons, text='Удалить выбранное', command=self.remove_paths).pack(
            fill=tk.X, pady=5)
        ttk.Button(buttons, text='Очистить', command=self.clear_paths).pack(
            fill=tk.X, pady=5)

        destination = ttk.LabelFrame(parent, text='Распаковка', padding=8)
        destination.pack(fill=tk.X, pady=(9, 0))
        ttk.Label(destination, text='Папка назначения:').grid(row=0, column=0,
                                                               sticky=tk.W)
        ttk.Entry(destination, textvariable=self.destination).grid(
            row=0, column=1, sticky=tk.EW, padx=7)
        ttk.Button(destination, text='Обзор…', command=self.choose_destination).grid(
            row=0, column=2)
        destination.columnconfigure(1, weight=1)
        flags = ttk.Frame(destination)
        flags.grid(row=1, column=0, columnspan=3, sticky=tk.EW, pady=(8, 0))
        ttk.Checkbutton(flags, text='Глубокий поиск путей (--deep)',
                        variable=self.deep).pack(side=tk.LEFT)
        ttk.Checkbutton(flags, text='Отдельная папка для каждого архива (--separate)',
                        variable=self.separate).pack(side=tk.LEFT, padx=18)

        mode_box = ttk.LabelFrame(parent, text='Режим работы', padding=8)
        mode_box.pack(fill=tk.X, pady=(9, 0))
        modes = (
            ('Распаковать', 'extract'), ('Список файлов', 'list'),
            ('Все найденные пути', 'list_all'), ('Таблица записей', 'list_entries'),
            ('Дерево каталогов', 'tree'),
        )
        for index, (label, value) in enumerate(modes):
            ttk.Radiobutton(mode_box, text=label, value=value,
                            variable=self.mode).grid(row=0, column=index,
                                                     sticky=tk.W, padx=(0, 14))

    def _field(self, parent, row: int, label: str, variable: tk.StringVar,
               browse=None, hint: str | None = None) -> ttk.Entry:
        ttk.Label(parent, text=label).grid(row=row, column=0, sticky=tk.W,
                                           padx=(0, 8), pady=4)
        entry = ttk.Entry(parent, textvariable=variable)
        entry.grid(row=row, column=1, sticky=tk.EW, pady=4)
        if browse:
            ttk.Button(parent, text='Обзор…', command=browse).grid(
                row=row, column=2, padx=(7, 0), pady=4)
        if hint:
            ttk.Label(parent, text=hint, style='Muted.TLabel').grid(
                row=row + 1, column=1, columnspan=2, sticky=tk.W)
        return entry

    def _build_options_tab(self, parent: ttk.Frame) -> None:
        files = ttk.LabelFrame(parent, text='Пути и отбор файлов', padding=8)
        files.pack(fill=tk.X)
        files.columnconfigure(1, weight=1)
        self._field(files, 0, 'Дополнительный список (--additional):',
                    self.additional, lambda: self.choose_file(self.additional))
        self._field(files, 1, 'Архивы-зависимости (-ref, --reference):',
                    self.reference, self.choose_reference_files)
        self._field(files, 2, 'Начальные пути из файла (--paths):',
                    self.path_file, lambda: self.choose_file(self.path_file))
        self._field(files, 3, 'Начальные пути (--partial):', self.partial)
        self._field(files, 4, 'Фильтр (--filter):', self.filter_text)
        self._field(files, 5, 'Файл списка (--list-output):', self.list_output,
                    lambda: self.choose_save_file(self.list_output))

        logging_box = ttk.LabelFrame(parent, text='Журнал', padding=8)
        logging_box.pack(fill=tk.X, pady=(9, 0))
        logging_box.columnconfigure(1, weight=1)
        ttk.Checkbutton(logging_box, text='Вести лог (--log)',
                        variable=self.log_enabled).grid(row=0, column=0, sticky=tk.W)
        ttk.Entry(logging_box, textvariable=self.log_file).grid(
            row=0, column=1, sticky=tk.EW, padx=7)
        ttk.Button(logging_box, text='Обзор…',
                   command=lambda: self.choose_save_file(self.log_file)).grid(row=0, column=2)

        flags = ttk.LabelFrame(parent, text='Переключатели', padding=8)
        flags.pack(fill=tk.BOTH, expand=True, pady=(9, 0))
        checks = (
            ('Сканировать *.scs в указанных папках (--all)', self.scan_folders),
            ('Проверка без записи файлов (--dry-run)', self.dry_run),
            ('Не выводить каждый файл (--quiet)', self.quiet),
            ('Не перезаписывать существующие (--skip-existing)', self.skip_existing),
            ('Сырые записи по хешам (--raw)', self.raw),
            ('Не обновлять ссылки после переименования (--no-update)', self.no_update),
            ('Принудительно распаковывать вместе со списком (--force-extract)',
             self.force_extract),
            ('Таблица HashFS v1 находится в конце (--table-at-end)', self.table_at_end),
        )
        for index, (label, variable) in enumerate(checks):
            ttk.Checkbutton(flags, text=label, variable=variable).grid(
                row=index // 2, column=index % 2, sticky=tk.W, padx=(0, 22), pady=4)
        ttk.Label(flags, text='Salt HashFS (--salt):').grid(
            row=4, column=0, sticky=tk.W, pady=4)
        ttk.Entry(flags, textvariable=self.salt, width=12).grid(
            row=4, column=1, sticky=tk.W, pady=4)

    def _build_output_tab(self, parent: ttk.Frame) -> None:
        toolbar = ttk.Frame(parent)
        toolbar.pack(fill=tk.X, pady=(0, 6))
        ttk.Label(toolbar, text='Здесь отображается объединённый stdout/stderr консольной версии.',
                  style='Muted.TLabel').pack(side=tk.LEFT)
        ttk.Button(toolbar, text='Очистить вывод', command=self.clear_output).pack(side=tk.RIGHT)
        frame = ttk.Frame(parent)
        frame.pack(fill=tk.BOTH, expand=True)
        self.output = tk.Text(frame, wrap=tk.NONE, bg='#111111', fg='#e6e6e6',
                              insertbackground='white', font=('Consolas', 9),
                              state=tk.DISABLED)
        self.output_widgets = [self.output]
        y_scroll = ttk.Scrollbar(frame, orient=tk.VERTICAL, command=self.output.yview)
        x_scroll = ttk.Scrollbar(frame, orient=tk.HORIZONTAL, command=self.output.xview)
        self.output.configure(yscrollcommand=y_scroll.set, xscrollcommand=x_scroll.set)
        self.output.grid(row=0, column=0, sticky=tk.NSEW)
        y_scroll.grid(row=0, column=1, sticky=tk.NS)
        x_scroll.grid(row=1, column=0, sticky=tk.EW)
        frame.rowconfigure(0, weight=1)
        frame.columnconfigure(0, weight=1)

    def _install_traces(self) -> None:
        variables = (
            self.mode, self.destination, self.additional, self.reference, self.path_file,
            self.partial, self.filter_text, self.list_output, self.log_enabled,
            self.log_file, self.salt, self.scan_folders, self.deep, self.dry_run,
            self.no_update, self.force_extract, self.quiet, self.raw,
            self.separate, self.skip_existing, self.table_at_end,
            self.edit_manifest, self.compress_v1,
            self.compress_no_compact,
        )
        for variable in variables:
            variable.trace_add('write', lambda *_args: self.refresh_command())
        for variable in (self.edit_manifest, self.compress_v1,
                         self.compress_no_compact):
            variable.trace_add(
                'write', lambda *_args: self._update_operation_mode_visibility())

    def _apply_default_options(self) -> None:
        if self.always_deep.get() and not self.deep.get():
            self.deep.set(True)
        if self.always_log.get() and not self.log_enabled.get():
            self.log_enabled.set(True)

    def _save_app_preferences(self, notify: bool = False) -> None:
        def number(value: str, fallback: int) -> int:
            try:
                return max(0, int(value.strip()))
            except ValueError:
                return fallback
        self._app_settings.update({
            'language': self.language_code,
            'max_threads': max(1, number(self.max_threads.get(), 1)),
            'always_deep': bool(self.always_deep.get()),
            'always_log': bool(self.always_log.get()),
            'log_path': self.log_file.get().strip() or '_extraction.log',
        })
        self.max_threads.set(str(self._app_settings['max_threads']))
        self.log_file.set(str(self._app_settings['log_path']))
        save_app_settings(self._app_settings)
        self._save_community_preferences()
        if notify:
            self._set_status(self.t('settings_saved'))

    def _archive_tools_active(self) -> bool:
        return bool(self.edit_manifest.get() or self.compress_v1.get()
                    or self.compress_no_compact.get())

    def _update_operation_mode_visibility(self) -> None:
        """Hide extraction/listing modes while an archive rewrite tool is active."""
        mode_box = getattr(self, '_main_mode_box', None)
        pack_options = getattr(self, '_main_mode_box_pack', None)
        if mode_box is None or pack_options is None:
            return
        if self._archive_tools_active():
            mode_box.pack_forget()
        elif not mode_box.winfo_manager():
            mode_box.pack(**pack_options)

    def archive_paths(self) -> list[str]:
        return list(self.paths.get(0, tk.END))

    def build_arguments(self) -> list[str]:
        arguments = self.archive_paths()
        if not self._archive_tools_active():
            mode_switch = {
                'list': '--list', 'list_all': '--list-all',
                'list_entries': '--list-entries', 'tree': '--tree',
            }.get(self.mode.get())
            if mode_switch:
                arguments.append(mode_switch)
        boolean_options = (
            (self.scan_folders, '--all'), (self.deep, '--deep'),
            (self.dry_run, '--dry-run'), (self.no_update, '--no-update'),
            (self.force_extract, '--force-extract'), (self.quiet, '--quiet'),
            (self.raw, '--raw'), (self.separate, '--separate'),
            (self.skip_existing, '--skip-existing'),
            (self.table_at_end, '--table-at-end'),
            (self.edit_manifest, '--manifest'),
            (self.compress_v1, '--compress'),
            (self.compress_no_compact, '--jc'),
        )
        for variable, option in boolean_options:
            if variable.get():
                arguments.append(option)
        if self.destination.get().strip() not in ('', 'extracted'):
            arguments.append('--dest=' + self.destination.get().strip())
        for variable, option in (
            (self.additional, '--additional'), (self.path_file, '--paths'),
            (self.partial, '--partial'), (self.filter_text, '--filter'),
        ):
            if variable.get().strip():
                arguments.append(option + '=' + variable.get().strip())
        for reference in self.reference.get().split(';'):
            if reference.strip():
                arguments.append('--reference=' + reference.strip())
        if (not self._archive_tools_active() and self.list_output.get().strip()
                and self.mode.get() in ('list', 'list_all')):
            arguments.append('--list-output=' + self.list_output.get().strip())
        if self.log_enabled.get():
            log_file = self.log_file.get().strip()
            arguments.append('--log' if not log_file or log_file == '_extraction.log'
                             else '--log=' + log_file)
        if self.salt.get().strip():
            arguments.append('--salt=' + self.salt.get().strip())
        for old_value, new_value in self.manifest_replacements:
            old = old_value.get()
            new = new_value.get()
            if old:
                arguments.append(f'--manifest-replace={old}={new}')
        return arguments

    def refresh_command(self) -> None:
        arguments = self.build_arguments()
        self.command_preview.set(subprocess.list2cmdline(
            [r'.\UniversalSCSExtractor.exe', *arguments]))

    def add_archives(self) -> None:
        filenames = filedialog.askopenfilenames(
            title=self.t('choose_archives'),
            filetypes=((self.t('archive_files'), '*.scs *.zip'),
                       (self.t('all_files'), '*.*')))
        self._append_paths(filenames)

    def add_folder(self) -> None:
        directory = filedialog.askdirectory(title=self.t('choose_folder'))
        if directory:
            self._append_paths((directory,))
            self.scan_folders.set(True)

    def _append_paths(self, paths) -> None:
        existing = set(self.archive_paths())
        for path in paths:
            normalized = _display_input_path(path)
            if normalized not in existing:
                self.paths.insert(tk.END, normalized)
                existing.add(normalized)
        self.refresh_command()

    def remove_paths(self) -> None:
        for index in reversed(self.paths.curselection()):
            self.paths.delete(index)
        self.refresh_command()

    def clear_paths(self) -> None:
        self.paths.delete(0, tk.END)
        self.refresh_command()

    def choose_destination(self) -> None:
        directory = filedialog.askdirectory(title=self.t('choose_destination'))
        if directory:
            self.destination.set(directory)

    def choose_file(self, variable: tk.StringVar) -> None:
        filename = filedialog.askopenfilename(title=self.t('choose_file'))
        if filename:
            variable.set(filename)

    def choose_reference_files(self) -> None:
        filenames = filedialog.askopenfilenames(
            title=self.t('choose_archives'),
            filetypes=((self.t('archive_files'), '*.scs *.zip'),
                       (self.t('all_files'), '*.*')))
        if filenames:
            self.reference.set(';'.join(_display_input_path(path) for path in filenames))

    def choose_save_file(self, variable: tk.StringVar) -> None:
        filename = filedialog.asksaveasfilename(
            title=self.t('choose_file'), defaultextension='.txt',
            filetypes=((self.t('text_files'), '*.txt'), (self.t('all_files'), '*.*')))
        if filename:
            variable.set(filename)

    def copy_command(self) -> None:
        self.root.clipboard_clear()
        self.root.clipboard_append(self.command_preview.get())
        self._temporary_status(self.t('copied'))

    def _copy_text_widget(self, widget: tk.Text, *, all_text: bool = False) -> None:
        try:
            if all_text and widget in getattr(self, 'output_widgets', ()):
                tab_index = self.output_widgets.index(widget)
                records = self._activity_tabs[tab_index].get('records', [])
                copied_rows: list[str] = []
                for level, message in records:
                    if level == 'blank':
                        copied_rows.append('')
                    elif level == 'structure':
                        # Folding markers are an implementation detail. Tree
                        # node markers do represent visible content, however.
                        tree_node = self._tree_node_from_marker(message)
                        if tree_node is not None:
                            copied_rows.append(tree_node)
                    else:
                        copied_rows.append(self._translate_activity_message(message))
                text = '\n'.join(copied_rows)
            else:
                text = (widget.get('1.0', 'end-1c') if all_text else
                        widget.get(tk.SEL_FIRST, tk.SEL_LAST))
        except tk.TclError:
            return
        self.root.clipboard_clear()
        self.root.clipboard_append(text)
        self._temporary_status(self.t('text_copied'))

    def _set_status(self, value: str) -> None:
        self._status_base = value
        if self._status_notice_after is None:
            self.status.set(value)

    def _temporary_status(self, value: str, duration_ms: int = 1800) -> None:
        if self._status_notice_after is not None:
            self.root.after_cancel(self._status_notice_after)
        self.status.set(value)

        def restore() -> None:
            self._status_notice_after = None
            self.status.set(self._status_base)

        self._status_notice_after = self.root.after(duration_ms, restore)

    @staticmethod
    def _select_all_text(widget: tk.Text) -> str:
        widget.tag_add(tk.SEL, '1.0', 'end-1c')
        widget.mark_set(tk.INSERT, '1.0')
        widget.see('1.0')
        return 'break'

    def _enable_text_copying(self, widget: tk.Text) -> None:
        def copy_selection(_event=None) -> str:
            self._copy_text_widget(widget)
            return 'break'

        def show_menu(event) -> str:
            menu = tk.Menu(self.root, tearoff=False)
            menu.add_command(label=self.t('copy_selection'), command=copy_selection)
            menu.add_command(label=self.t('select_all'),
                             command=lambda: self._select_all_text(widget))
            try:
                menu.tk_popup(event.x_root, event.y_root)
            finally:
                menu.grab_release()
            return 'break'

        widget.bind('<Control-c>', copy_selection)
        widget.bind('<Control-C>', copy_selection)
        widget.bind('<Control-a>', lambda _event: self._select_all_text(widget))
        widget.bind('<Control-A>', lambda _event: self._select_all_text(widget))
        widget.bind('<Button-3>', show_menu)

    def clear_output(self) -> None:
        index = self._active_activity_index
        model = self._activity_tabs[index]
        model['fragment'] = ''
        records = model['records']
        assert isinstance(records, list)
        records.clear()
        model['groups'] = []
        model['active_group'] = None
        model['next_group_id'] = 0
        model['status_mark_created'] = False
        model['render_operation_id'] = 0
        model['aggregate'] = {}
        model['total_rendered'] = False
        output = self.output_widgets[index]
        output.configure(state=tk.NORMAL)
        output.delete('1.0', tk.END)
        output.configure(state=tk.NORMAL)

    @staticmethod
    def _archive_group_event(message: str) -> tuple[str, str] | None:
        """Return a structural extraction event from the untranslated CLI text."""
        started = re.fullmatch(r'Extracting\s+(.+?)\s+\.\.\.', message)
        if started:
            return 'start', started.group(1)
        summary = re.fullmatch(r'(.+?):\s+(\d+\s+extracted\b.*)', message,
                               flags=re.IGNORECASE)
        if summary:
            return 'summary', summary.group(2)
        summary = re.fullmatch(r'(\d+\s+extracted\b.*)', message,
                               flags=re.IGNORECASE)
        if summary:
            return 'summary', summary.group(1)
        return None

    def _start_archive_group(self, output: tk.Text, index: int,
                             level: str, message: str, archive: str,
                             ending: str) -> None:
        model = self._activity_tabs[index]
        groups = model.get('groups', [])
        render_operation_id = int(model.get('render_operation_id', 0))
        status_mark = f'archive_status_end_{index}_{render_operation_id}'
        if not model.get('status_mark_created', False):
            output.mark_set(status_mark, 'end-1c')
            output.mark_gravity(status_mark, tk.LEFT)
            model['status_mark_created'] = True
        marker_start = f'archive_marker_{index}_{model.get("next_group_id", 0)}_start'
        marker_end = f'archive_marker_{index}_{model.get("next_group_id", 0)}_end'
        output.mark_set(marker_start, 'end-1c')
        output.mark_gravity(marker_start, tk.LEFT)
        output.mark_set(marker_end, marker_start)
        # Let the boundary capture only the status row inserted immediately
        # below, then freeze it before streamed detail rows are appended.
        output.mark_gravity(marker_end, tk.RIGHT)
        output.insert(marker_start, self._translate_activity_message(message)
                      + (ending or '\n'), f'activity_{level}')
        output.mark_gravity(marker_end, tk.LEFT)

        group_id = int(model.get('next_group_id', 0))
        model['next_group_id'] = group_id + 1
        prefix = f'archive_group_{index}_{group_id}'
        header_start = prefix + '_header_start'
        detail_start = prefix + '_detail_start'
        output.mark_set(header_start, 'end-1c')
        output.mark_gravity(header_start, tk.LEFT)
        output.mark_set(detail_start, 'end-1c')
        # Keep the mark before streamed file rows.  It switches to RIGHT only
        # while the completed summary header is inserted at this same point.
        output.mark_gravity(detail_start, tk.LEFT)
        group = {
            'id': group_id, 'archive': archive, 'header_start': header_start,
            'detail_start': detail_start, 'detail_end': prefix + '_detail_end',
            'detail_tag': prefix + '_details', 'header_tag': prefix + '_header',
            'collapsed': False, 'details_visible': True,
            'record_start': None, 'record_end': None,
            'marker_start': marker_start, 'marker_end': marker_end,
            'status_mark': status_mark,
        }
        groups = model.setdefault('groups', [])
        assert isinstance(groups, list)
        groups.append(group)
        records = model.get('records', [])
        if isinstance(records, list):
            group['record_start'] = len(records)
        model['active_group'] = group

    def _promote_archive_marker(self, output: tk.Text, index: int,
                                group: dict[str, object]) -> None:
        """Move a completed archive's live marker above all folded sections."""
        model = self._activity_tabs[index]
        groups = model.get('groups', [])
        # A promoted live marker is inserted before the folded summaries.
        # Only summary starts that share that exact insertion point need to
        # move to its right.  Detail boundaries must always remain LEFT:
        # making them RIGHT lets later archives and the final Total line drift
        # inside an earlier fold, so collapsing it deletes unrelated output.
        if isinstance(groups, list):
            for item in groups:
                if isinstance(item, dict):
                    try:
                        output.mark_gravity(str(item['header_start']), tk.RIGHT)
                    except tk.TclError:
                        pass
        marker_start = str(group['marker_start'])
        marker_end = str(group['marker_end'])
        marker_text = output.get(marker_start, marker_end)
        output.delete(marker_start, marker_end)
        status_mark = str(group['status_mark'])
        output.mark_gravity(status_mark, tk.RIGHT)
        output.mark_set(marker_start, status_mark)
        output.mark_gravity(marker_start, tk.LEFT)
        output.mark_set(marker_end, marker_start)
        output.mark_gravity(marker_end, tk.LEFT)
        output.insert(status_mark, marker_text,
                      f'activity_{self._activity_level(marker_text)}')
        output.mark_gravity(status_mark, tk.LEFT)
        if isinstance(groups, list):
            for item in groups:
                if isinstance(item, dict):
                    try:
                        output.mark_gravity(str(item['header_start']), tk.LEFT)
                        output.mark_gravity(str(item['detail_start']), tk.LEFT)
                        output.mark_gravity(str(item['detail_end']), tk.LEFT)
                    except tk.TclError:
                        pass

        # Keep this operation-wide boundary after every promoted status row.
        # Moving it down to the just-finished header caused the next
        # "Extracting ..." row to appear between folded archive summaries.
        # With RIGHT gravity during insertion it naturally advances past the
        # new row and remains immediately before the first folded section.
        output.mark_gravity(status_mark, tk.LEFT)

    def _finish_archive_group(self, output: tk.Text, index: int,
                              summary: str, ending: str) -> bool:
        model = self._activity_tabs[index]
        group = model.get('active_group')
        if not isinstance(group, dict):
            return False

        detail_end = str(group['detail_end'])
        output.mark_set(detail_end, 'end-1c')
        # Freeze this boundary before later archives append their own rows.
        output.mark_gravity(detail_end, tk.LEFT)
        header_start = str(group['header_start'])
        detail_start = str(group['detail_start'])
        output.mark_gravity(detail_start, tk.RIGHT)
        header_tag = str(group['header_tag'])
        insert_mark = header_start + '_insert'
        output.mark_set(insert_mark, header_start)
        output.mark_gravity(insert_mark, tk.RIGHT)

        translated = self._translate_activity_message(
            f"{group['archive']}: {summary}")
        group['summary'] = summary
        aggregate = model.setdefault('aggregate', {})
        if isinstance(aggregate, dict):
            for label, display in (('extracted', 'extracted'), ('renamed', 'renamed'),
                                   ('modified', 'modified'), ('dumped', 'dumped'),
                                   ('skipped', 'skipped'), ('not_found', 'not found'),
                                   ('junk', 'junk'), ('failed', 'failed')):
                match = re.search(rf'(\d+)\s+{re.escape(display)}\b', summary,
                                  flags=re.IGNORECASE)
                if match:
                    aggregate[label] = int(aggregate.get(label, 0)) + int(match.group(1))
        output.insert(insert_mark, '\N{BLACK RIGHT-POINTING TRIANGLE} ',
                      ('activity_summary', header_tag))
        self._insert_summary(output, translated, insert_mark)
        output.insert(insert_mark, ending or '\n', header_tag)
        output.mark_unset(insert_mark)
        # Re-anchor the detail range immediately after the newly inserted
        # summary line.  This avoids relying on mark gravity when another
        # archive later inserts its status line above this section.
        output.mark_set(detail_start, f'{header_start} lineend +1c')
        output.mark_gravity(detail_start, tk.LEFT)
        output.tag_add(header_tag, header_start, detail_start)
        records = model.get('records', [])
        if isinstance(records, list):
            group['record_end'] = max(int(group.get('record_start') or 0), len(records) - 1)

        restored = model.get('restore_collapsed')
        group_number = len(model.get('groups', [])) - 1
        collapsed = (bool(restored[group_number])
                     if isinstance(restored, list) and group_number < len(restored)
                     else True)
        group['collapsed'] = collapsed
        if collapsed and group.get('details_visible', True):
            output.mark_set(detail_start, f'{header_start} lineend +1c')
            output.delete(detail_start, detail_end)
            output.mark_set(detail_end, detail_start)
            output.mark_gravity(detail_end, tk.LEFT)
            group['details_visible'] = False
        self._promote_archive_marker(output, index, group)
        model['active_group'] = None
        return True

    def _view_group_labels(self, kind: str, count: int = 0, *,
                           completed: bool = False) -> tuple[str, str]:
        """Return short, localised labels for a collapsible path view."""
        russian = getattr(self, 'language_code', 'ru') == 'ru'
        if kind == 'entries':
            if russian:
                title = ("\u0421\u043e\u0441\u0442\u0430\u0432\u043b\u0435\u043d\u0430 \u0442\u0430\u0431\u043b\u0438\u0446\u0430 \u0437\u0430\u043f\u0438\u0441\u0435\u0439" if completed else
                         "\u0421\u043e\u0441\u0442\u0430\u0432\u043b\u0435\u043d\u0438\u0435 \u0442\u0430\u0431\u043b\u0438\u0446\u044b \u0437\u0430\u043f\u0438\u0441\u0435\u0439")
            else:
                title = 'Entry table built' if completed else 'Building entry table'
            return title, f"{count} \u0437\u0430\u043f\u0438\u0441\u0435\u0439" if russian else f'{count} entries'
        if kind == 'tree':
            if russian:
                title = ("\u0421\u043e\u0441\u0442\u0430\u0432\u043b\u0435\u043d\u043e \u0434\u0435\u0440\u0435\u0432\u043e \u043f\u0443\u0442\u0435\u0439" if completed else
                         "\u0421\u043e\u0441\u0442\u0430\u0432\u043b\u0435\u043d\u0438\u0435 \u0434\u0435\u0440\u0435\u0432\u0430 \u043f\u0443\u0442\u0435\u0439")
            else:
                title = 'Path tree built' if completed else 'Building path tree'
            return title, f"{count} \u0443\u0437\u043b\u043e\u0432" if russian else f'{count} nodes'
        if russian:
            title = ("\u0421\u043e\u0441\u0442\u0430\u0432\u043b\u0435\u043d \u0441\u043f\u0438\u0441\u043e\u043a \u043f\u0443\u0442\u0435\u0439" if completed else
                     "\u0421\u043e\u0441\u0442\u0430\u0432\u043b\u0435\u043d\u0438\u0435 \u0441\u043f\u0438\u0441\u043a\u0430 \u043f\u0443\u0442\u0435\u0439")
        else:
            title = 'Path list built' if completed else 'Building path list'
        return title, f"{count} \u043f\u0443\u0442\u0435\u0439" if russian else f'{count} paths'

    @staticmethod
    def _view_group_event(message: str) -> tuple[str, str] | None:
        """Recognise invisible structural records emitted only by the GUI child."""
        events = {
            '__USEX_GUI_PATH_LIST_BEGIN__': ('start', 'list'),
            '__USEX_GUI_PATH_LIST_END__': ('end', 'list'),
            '__USEX_GUI_PATH_TREE_BEGIN__': ('start', 'tree'),
            '__USEX_GUI_PATH_TREE_END__': ('end', 'tree'),
            '__USEX_GUI_ENTRY_LIST_BEGIN__': ('start', 'entries'),
            '__USEX_GUI_ENTRY_LIST_END__': ('end', 'entries'),
        }
        if message in events:
            return events[message]
        prefix = '__USEX_GUI_PATH_TREE_NODE__'
        if message.startswith(prefix):
            return 'node', message[len(prefix):]
        return None

    def _start_view_group(self, output: tk.Text, index: int, kind: str,
                          records: list[tuple[str, str]], marker: str) -> None:
        title, _ = self._view_group_labels(kind)
        self._start_archive_group(output, index, 'info', title + ' ...', title, '\n')
        group = self._activity_tabs[index].get('active_group')
        if isinstance(group, dict):
            group['kind'] = kind
        # Keep the marker in the retained journal so switching language can
        # rebuild the folding structure, but never render it as a text row.
        records.append(('structure', marker))
        if isinstance(group, dict):
            group['record_start'] = len(records)

    def _finish_view_group(self, output: tk.Text, index: int, kind: str,
                           records: list[tuple[str, str]], marker: str,
                           ending: str) -> bool:
        group = self._activity_tabs[index].get('active_group')
        if not isinstance(group, dict) or group.get('kind') != kind:
            records.append(('structure', marker))
            return False
        record_start = int(group.get('record_start') or 0)
        count = sum(
            1 for level, message in records[record_start:]
            if ((kind == 'tree' and self._tree_node_from_marker(message) is not None)
                or (kind != 'tree' and level not in ('structure', 'blank')))
        )
        if kind == 'entries':
            # Do not count archive name, column heading and separator.
            count = max(0, count - 3)
        # The end marker plays the same role as a regular archive summary:
        # _finish_archive_group deliberately leaves that last record outside
        # the detail range restored after clicking the fold arrow.
        records.append(('structure', marker))
        completed_title, summary = self._view_group_labels(
            kind, count, completed=True)
        group['archive'] = completed_title
        return self._finish_archive_group(output, index, summary, ending)

    @staticmethod
    def _tree_node_info(message: str) -> tuple[str, bool, bool, str, str] | None:
        prefix = '__USEX_GUI_PATH_TREE_NODE__'
        if not message.startswith(prefix):
            return None
        try:
            parts = message[len(prefix):].split('\t')
            if len(parts) >= 5:
                flags, last, children, path = parts[:4]
                name = '\t'.join(parts[4:])
                return flags, last == '1', children == '1', path, name
            flags, last, name = parts
        except ValueError:
            return None
        return flags, last == '1', name.endswith('/'), name.rstrip('/'), name

    @classmethod
    def _tree_node_from_marker(cls, message: str) -> str | None:
        info = cls._tree_node_info(message)
        if info is None:
            return None
        flags, is_last, has_children, _path, name = info
        indent = ''.join('\u2502   ' if flag == '1' else '    ' for flag in flags)
        branch = '\u2514\u2500\u2500 ' if is_last else '\u251c\u2500\u2500 '
        toggle = '\u25bc ' if has_children else '  '
        return indent + branch + toggle + name

    def _append_total_summary(self, index: int) -> None:
        model = self._activity_tabs[index]
        # Path/list/tree/entry-table operations may still emit archive-like
        # counters while inspecting data.  They are not an extraction result.
        # Show the GUI-wide extracted total only for an actual extraction or
        # when extraction was explicitly requested alongside another mode.
        if not model.get('show_extraction_total', False):
            return
        if model.get('total_rendered', False):
            return
        aggregate = model.get('aggregate', {})
        if not isinstance(aggregate, dict):
            return
        # A process may finish before its final summary was structurally
        # attached (for example after a cancelled/short CLI run). Reconstruct
        # the counters from retained raw records so every activity tab gets
        # the same final line.
        records = model.get('records', [])
        if isinstance(records, list):
            for _level, message in records:
                if not isinstance(message, str) or ' extracted' not in message.lower():
                    continue
                for label, display in (('extracted', 'extracted'), ('renamed', 'renamed'),
                                       ('modified', 'modified'), ('dumped', 'dumped'),
                                       ('skipped', 'skipped'), ('not_found', 'not found'),
                                       ('junk', 'junk'), ('failed', 'failed')):
                    match = re.search(rf'(\d+)\s+{re.escape(display)}\b', message,
                                      flags=re.IGNORECASE)
                    if match:
                        value = int(match.group(1))
                        # Do not double count summaries already collected by
                        # _finish_archive_group; this fallback is only used
                        # when no aggregate exists yet.
                        if not aggregate:
                            aggregate[label] = aggregate.get(label, 0) + value
                if aggregate:
                    break
        # ``show_extraction_total`` already excludes listing/archive-tool
        # operations.  An empty aggregate here means extraction stopped before
        # an archive group was created (for example, a missing root listing),
        # and must still end with the operation-wide Total 0 line.
        # ``extracted`` already includes every successfully written file.
        # ``dumped`` is a successful extraction with an unresolved path, not
        # a failure, so keep it in the total and show it only as a breakdown.
        successfully_extracted = max(0, int(aggregate.get('extracted', 0)))
        total_line = f"Total {successfully_extracted} extracted"
        inner = [(display, aggregate.get(source, 0))
                 for source, display in (('renamed', 'renamed'),
                                         ('modified', 'modified'),
                                         ('dumped', 'dumped'))
                 if aggregate.get(source, 0)]
        if inner:
            total_line += ' (' + ', '.join(
                f'{value} {label}' for label, value in inner) + ')'
        trailing = []
        for label in ('skipped', 'not found', 'junk', 'failed'):
            value = aggregate.get(label.replace(' ', '_'), 0)
            if value:
                trailing.append(f'{value} {label}')
        if trailing:
            total_line += ', ' + ', '.join(trailing)
        self._write_output('\n' + total_line + '\n', tab_index=index)
        model['total_rendered'] = True

    def _insert_tree_group_details(self, output: tk.Text, index: int,
                                   group: dict[str, object], insert_mark: str) -> None:
        """Render visible tree nodes and bind every directory independently."""
        model = self._activity_tabs[index]
        records = model.get('records', [])
        if not isinstance(records, list):
            return
        collapsed = group.setdefault('tree_collapsed', set())
        if not isinstance(collapsed, set):
            collapsed = set()
            group['tree_collapsed'] = collapsed
        record_start = int(group.get('record_start') or 0)
        record_end = int(group.get('record_end') or record_start)
        for _level, message in records[record_start:record_end]:
            info = self._tree_node_info(message)
            if info is None:
                continue
            flags, is_last, has_children, path, name = info
            parts = [part for part in path.strip('/').split('/') if part]
            ancestors = ['/' + '/'.join(parts[:position])
                         for position in range(1, len(parts))]
            if any(parent in collapsed for parent in ancestors):
                continue
            indent = ''.join('\u2502   ' if flag == '1' else '    ' for flag in flags)
            branch = '\u2514\u2500\u2500 ' if is_last else '\u251c\u2500\u2500 '
            toggle = ('\u25b6 ' if path in collapsed else '\u25bc ') if has_children else '  '
            line = indent + branch + toggle + name + '\n'
            if has_children:
                tag = (f'tree_directory_{index}_{int(group.get("id", 0))}_'
                       f'{abs(hash(path))}')
                output.insert(insert_mark, line, ('activity_view_tree', tag))
                output.tag_configure(tag, underline=False)
                output.tag_bind(
                    tag, '<Button-1>',
                    lambda _event, tab=index, item=group, node=path:
                    self._toggle_tree_directory(tab, item, node))
                output.tag_bind(tag, '<Enter>', lambda _event: output.configure(cursor='hand2'))
                output.tag_bind(tag, '<Leave>', lambda _event: output.configure(cursor=''))
            else:
                output.insert(insert_mark, line, 'activity_view_tree')

    def _toggle_tree_directory(self, index: int, group: dict[str, object],
                               path: str) -> str:
        collapsed = group.setdefault('tree_collapsed', set())
        if not isinstance(collapsed, set):
            collapsed = set()
            group['tree_collapsed'] = collapsed
        if path in collapsed:
            collapsed.remove(path)
        else:
            collapsed.add(path)
        if not group.get('details_visible', False):
            return 'break'
        output = self.output_widgets[index]
        output.configure(state=tk.NORMAL)
        detail_start, detail_end = str(group['detail_start']), str(group['detail_end'])
        output.delete(detail_start, detail_end)
        output.mark_set(detail_end, detail_start)
        insert_mark = detail_start + '_tree_refresh'
        output.mark_set(insert_mark, detail_start)
        output.mark_gravity(insert_mark, tk.RIGHT)
        self._insert_tree_group_details(output, index, group, insert_mark)
        output.mark_set(detail_end, insert_mark)
        output.mark_gravity(detail_end, tk.LEFT)
        output.mark_unset(insert_mark)
        return 'break'

    def _toggle_archive_group(self, index: int, group: dict[str, object]) -> str:
        output = self.output_widgets[index]
        model = self._activity_tabs[index]
        collapsed = not bool(group.get('collapsed', True))
        output.configure(state=tk.NORMAL)
        # The archive name is not unique across sequential operations.  Its
        # per-group tag is, so use that instead of searching visible text.
        header_ranges = output.tag_ranges(str(group['header_tag']))
        header_position = header_ranges[0] if header_ranges else None
        if header_position:
            output.mark_set(str(group['header_start']), header_position)
            output.mark_gravity(str(group['header_start']), tk.LEFT)
        # Header anchors remain authoritative even after later archive status
        # lines were inserted above this section.
        output.mark_set(str(group['detail_start']),
                        f"{group['header_start']} lineend +1c")
        if not collapsed and not group.get('details_visible', True):
            records = model.get('records', [])
            record_start = int(group.get('record_start') or 0)
            record_end = int(group.get('record_end') or record_start)
            insert_mark = str(group['detail_start']) + '_restore'
            output.mark_set(insert_mark, str(group['detail_start']))
            output.mark_gravity(insert_mark, tk.RIGHT)
            pending: list[str] = []
            pending_tag: str | None = None

            def flush() -> None:
                nonlocal pending, pending_tag
                if pending:
                    output.insert(insert_mark, ''.join(pending), pending_tag or ())
                    pending = []
                    pending_tag = None

            if group.get('kind') == 'tree':
                self._insert_tree_group_details(output, index, group, insert_mark)
            elif isinstance(records, list):
                for level, message in records[record_start:record_end]:
                    if level == 'blank':
                        tag, text = 'activity_info', '\n'
                    elif level == 'structure':
                        tree_node = self._tree_node_from_marker(message)
                        if tree_node is None:
                            continue
                        tag, text = 'activity_view_tree', tree_node + '\n'
                    else:
                        tag = ('activity_view' if group.get('kind') in ('list', 'tree', 'entries')
                               else f'activity_{level}')
                        text = self._translate_activity_message(message) + '\n'
                    if pending_tag not in (None, tag):
                        flush()
                    pending_tag = tag
                    pending.append(text)
            flush()
            output.mark_set(str(group['detail_end']), insert_mark)
            output.mark_gravity(str(group['detail_end']), tk.LEFT)
            output.mark_unset(insert_mark)
            group['details_visible'] = True
        elif collapsed and group.get('details_visible', True):
            output.mark_set(str(group['detail_start']),
                            f"{group['header_start']} lineend +1c")
            output.delete(str(group['detail_start']), str(group['detail_end']))
            output.mark_set(str(group['detail_end']), str(group['detail_start']))
            output.mark_gravity(str(group['detail_end']), tk.LEFT)
            group['details_visible'] = False
        group['collapsed'] = collapsed
        start = str(group['header_start'])
        output.delete(start, f'{start}+1c')
        output.insert(start, ('\N{BLACK RIGHT-POINTING TRIANGLE}' if collapsed
                              else '\N{BLACK DOWN-POINTING TRIANGLE}'),
                      ('activity_summary', str(group['header_tag'])))
        output.configure(state=tk.NORMAL)
        return 'break'

    def _write_output(self, text: str, *, flush: bool = False,
                      tab_index: int | None = None) -> None:
        if not text and not flush:
            return
        index = self._active_activity_index if tab_index is None else tab_index
        model = self._activity_tabs[index]
        output = self.output_widgets[index]
        follow_tail = bool(model.get('follow_tail', True))
        fragment = str(model['fragment'])
        data = fragment + text
        lines = data.splitlines(keepends=True)
        model['fragment'] = ''
        if lines and not lines[-1].endswith(('\n', '\r')) and not flush:
            model['fragment'] = lines.pop()
        if not lines:
            return
        records = model['records']
        assert isinstance(records, list)
        model['updating_view'] = True
        output.configure(state=tk.NORMAL)
        pending_text: list[str] = []
        pending_tag: str | None = None

        def flush_pending() -> None:
            nonlocal pending_text, pending_tag
            if pending_text:
                output.insert(tk.END, ''.join(pending_text), pending_tag or ())
                pending_text = []
                pending_tag = None

        for line in lines:
            message = line.rstrip('\r\n')
            ending = line[len(message):]
            if not message:
                records.append(('blank', ''))
                pending_text.append(ending or '\n')
                continue
            if message.startswith('Running...'):
                # Begin a new visual operation inside this reusable journal.
                # Each operation owns a separate promotion boundary so its
                # "Extracting ..." rows cannot move into an earlier run.
                model['render_operation_id'] = int(
                    model.get('render_operation_id', 0)) + 1
                model['status_mark_created'] = False
                model['active_group'] = None
                model['traceback_active'] = False
            view_event = self._view_group_event(message)
            if view_event is not None:
                flush_pending()
                event, value = view_event
                if event == 'start':
                    self._start_view_group(output, index, value, records, message)
                    continue
                if event == 'node':
                    records.append(('structure', message))
                    tree_node = self._tree_node_from_marker(message)
                    if tree_node is not None:
                        output.insert(tk.END, tree_node + (ending or '\n'),
                                      'activity_view_tree')
                    continue
                self._finish_view_group(output, index, value, records, message, ending)
                continue
            level = self._activity_level(message)
            # Python emits a traceback as multiple neutral-looking lines
            # (file locations, source snippets and exception text).  Once its
            # header is seen, render the whole block as one error rather than
            # leaving every line after the first blue INFO colour.
            if message.startswith('Traceback (most recent call last):'):
                model['traceback_active'] = True
            if model.get('traceback_active'):
                level = 'error'
            records.append((level, message))
            if message.startswith('Total '):
                flush_pending()
                translated_total = self._translate_activity_message(message)
                total_start = output.index('end-1c')
                self._insert_summary(output, translated_total, total=True)
                total_end = output.index('end-1c')
                output.tag_add('activity_total_summary', total_start, total_end)
                output.insert(tk.END, ending or '\n', 'activity_total_summary')
                continue
            group_event = self._archive_group_event(message)
            if group_event is not None:
                flush_pending()
                event, value = group_event
                if event == 'start':
                    self._start_archive_group(
                        output, index, level, message, value, ending)
                    continue
                if self._finish_archive_group(output, index, value, ending):
                    continue
            translated = self._translate_activity_message(message)
            if re.search(r'\d+\s+(?:extracted|распаковано)\b', translated,
                         flags=re.IGNORECASE):
                flush_pending()
                self._insert_summary(output, translated)
                output.insert(tk.END, ending or '\n')
                continue
            active_group = model.get('active_group')
            tag = ('activity_view' if isinstance(active_group, dict)
                   and active_group.get('kind') in ('list', 'tree', 'entries')
                   else f'activity_{level}')
            if pending_tag not in (None, tag):
                flush_pending()
            pending_tag = tag
            pending_text.append(translated + (ending or '\n'))
        flush_pending()
        if follow_tail:
            output.see(tk.END)
        output.configure(state=tk.NORMAL)
        model['updating_view'] = False

    def _insert_activity_row(self, output: tk.Text, level: str, message: str) -> None:
        """Insert one event without a textual severity badge."""
        translated = self._translate_activity_message(message)
        if re.search(r'\d+\s+(?:extracted|распаковано)\b', translated,
                     flags=re.IGNORECASE):
            self._insert_summary(output, translated)
        else:
            output.insert(tk.END, translated, f'activity_{level}')

    def _insert_summary(self, output: tk.Text, message: str,
                        insertion_index: str = tk.END, total: bool = False) -> None:
        """Color only meaningful non-zero values in a final summary."""
        metric = re.compile(
            r'\d+\s+(?:extracted|распаковано|dumped|дампов|renamed|переименовано|'
            r'modified|changed|изменено|skipped|пропущено|junk|мусора|failed|ошибок|'
            r'not found|не найдено)',
            flags=re.IGNORECASE)
        position = 0
        for match in metric.finditer(message):
            output.insert(insertion_index, message[position:match.start()],
                          'activity_summary')
            fragment = match.group(0)
            count = int(fragment.split(None, 1)[0])
            label = fragment.split(None, 1)[1].lower()
            tag = 'activity_summary'
            if count:
                if label in ('skipped', 'пропущено', 'failed', 'ошибок',
                             'not found', 'не найдено'):
                    tag = 'activity_metric_error'
                elif label in ('dumped', 'дампов', 'renamed', 'переименовано',
                               'modified', 'changed', 'изменено', 'junk', 'мусора'):
                    tag = 'activity_metric_warn'
                elif label in ('extracted', 'распаковано'):
                    tag = 'activity_metric_success'
            output.insert(insertion_index, fragment, tag)
            position = match.end()
        output.insert(insertion_index, message[position:], 'activity_summary')

    def _restore_activity_output(self, _text: str = '') -> None:
        """Re-render retained raw events in the currently selected language."""
        for index, output in enumerate(self.output_widgets):
            model = self._activity_tabs[index]
            records = model['records']
            assert isinstance(records, list)
            retained = list(records)
            fragment = str(model.get('fragment', ''))
            groups = model.get('groups', [])
            model['restore_collapsed'] = [
                bool(group.get('collapsed', True)) for group in groups
                if isinstance(group, dict)
            ] if isinstance(groups, list) else []
            records.clear()
            model['fragment'] = ''
            model['groups'] = []
            model['active_group'] = None
            model['next_group_id'] = 0
            model['status_mark_created'] = False
            model['render_operation_id'] = 0
            output.configure(state=tk.NORMAL)
            output.delete('1.0', tk.END)
            output.configure(state=tk.NORMAL)
            payload = ''.join(('\n' if level == 'blank' else message + '\n')
                              for level, message in retained)
            if payload:
                self._write_output(payload, tab_index=index)
            model['fragment'] = fragment
            model.pop('restore_collapsed', None)
            self._activity_tabs[index]['follow_tail'] = True
            output.see(tk.END)

    def _jump_active_output_to_end(self, _event=None):
        if getattr(self, 'current_page', 'output') != 'output':
            return None
        index = self._active_activity_index
        if index >= len(getattr(self, 'output_widgets', ())):
            return None
        self._activity_tabs[index]['follow_tail'] = True
        self.output_widgets[index].see(tk.END)
        return 'break'

    def _translate_activity_message(self, message: str) -> str:
        """Translate known live extractor events without touching paths/names."""
        return translate_activity_message(
            message, getattr(self, 'language_code', 'ru'))

    @staticmethod
    def _activity_level(line: str) -> str:
        value = line.strip().lower()
        # Explicit embedded markers take precedence over an outer INFO prefix.
        if re.search(r'(?:^|\s)(?:error|err):', value):
            return 'error'
        if re.search(r'(?:^|\s)(?:warning|warn|wrn):', value):
            return 'warn'
        if re.search(r'(?:^|\s)(?:debug|dbg):', value):
            return 'debug'
        if value.startswith('success:'):
            return 'success'
        if value.startswith(('error:', 'err:')):
            return 'error'
        if value.startswith(('warning:', 'warn:', 'wrn:')):
            return 'warn'
        if value.startswith(('debug:', 'dbg:')):
            return 'debug'
        if value.startswith(('info:', 'inf:')):
            return 'info'
        if '[dbg]' in value or ' dbg]' in value or ' debug]' in value:
            return 'debug'
        if '[wrn]' in value or ' wrn]' in value or ' warn]' in value:
            return 'warn'
        if '[err]' in value or ' err]' in value or ' error]' in value:
            return 'error'
        if ('extracted' in value and value.endswith(' 0 failed')) or 'success' in value:
            return 'success'
        if any(token in value for token in (
                'traceback', 'exception', 'failed to',
                'unable to', ' failed', 'not found')):
            return 'error'
        if value.startswith('top level directory is missing'):
            return 'error'
        if 'cannot scan' in value:
            return 'warn'
        if value.startswith(('extracted ', 'validated ')):
            return 'success'
        if value.startswith(('dumping ', 'dumped ')):
            return 'warn'
        if value.startswith((
                'updated path list ', 'created path list ',
                'обновлён список путей ', 'создан список путей ')):
            return 'success'
        if value.startswith(('skipping ', 'skipped ')):
            return 'error'
        if value.startswith('searching for paths'):
            return 'info'
        if value.startswith(('opened ', 'opening ', 'reading ', 'scanning ',
                             'loading ', 'searching ')):
            return 'debug'
        return 'info'

    def _validate(self) -> bool:
        if not self.archive_paths():
            messagebox.showerror(APP_NAME, self.t('need_input'))
            return False
        if self.path_file.get().strip() and self.partial.get().strip():
            messagebox.showerror(
                APP_NAME, self.t('paths_conflict'))
            return False
        salt = self.salt.get().strip()
        if salt:
            try:
                value = int(salt)
            except ValueError:
                value = -1
            if not 0 <= value <= 65535:
                messagebox.showerror(APP_NAME, self.t('bad_salt'))
                return False
        return True

    def configure_community(self) -> None:
        """Configure the private webhook and the one message holding its file."""
        strings = ({
            'url': 'URL вебхука Discord',
            'message': 'ID сообщения-хранилища',
            'note': ('URL хранится только в профиле текущего пользователя Windows и не попадает '
                     'в команду или журнал. Создайте хранилище один раз: это будет одно сообщение '
                     'со сменяемым сжатым списком путей.'),
            'create': 'Создать сообщение-хранилище', 'save': 'Сохранить',
            'paste': 'Вставить',
            'bad_url': 'Введите корректный URL вебхука Discord.',
            'bad_id': 'ID сообщения-хранилища должен состоять только из цифр.',
            'first_url': 'Сначала введите корректный URL вебхука Discord.',
            'create_error': 'Не удалось создать хранилище: {error}',
        } if self.language_code == 'ru' else {
            'url': 'Discord webhook URL', 'message': 'Storage message ID',
            'note': ('The URL is stored only in this Windows user profile and is never put '
                     'into the command line or activity log. Create storage once to make one '
                     'message with a replaceable compressed path-list attachment.'),
            'create': 'Create storage message', 'save': 'Save',
            'paste': 'Paste',
            'bad_url': 'Enter a valid Discord webhook URL.',
            'bad_id': 'Storage message ID must contain digits only.',
            'first_url': 'Enter a valid Discord webhook URL first.',
            'create_error': 'Could not create storage: {error}',
        })
        dialog = tk.Toplevel(self.root)
        dialog.title(self.t('community_setup'))
        dialog.configure(bg=COLORS['window'])
        dialog.transient(self.root)
        dialog.grab_set()
        frame = ttk.Frame(dialog, style='Content.TFrame', padding=16)
        frame.pack(fill=tk.BOTH, expand=True)
        url = tk.StringVar(value=str(self._community_config.get('webhook_url', '')))
        message_id = tk.StringVar(value=str(self._community_config.get('message_id', '')))
        ttk.Label(frame, text=str(strings['url'])).grid(row=0, column=0, sticky=tk.W, pady=(0, 5))
        url_row = ttk.Frame(frame, style='Content.TFrame')
        url_row.grid(row=1, column=0, sticky=tk.EW)
        url_entry = ttk.Entry(url_row, textvariable=url, width=58)
        url_entry.pack(side=tk.LEFT, fill=tk.X, expand=True)
        ttk.Label(frame, text=str(strings['message'])).grid(row=2, column=0, sticky=tk.W, pady=(12, 5))
        id_row = ttk.Frame(frame, style='Content.TFrame')
        id_row.grid(row=3, column=0, sticky=tk.EW)
        id_entry = ttk.Entry(id_row, textvariable=message_id, width=58)
        id_entry.pack(side=tk.LEFT, fill=tk.X, expand=True)
        ttk.Label(frame, text=str(strings['note']),
                  style='Muted.TLabel', wraplength=510, justify=tk.LEFT).grid(
                      row=4, column=0, sticky=tk.W, pady=(12, 12))
        frame.columnconfigure(0, weight=1)

        def entry_shortcuts(entry: ttk.Entry) -> None:
            def clipboard_text() -> str:
                try:
                    return str(self.root.clipboard_get())
                except tk.TclError:
                    if sys.platform != 'win32':
                        raise
                # Tk's clipboard target can be unavailable in a frozen,
                # windowed process.  Read CF_UNICODETEXT directly on Windows.
                import ctypes
                user32, kernel32 = ctypes.windll.user32, ctypes.windll.kernel32
                user32.GetClipboardData.restype = ctypes.c_void_p
                kernel32.GlobalLock.restype = ctypes.c_void_p
                if not user32.OpenClipboard(None):
                    raise tk.TclError('Could not open Windows clipboard')
                try:
                    handle = user32.GetClipboardData(13)  # CF_UNICODETEXT
                    pointer = kernel32.GlobalLock(handle) if handle else None
                    if not pointer:
                        raise tk.TclError('Clipboard has no Unicode text')
                    try:
                        return ctypes.wstring_at(pointer)
                    finally:
                        kernel32.GlobalUnlock(handle)
                finally:
                    user32.CloseClipboard()

            def paste(_event=None) -> str:
                try:
                    text = clipboard_text()
                    try:
                        entry.delete(tk.SEL_FIRST, tk.SEL_LAST)
                    except tk.TclError:
                        pass
                    entry.insert(tk.INSERT, text)
                except (tk.TclError, OSError):
                    return 'break'
                return 'break'
            def copy(event, cut: bool = False) -> str:
                try:
                    self.root.clipboard_clear()
                    self.root.clipboard_append(entry.selection_get())
                    if cut:
                        entry.delete(tk.SEL_FIRST, tk.SEL_LAST)
                except tk.TclError:
                    pass
                return 'break'
            def select_all(_event) -> str:
                entry.selection_range(0, tk.END)
                entry.icursor(tk.END)
                return 'break'
            entry.bind('<Control-v>', paste, add='+')
            entry.bind('<Control-V>', paste, add='+')
            entry.bind('<Control-Key-v>', paste, add='+')
            entry.bind('<Control-Key-V>', paste, add='+')
            entry.bind('<Shift-Insert>', paste, add='+')
            entry.bind('<Control-c>', lambda event: copy(event))
            entry.bind('<Control-C>', lambda event: copy(event))
            entry.bind('<Control-x>', lambda event: copy(event, True))
            entry.bind('<Control-X>', lambda event: copy(event, True))
            entry.bind('<Control-a>', select_all)
            entry.bind('<Control-A>', select_all)
            return paste
        url_paste = entry_shortcuts(url_entry)
        id_paste = entry_shortcuts(id_entry)
        ttk.Button(url_row, text=str(strings['paste']),
                   command=url_paste).pack(side=tk.LEFT, padx=(8, 0))
        ttk.Button(id_row, text=str(strings['paste']),
                   command=id_paste).pack(side=tk.LEFT, padx=(8, 0))

        def save() -> None:
            value = url.get().strip()
            if not valid_webhook(value):
                messagebox.showerror(APP_NAME, str(strings['bad_url']), parent=dialog)
                return
            if message_id.get().strip() and not message_id.get().strip().isdecimal():
                messagebox.showerror(APP_NAME, str(strings['bad_id']), parent=dialog)
                return
            self._community_config.update({'webhook_url': value,
                                           'message_id': message_id.get().strip()})
            self._save_community_preferences()
            dialog.destroy()

        def create() -> None:
            value = url.get().strip()
            if not valid_webhook(value):
                messagebox.showerror(APP_NAME, str(strings['first_url']), parent=dialog)
                return
            try:
                message_id.set(create_storage(value))
            except Exception as exc:
                messagebox.showerror(
                    APP_NAME, str(strings['create_error']).format(error=exc), parent=dialog)

        buttons = ttk.Frame(frame, style='Content.TFrame')
        buttons.grid(row=5, column=0, sticky=tk.E)
        ttk.Button(buttons, text=str(strings['create']), command=create).pack(side=tk.LEFT, padx=(0, 8))
        ttk.Button(buttons, text=str(strings['save']), command=save).pack(side=tk.LEFT)

    def _save_community_preferences(self) -> None:
        self._community_config['share'] = bool(self.community_share.get())
        self._community_config['use'] = bool(self.community_use.get())
        if not self.community_share.get():
            # Disabling consent must also revoke the remembered automatic
            # publication choice.  Re-enabling starts with a fresh prompt.
            self._community_config.pop('auto_publish', None)
            self._community_config.pop('auto_publish_confirmed', None)
        save_config(self._community_config)

    def _community_network_wait(self, message: str, task):
        """Run a cancellable Discord transfer without freezing Tk."""
        dialog = tk.Toplevel(self.root)
        dialog.title(APP_NAME)
        dialog.configure(bg=COLORS['window'])
        dialog.transient(self.root)
        dialog.grab_set()
        frame = ttk.Frame(dialog, style='Content.TFrame', padding=18)
        frame.pack(fill=tk.BOTH, expand=True)
        ttk.Label(frame, text=message, style='Card.TLabel').pack(anchor=tk.W)
        progress = ttk.Progressbar(frame, mode='indeterminate', length=330)
        progress.pack(fill=tk.X, pady=(12, 0))
        progress.start(12)
        state: dict[str, object] = {}
        cancelled = threading.Event()
        response_holder: dict[str, object] = {}

        def worker() -> None:
            try:
                state['value'] = task(cancelled, response_holder)
            except Exception as exc:
                state['error'] = exc
            finally:
                state['done'] = True

        threading.Thread(target=worker, daemon=True).start()

        def cancel() -> None:
            cancelled.set()
            response = response_holder.get('response')
            if response is not None:
                try:
                    response.close()
                except OSError:
                    pass

        controls = ttk.Frame(frame, style='Content.TFrame')
        controls.pack(anchor=tk.E, pady=(12, 0))
        ttk.Button(controls, text=('Отмена' if self.language_code == 'ru' else 'Cancel'),
                   command=cancel).pack()

        def check() -> None:
            if state.get('done'):
                progress.stop()
                dialog.destroy()
            else:
                dialog.after(50, check)

        dialog.protocol('WM_DELETE_WINDOW', cancel)
        dialog.after(50, check)
        self.root.wait_window(dialog)
        if cancelled.is_set() and 'error' not in state:
            raise CommunityTransferCancelled('Community transfer cancelled')
        if 'error' in state:
            raise state['error']  # type: ignore[misc]
        return state.get('value')

    def _ask_publish_community_paths(self, count: int) -> bool:
        """Ask once, with an explicit opt-in for later automatic publishing."""
        if (bool(self._community_config.get('auto_publish', False))
                and bool(self._community_config.get('auto_publish_confirmed', False))):
            return True
        language_ru = self.language_code == 'ru'
        strings = ({
            'title': 'Публикация путей',
            'body': f'Найдено новых подтверждённых путей: {count}.\nОпубликовать их для других пользователей?',
            'never': 'Больше не спрашивать — публиковать новые пути автоматически',
            'publish': 'Опубликовать', 'later': 'Позже',
        } if language_ru else {
            'title': 'Publish paths',
            'body': f'Found {count} new verified path(s).\nPublish them for other users?',
            'never': 'Do not ask again — publish new paths automatically',
            'publish': 'Publish', 'later': 'Later',
        })
        dialog = tk.Toplevel(self.root)
        dialog.title(str(strings['title']))
        dialog.configure(bg=COLORS['window'])
        dialog.transient(self.root)
        dialog.grab_set()
        answer = tk.BooleanVar(value=False)
        auto = tk.BooleanVar(value=False)
        body = ttk.Frame(dialog, style='Content.TFrame', padding=16)
        body.pack(fill=tk.BOTH, expand=True)
        ttk.Label(body, text=str(strings['body']), style='Card.TLabel',
                  justify=tk.LEFT).pack(anchor=tk.W)
        ttk.Checkbutton(body, text=str(strings['never']), variable=auto).pack(
            anchor=tk.W, pady=(12, 12))

        def finish(publish: bool) -> None:
            if publish and auto.get():
                self._community_config['auto_publish'] = True
                self._community_config['auto_publish_confirmed'] = True
                self._save_community_preferences()
            answer.set(publish)
            dialog.destroy()

        controls = ttk.Frame(body, style='Content.TFrame')
        controls.pack(anchor=tk.E)
        ttk.Button(controls, text=str(strings['later']),
                   command=lambda: finish(False)).pack(side=tk.LEFT, padx=(0, 8))
        ttk.Button(controls, text=str(strings['publish']),
                   command=lambda: finish(True)).pack(side=tk.LEFT)
        dialog.protocol('WM_DELETE_WINDOW', lambda: finish(False))
        self.root.wait_window(dialog)
        return bool(answer.get())

    def _publish_pending_community_paths(self, pending: set[str], webhook: str,
                                         message_id: str) -> bool:
        try:
            self._community_network_wait(
                'Публикация путей…' if self.language_code == 'ru' else 'Publishing paths…',
                lambda cancel, holder: publish_paths(
                    webhook, message_id,
                    fetch_paths(webhook, message_id, cancel, holder) | pending,
                    cancel, holder))
            write_pending(set())
            self._set_status('Community paths published')
            return True
        except Exception as exc:
            messagebox.showwarning(APP_NAME, f'Could not publish pending paths: {exc}')
            return False

    def _ask_pending_community_paths(self) -> None:
        """Honor a deferred publish decision as soon as the next GUI starts."""
        pending = read_pending()
        webhook = str(self._community_config.get('webhook_url', '')).strip()
        message_id = str(self._community_config.get('message_id', '')).strip()
        if not pending or not valid_webhook(webhook) or not message_id.isdecimal():
            return
        if not self.community_share.get():
            return
        if not self._ask_publish_community_paths(len(pending)):
            return
        self._publish_pending_community_paths(pending, webhook, message_id)

    def _community_enabled(self) -> bool:
        return bool(self.community_share.get() or self.community_use.get())

    def _prepare_community_run(self, arguments: list[str], tab_index: int) -> list[str] | None:
        """Fetch the shared dictionary and inject private child-only hand-off files."""
        self._save_community_preferences()
        if not self._community_enabled():
            return arguments
        webhook = str(self._community_config.get('webhook_url', '')).strip()
        message_id = str(self._community_config.get('message_id', '')).strip()
        if not valid_webhook(webhook) or not message_id.isdecimal():
            messagebox.showerror(APP_NAME, 'Configure a Discord webhook and storage message first.')
            return None
        pending = read_pending()
        if pending:
            if self._ask_publish_community_paths(len(pending)):
                self._publish_pending_community_paths(pending, webhook, message_id)
        try:
            shared = self._community_network_wait(
                'Загрузка общих путей…' if self.language_code == 'ru' else 'Loading community paths…',
                lambda cancel, holder: fetch_paths(webhook, message_id, cancel, holder))
        except Exception as exc:
            messagebox.showwarning(APP_NAME, f'Could not load community paths: {exc}\nExtraction will continue.')
            shared = set()
        result_file = temporary_path_list(set())
        merged = set(shared) if self.community_use.get() else set()
        additional = self.additional.get().strip()
        if additional:
            try:
                merged.update(read_local_paths(additional))
            except Exception as exc:
                messagebox.showwarning(APP_NAME, f'Could not read additional path list: {exc}')
        temporary = temporary_path_list(merged) if merged else None
        arguments = [argument for argument in arguments if not argument.startswith('--additional=')]
        if temporary:
            arguments.append('--additional=' + str(temporary))
        if self.community_share.get():
            arguments.append('--community-new-paths-output=' + str(result_file))
        self._community_runs[tab_index] = {'shared': shared, 'result': result_file,
                                           'temporary': temporary, 'webhook': webhook,
                                           'message_id': message_id,
                                           'share': bool(self.community_share.get())}
        return arguments

    def _finish_community_run(self, tab_index: int) -> None:
        state = self._community_runs.pop(tab_index, None)
        if not state:
            return
        result = state['result']
        temporary = state['temporary']
        try:
            recovered = read_local_paths(str(result))
        except Exception:
            recovered = set()
        for path in (result, temporary):
            if path:
                try:
                    Path(str(path)).unlink(missing_ok=True)
                except OSError:
                    pass
        new_paths = recovered - set(state['shared'])
        if not state['share'] or not new_paths:
            return
        pending = read_pending() | new_paths
        write_pending(pending)
        if not self._ask_publish_community_paths(len(new_paths)):
            return
        self._publish_pending_community_paths(
            pending, str(state['webhook']), str(state['message_id']))

    def run(self) -> None:
        tab_index = self._active_activity_index
        if tab_index in self._processes or not self._validate():
            return
        # A journal tab is reusable.  Keep its previous text, but start a new
        # per-run aggregate so every subsequent extraction receives its own
        # final "Total ..." line as well.
        model = self._activity_tabs[tab_index]
        model['aggregate'] = {}
        model['total_rendered'] = False
        self._save_app_preferences()
        arguments = self.build_arguments()
        arguments = self._prepare_community_run(arguments, tab_index)
        if arguments is None:
            return
        model['show_extraction_total'] = (
            not self._archive_tools_active()
            and (self.mode.get() == 'extract' or '--force-extract' in arguments))
        # Run the CLI implementation as a hidden child and render its output
        # in the application's structured Activity page.  The marker avoids
        # treating a single selected archive as another GUI launch.
        command = _runtime_command(['--gui-child', *arguments])
        records = self._activity_tabs[tab_index]['records']
        if records:
            self._write_output('\n', tab_index=tab_index)
        # Store a language-neutral event so changing the UI language also
        # translates this already-rendered row.
        self._write_output('Running...  ' + self.command_preview.get() + '\n\n',
                           tab_index=tab_index)
        self.notebook.select(2)
        self._set_status(self.t('running'))
        try:
            creationflags = ((subprocess.CREATE_NO_WINDOW |
                              subprocess.CREATE_NEW_PROCESS_GROUP)
                             if sys.platform == 'win32' else 0)
            environment = os.environ.copy()
            environment['PYTHONUNBUFFERED'] = '1'
            environment['PYTHONIOENCODING'] = 'utf-8'
            environment['UNIVERSAL_SCS_EXTRACTOR_GUI_CHILD'] = '1'
            environment['UNIVERSAL_SCS_MAX_THREADS'] = self.max_threads.get()
            if self._activity_tabs[tab_index].get('announce_new_process', False):
                environment['UNIVERSAL_SCS_EXTRACTOR_NEW_PROCESS'] = '1'
            process = subprocess.Popen(
                command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                stdin=subprocess.DEVNULL, bufsize=0, creationflags=creationflags,
                env=environment, cwd=_application_directory())
        except OSError as exc:
            self._update_process_controls()
            messagebox.showerror(APP_NAME, self.t('launch_error').format(error=exc))
            return
        operation_id = self._next_operation_id
        self._next_operation_id += 1
        self._processes[tab_index] = process
        self._operation_by_tab[tab_index] = operation_id
        model['operation_id'] = operation_id
        self._activity_tabs[tab_index]['announce_new_process'] = False
        self.process = process
        self._update_activity_tab_title(tab_index)
        self._update_process_controls()
        threading.Thread(target=self._read_process,
                         args=(process, tab_index, operation_id), daemon=True).start()

    def _read_process(self, process: subprocess.Popen,
                      tab_index: int, operation_id: int) -> None:
        if process is None or process.stdout is None:
            return
        decoder = codecs.getincrementaldecoder('utf-8')('replace')
        try:
            while True:
                chunk = process.stdout.read(65536)
                if not chunk:
                    break
                text = decoder.decode(chunk)
                if text:
                    self.output_queue.put(
                        ('text', (operation_id, tab_index, process, text)))
            tail = decoder.decode(b'', final=True)
            if tail:
                self.output_queue.put(
                    ('text', (operation_id, tab_index, process, tail)))
            code = process.wait()
            self.output_queue.put(
                ('done', (operation_id, tab_index, process, code)))
        except Exception as exc:  # keep GUI alive if pipe reading fails
            self.output_queue.put(('text', (
                operation_id, tab_index, process,
                '\n' + self.t('read_error').format(error=exc) + '\n')))
            self.output_queue.put(('done', (
                operation_id, tab_index, process,
                process.poll() if process.poll() is not None else 1)))

    def _is_current_operation(self, operation_id: int, tab_index: int,
                              process: subprocess.Popen) -> bool:
        """Accept output only from the process assigned to this exact tab run."""
        return (0 <= tab_index < len(self._activity_tabs)
                and self._operation_by_tab.get(tab_index) == operation_id
                and self._processes.get(tab_index) is process
                and self._activity_tabs[tab_index].get('operation_id') == operation_id)

    def _drain_output(self) -> None:
        text_by_operation: dict[
            tuple[int, int, subprocess.Popen], list[str]] = {}
        completed: list[tuple[int, int, subprocess.Popen, int]] = []
        queued_characters = 0
        try:
            for _ in range(MAX_OUTPUT_CHUNKS_PER_TICK):
                kind, value = self.output_queue.get_nowait()
                if kind == 'text':
                    operation_id, tab_index, process, text = value
                    operation_id = int(operation_id)
                    tab_index = int(tab_index)
                    if not self._is_current_operation(
                            operation_id, tab_index, process):
                        continue
                    text = str(text)
                    key = (operation_id, tab_index, process)
                    text_by_operation.setdefault(key, []).append(text)
                    queued_characters += len(text)
                    if queued_characters >= MAX_OUTPUT_CHARS_PER_TICK:
                        break
                elif kind == 'done':
                    operation_id, tab_index, process, code = value
                    operation_id = int(operation_id)
                    tab_index = int(tab_index)
                    if self._is_current_operation(
                            operation_id, tab_index, process):
                        completed.append(
                            (operation_id, tab_index, process, int(code)))
        except queue.Empty:
            pass
        for (operation_id, tab_index, process), chunks in text_by_operation.items():
            # Completion from another reader may have been processed in a
            # previous GUI tick.  Recheck before touching the tab model.
            if self._is_current_operation(operation_id, tab_index, process):
                self._write_output(''.join(chunks), tab_index=tab_index)
        for operation_id, tab_index, process, code in completed:
            if not self._is_current_operation(operation_id, tab_index, process):
                continue
            self._write_output('', flush=True, tab_index=tab_index)
            cancelled = operation_id in self._cancelled_operations
            if not cancelled:
                self._append_total_summary(tab_index)
            self._finish_community_run(tab_index)
            self._processes.pop(tab_index, None)
            self._operation_by_tab.pop(tab_index, None)
            self._activity_tabs[tab_index]['operation_id'] = None
            self._cancelled_operations.discard(operation_id)
            self._update_activity_tab_title(tab_index)
            self.process = next(iter(self._processes.values()), None)
            self._set_status(
                self.t('running') if self._processes else
                self.t('success') if code == 0 else
                self.t('failed').format(code=code))
            self._update_process_controls()
        if not self._closing:
            self.root.after(10 if not self.output_queue.empty() else 75, self._drain_output)

    def _update_process_controls(self) -> None:
        if not hasattr(self, 'run_button'):
            return
        active_running = self._active_activity_index in self._processes
        self.run_button.configure(state=tk.DISABLED if active_running else tk.NORMAL)
        self.stop_button.configure(state=tk.NORMAL if active_running else tk.DISABLED)
        if hasattr(self, 'new_tab_run_button'):
            self.new_tab_run_button.configure(state=tk.NORMAL)

    def stop(self) -> None:
        process = self._processes.get(self._active_activity_index)
        if process is None:
            return
        if messagebox.askyesno(APP_NAME, self.t('stop_question')):
            operation_id = self._operation_by_tab.get(self._active_activity_index)
            if operation_id is not None:
                self._cancelled_operations.add(operation_id)
            self._terminate_process(process)
            self._set_status(self.t('stopping'))

    def _terminate_process(self, process: subprocess.Popen | None = None) -> None:
        """Terminate the CLI and its worker processes, not only their parent."""
        process = process or self.process
        if process is None:
            return
        try:
            if sys.platform == 'win32':
                subprocess.run(
                    ['taskkill', '/PID', str(process.pid), '/T', '/F'],
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                    creationflags=subprocess.CREATE_NO_WINDOW, check=False)
            else:
                process.terminate()
        except OSError:
            try:
                process.terminate()
            except OSError:
                pass

    def open_destination(self) -> None:
        destination = Path(self.destination.get().strip() or 'extracted').resolve()
        if not destination.exists():
            messagebox.showinfo(APP_NAME, self.t('missing_destination').format(path=destination))
            return
        try:
            if sys.platform == 'win32':
                os.startfile(destination)  # type: ignore[attr-defined]
            elif sys.platform == 'darwin':
                subprocess.Popen(['open', str(destination)])
            else:
                subprocess.Popen(['xdg-open', str(destination)])
        except OSError as exc:
            messagebox.showerror(APP_NAME, self.t('open_error').format(error=exc))

    def close(self) -> None:
        if self._processes:
            if not messagebox.askyesno(APP_NAME, self.t('close_question')):
                return
            for process in list(self._processes.values()):
                self._terminate_process(process)
        self._closing = True
        self._save_app_preferences()
        self.root.destroy()


class _PageRouter:
    """Compatibility shim for the backend's historical Notebook call."""
    def __init__(self, gui):
        self.gui = gui

    def select(self, page):
        self.gui.show_page('output' if page == 2 else str(page))


class ModernExtractorGui(ExtractorGui):
    """Dark, responsive shell around the shared extraction backend."""
    def __init__(self, root: tk.Tk, initial_paths: list[str]):
        self._app_settings = load_app_settings()
        self.language_code = ('en' if self._app_settings.get('language') == 'en'
                              else 'ru')
        self.language_choice = tk.StringVar(
            master=root, value='English' if self.language_code == 'en' else 'Русский')
        self.help_query = tk.StringVar(master=root)
        self.current_page = 'main'
        self.plugin_manager = PluginManager(_application_directory())
        self.plugin_symbols = gui_symbols(self.plugin_manager.execute(
            'gui.initialize', {'language': self.language_code,
                               'application': APP_NAME}))
        super().__init__(root, initial_paths)
        self._set_status(self.t('ready'))

    def t(self, key: str) -> str:
        return UI_TEXT[self.language_code].get(key, key)

    @staticmethod
    def _asset(name: str) -> Path:
        if getattr(sys, 'frozen', False) and hasattr(sys, '_MEIPASS'):
            return Path(sys._MEIPASS) / 'assets' / name
        return Path(__file__).resolve().parent.parent / 'assets' / name

    def _configure_style(self) -> None:
        self.root.configure(bg=COLORS['window'])
        style = ttk.Style(self.root)
        style.theme_use('clam')
        style.configure('.', background=COLORS['window'], foreground=COLORS['text'],
                        font=('Segoe UI', 10), borderwidth=0)
        style.configure('App.TFrame', background=COLORS['window'])
        style.configure('Content.TFrame', background=COLORS['window'])
        style.configure('Card.TFrame', background=COLORS['surface'])
        style.configure('Card.TLabelframe', background=COLORS['surface'],
                        foreground=COLORS['muted'], bordercolor=COLORS['border'],
                        relief='solid', borderwidth=1)
        style.configure('Card.TLabelframe.Label', background=COLORS['surface'],
                        foreground=COLORS['muted'], font=('Segoe UI Semibold', 9))
        style.configure('TLabel', background=COLORS['window'], foreground=COLORS['text'])
        style.configure('Card.TLabel', background=COLORS['surface'], foreground=COLORS['text'])
        style.configure('Title.TLabel', background=COLORS['window'], foreground=COLORS['text'],
                        font=('Segoe UI Semibold', 21))
        style.configure('HelpTitle.TLabel', background=COLORS['surface'],
                        foreground=COLORS['text'], font=('Segoe UI', 17, 'bold'))
        style.configure('Muted.TLabel', background=COLORS['window'], foreground=COLORS['muted'])
        style.configure('CardMuted.TLabel', background=COLORS['surface'], foreground=COLORS['muted'])
        style.configure('TEntry', fieldbackground=COLORS['surface2'], foreground=COLORS['text'],
                        insertcolor=COLORS['text'], bordercolor=COLORS['border'], padding=7)
        style.map('TEntry', bordercolor=[('focus', COLORS['accent2'])])
        style.configure('TCombobox', fieldbackground=COLORS['surface2'],
                        background=COLORS['surface2'], foreground=COLORS['text'], padding=5)
        style.map('TCombobox', fieldbackground=[('readonly', COLORS['surface2'])],
                  foreground=[('readonly', COLORS['text'])])
        style.configure('Treeview', background=COLORS['surface2'],
                        fieldbackground=COLORS['surface2'], foreground=COLORS['text'],
                        bordercolor=COLORS['border'], rowheight=30)
        style.configure('Treeview.Heading', background=COLORS['surface'],
                        foreground=COLORS['muted'], font=('Segoe UI Semibold', 9))
        style.map('Treeview', background=[('selected', COLORS['accent2'])],
                  foreground=[('selected', '#ffffff')])
        style.configure('TButton', background=COLORS['surface2'], foreground=COLORS['text'],
                        padding=(12, 7), bordercolor=COLORS['border'])
        style.map('TButton', background=[('active', COLORS['border'])])
        style.configure('Accent.TButton', background=COLORS['accent2'], foreground='white',
                        padding=(18, 9), font=('Segoe UI Semibold', 10))
        style.map('Accent.TButton', background=[('active', COLORS['accent'])],
                  foreground=[('active', '#07111f')])
        style.configure('Danger.TButton', background='#3a1c2a', foreground='#fecdd3')
        style.configure('TCheckbutton', background=COLORS['surface'], foreground=COLORS['text'],
                        indicatorbackground=COLORS['surface2'], indicatorforeground=COLORS['accent'])
        style.map('TCheckbutton', background=[('active', COLORS['surface'])])
        style.configure('TRadiobutton', background=COLORS['surface'], foreground=COLORS['text'],
                        indicatorbackground=COLORS['surface2'], indicatorforeground=COLORS['accent'])
        style.map('TRadiobutton', background=[('active', COLORS['surface'])])
        style.configure('Vertical.TScrollbar', background=COLORS['surface2'],
                        troughcolor=COLORS['window'], arrowcolor=COLORS['muted'])
        style.configure('Activity.TNotebook', background=COLORS['window'],
                        bordercolor=COLORS['border'], lightcolor=COLORS['border'],
                        darkcolor=COLORS['border'], tabmargins=(0, 0, 0, 0))
        style.configure('Activity.TNotebook.Tab', background=COLORS['surface2'],
                        foreground=COLORS['muted'], bordercolor=COLORS['border'],
                        lightcolor=COLORS['border'], darkcolor=COLORS['border'],
                        padding=(13, 6), font=('Segoe UI Semibold', 9))
        style.map('Activity.TNotebook.Tab',
                  background=[('selected', COLORS['surface']),
                              ('active', COLORS['border'])],
                  foreground=[('selected', COLORS['accent']),
                              ('active', COLORS['text'])],
                  padding=[('selected', (20, 10)), ('!selected', (13, 6))],
                  font=[('selected', ('Segoe UI Semibold', 10)),
                        ('!selected', ('Segoe UI Semibold', 9))])

    def _build_plugin_symbols(self, parent) -> None:
        """Render declarative contributions returned by DLL plugins."""
        if not self.plugin_symbols and not self.plugin_manager.errors:
            return
        holder = tk.Frame(parent, bg=COLORS['window'])
        holder.pack(side=tk.RIGHT, padx=(0, 15), pady=2)
        for symbol in self.plugin_symbols:
            label = tk.Label(
                holder, text=symbol['text'], bg=COLORS['surface2'],
                fg=symbol['foreground'], activebackground=COLORS['border'],
                activeforeground=symbol['foreground'], cursor='hand2',
                font=('Segoe UI Symbol', 15, 'bold'), padx=9, pady=3)
            label.pack(side=tk.LEFT, padx=3)
            label.bind('<Button-1>', lambda _event, value=symbol:
                       messagebox.showinfo(
                           value['plugin_name'],
                           f"{value['plugin_name']} {value['plugin_version']}\n\n"
                           f"{value['tooltip']}"))
        if self.plugin_manager.errors:
            warning = tk.Label(
                holder, text='!', bg='#3a1c2a', fg=COLORS['danger'],
                cursor='hand2', font=('Segoe UI', 12, 'bold'), padx=9, pady=3)
            warning.pack(side=tk.LEFT, padx=3)
            warning.bind('<Button-1>', lambda _event: messagebox.showwarning(
                'DLL plugins', '\n\n'.join(
                    f"{error['path']}\n{error['message']}"
                    for error in self.plugin_manager.errors)))

    def _build_ui(self) -> None:
        self.root.title(APP_NAME)
        self.root.geometry('1180x820')
        self.root.minsize(980, 680)
        try:
            self.root.iconbitmap(default=str(self._asset('app-icon.ico')))
        except tk.TclError:
            pass

        self.shell = tk.Frame(self.root, bg=COLORS['window'])
        self.shell.pack(fill=tk.BOTH, expand=True)
        sidebar = tk.Frame(self.shell, bg=COLORS['sidebar'], width=218)
        sidebar.pack(side=tk.LEFT, fill=tk.Y)
        sidebar.pack_propagate(False)
        brand = tk.Frame(sidebar, bg=COLORS['sidebar'])
        brand.pack(fill=tk.X, padx=18, pady=(20, 24))
        try:
            self.logo_icon = tk.PhotoImage(file=str(self._asset('app-icon-48.png')))
            tk.Label(brand, image=self.logo_icon, bg=COLORS['sidebar']).pack(side=tk.LEFT)
        except tk.TclError:
            self.logo_icon = None
        brand_text = tk.Frame(brand, bg=COLORS['sidebar'])
        brand_text.pack(side=tk.LEFT, padx=(10, 0))
        tk.Label(brand_text, text='Universal SCS', bg=COLORS['sidebar'], fg=COLORS['text'],
                 font=('Segoe UI Semibold', 11)).pack(anchor=tk.W)
        tk.Label(brand_text, text='Extractor', bg=COLORS['sidebar'], fg=COLORS['accent'],
                 font=('Segoe UI Semibold', 11)).pack(anchor=tk.W)

        self.nav_buttons = {}
        for key, glyph in (('main', '▣'), ('options', '⚙'), ('settings', '☰'),
                           ('output', '›_'), ('help', '?')):
            button = tk.Button(
                sidebar, text=f'  {glyph}   {self.t(key)}', anchor=tk.W,
                bg=COLORS['sidebar'], fg=COLORS['muted'], activebackground=COLORS['surface2'],
                activeforeground=COLORS['text'], relief=tk.FLAT, bd=0,
                font=('Segoe UI Semibold', 10), padx=16, pady=11,
                command=lambda page=key: self.show_page(page))
            button.pack(fill=tk.X, padx=10, pady=2)
            self.nav_buttons[key] = button
        plugin_button = tk.Button(
            sidebar, text=f'  \u25c6   {self.t("plugins")}', anchor=tk.W,
            bg=COLORS['sidebar'], fg=COLORS['muted'],
            activebackground=COLORS['surface2'], activeforeground=COLORS['text'],
            relief=tk.FLAT, bd=0, font=('Segoe UI Semibold', 10), padx=16, pady=11,
            command=lambda: self.show_page('plugins'))
        plugin_button.pack(fill=tk.X, padx=10, pady=2,
                           before=self.nav_buttons['output'])
        self.nav_buttons['plugins'] = plugin_button
        tk.Label(sidebar, text='v1 • Python', bg=COLORS['sidebar'], fg='#56667e',
                 font=('Segoe UI', 9)).pack(side=tk.BOTTOM, pady=18)

        right = ttk.Frame(self.shell, style='Content.TFrame', padding=(22, 17, 22, 14))
        right.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        top = ttk.Frame(right, style='Content.TFrame')
        top.pack(fill=tk.X, pady=(0, 13))
        title_box = ttk.Frame(top, style='Content.TFrame')
        title_box.pack(side=tk.LEFT)
        self.page_title = ttk.Label(title_box, style='Title.TLabel')
        self.page_title.pack(anchor=tk.W)
        self.page_subtitle = ttk.Label(title_box, style='Muted.TLabel')
        self.page_subtitle.pack(anchor=tk.W, pady=(2, 0))
        language = ttk.Frame(top, style='Content.TFrame')
        language.pack(side=tk.RIGHT, pady=4)
        ttk.Label(language, text=self.t('language'), style='Muted.TLabel').pack(
            side=tk.LEFT, padx=(0, 7))
        self.top_language_combo = ttk.Combobox(
            language, textvariable=self.language_choice,
            values=('Русский', 'English'), state='readonly', width=10,
            takefocus=False)
        self.top_language_combo.pack(side=tk.LEFT)
        self.top_language_combo.bind('<<ComboboxSelected>>', self.change_language)
        self.top_language_combo.bind('<MouseWheel>', lambda _event: 'break')
        self._build_plugin_symbols(top)

        self.page_host = ttk.Frame(right, style='Content.TFrame')
        self.page_host.pack(fill=tk.BOTH, expand=True)
        self.pages = {}
        for key in ('main', 'options', 'settings', 'plugins', 'output', 'help'):
            page = ttk.Frame(self.page_host, style='Content.TFrame')
            self.pages[key] = page
        self._build_main_tab(self.pages['main'])
        self._build_options_tab(self.pages['options'])
        self._build_settings_tab(self.pages['settings'])
        self._build_plugins_tab(self.pages['plugins'])
        self._build_output_tab(self.pages['output'])
        self._build_help_tab(self.pages['help'])
        self.notebook = _PageRouter(self)

        preview = ttk.Frame(right, style='Card.TFrame', padding=10)
        preview.pack(fill=tk.X, pady=(12, 8))
        ttk.Label(preview, text=self.t('command'), style='CardMuted.TLabel').pack(anchor=tk.W)
        row = ttk.Frame(preview, style='Card.TFrame')
        row.pack(fill=tk.X, pady=(5, 0))
        ttk.Entry(row, textvariable=self.command_preview, state='readonly').pack(
            side=tk.LEFT, fill=tk.X, expand=True)
        ttk.Button(row, text=self.t('copy'), command=self.copy_command).pack(side=tk.LEFT, padx=(8, 0))

        footer = ttk.Frame(right, style='Content.TFrame')
        footer.pack(fill=tk.X)
        ttk.Label(footer, textvariable=self.status, style='Muted.TLabel').pack(side=tk.LEFT)
        ttk.Button(footer, text=self.t('open_result'), command=self.open_destination).pack(side=tk.RIGHT)
        self.stop_button = ttk.Button(footer, text=self.t('stop'), style='Danger.TButton',
                                      command=self.stop,
                                      state=tk.NORMAL if self.process else tk.DISABLED)
        self.stop_button.pack(side=tk.RIGHT, padx=8)
        self.run_button = ttk.Button(footer, text='▶  ' + self.t('run'), style='Accent.TButton',
                                     command=self.run,
                                     state=tk.DISABLED if self.process else tk.NORMAL)
        self.run_button.pack(side=tk.RIGHT)
        self.new_tab_run_button = ttk.Button(
            footer, text=self.t('run_new_tab'), command=self.run_in_new_activity_tab,
            state=tk.DISABLED if self.process else tk.NORMAL)
        self.new_tab_run_button.pack(side=tk.RIGHT, padx=(0, 8))
        self.show_page(self.current_page)
        self._update_process_controls()

    def _card(self, parent, title: str, **pack):
        card = ttk.LabelFrame(parent, text=title, style='Card.TLabelframe', padding=12)
        card.pack(**pack)
        return card

    def _build_main_tab(self, parent: ttk.Frame) -> None:
        archives = self._card(parent, self.t('inputs'), fill=tk.BOTH, expand=True)
        list_frame = ttk.Frame(archives, style='Card.TFrame')
        list_frame.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        self.paths = tk.Listbox(
            list_frame, selectmode=tk.EXTENDED, height=8, activestyle='none',
            bg=COLORS['surface2'], fg=COLORS['text'], selectbackground=COLORS['accent2'],
            selectforeground='white', relief=tk.FLAT, bd=0, highlightthickness=1,
            highlightbackground=COLORS['border'], font=('Segoe UI', 10))
        scrollbar = ttk.Scrollbar(list_frame, command=self.paths.yview)
        self.paths.configure(yscrollcommand=scrollbar.set)
        self.paths.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        scrollbar.pack(side=tk.RIGHT, fill=tk.Y)
        self.paths.bind('<<ListboxSelect>>', lambda _event: self.refresh_command())
        actions = ttk.Frame(archives, style='Card.TFrame')
        actions.pack(side=tk.LEFT, fill=tk.Y, padx=(10, 0))
        for label, command in ((self.t('add_archives'), self.add_archives),
                               (self.t('add_folder'), self.add_folder),
                               (self.t('remove'), self.remove_paths),
                               (self.t('clear'), self.clear_paths)):
            ttk.Button(actions, text=label, command=command).pack(fill=tk.X, pady=(0, 7))

        destination = self._card(parent, self.t('destination'), fill=tk.X, pady=(10, 0))
        ttk.Entry(destination, textvariable=self.destination).pack(side=tk.LEFT, fill=tk.X, expand=True)
        ttk.Button(destination, text=self.t('browse'), command=self.choose_destination).pack(
            side=tk.LEFT, padx=(8, 0))

        lower = ttk.Frame(parent, style='Content.TFrame')
        lower.pack(fill=tk.X, pady=(10, 0))
        quick = self._card(lower, self.t('quick'), side=tk.LEFT, fill=tk.BOTH, expand=True)
        ttk.Checkbutton(quick, text=self.t('deep') + '  (--deep)', variable=self.deep).pack(anchor=tk.W, pady=3)
        ttk.Checkbutton(quick, text=self.t('separate') + '  (--separate)', variable=self.separate).pack(anchor=tk.W, pady=3)
        self._main_mode_box = self._card(
            lower, self.t('mode'), side=tk.LEFT, fill=tk.BOTH,
            expand=True, padx=(10, 0))
        self._main_mode_box_pack = {
            'side': tk.LEFT, 'fill': tk.BOTH, 'expand': True, 'padx': (10, 0)}
        modes = (('extract', 'extract'), ('list', 'list'), ('list_all', 'list_all'),
                 ('list_entries', 'list_entries'), ('tree', 'tree'))
        for index, (label, value) in enumerate(modes):
            ttk.Radiobutton(self._main_mode_box, text=self.t(label), value=value,
                            variable=self.mode).grid(
                row=index // 3, column=index % 3, sticky=tk.W, padx=(0, 14), pady=3)
        self._update_operation_mode_visibility()

    def _field(self, parent, row: int, label: str, variable: tk.StringVar,
               browse=None, hint: str | None = None) -> ttk.Entry:
        ttk.Label(parent, text=label, style='Card.TLabel').grid(
            row=row, column=0, sticky=tk.W, padx=(0, 10), pady=5)
        field = ttk.Frame(parent, style='Card.TFrame')
        field.grid(row=row, column=1, sticky=tk.EW, pady=5)
        entry = ttk.Entry(field, textvariable=variable)
        entry.pack(side=tk.LEFT, fill=tk.X, expand=True)
        ttk.Button(
            field, text='\N{MULTIPLICATION SIGN}', width=2,
            command=lambda value=variable: value.set('')).pack(
                side=tk.LEFT, padx=(5, 0))
        if browse:
            ttk.Button(parent, text=self.t('browse'), command=browse).grid(
                row=row, column=2, padx=(8, 0), pady=5)
        return entry

    def _build_options_tab(self, parent: ttk.Frame) -> None:
        canvas = tk.Canvas(
            parent, bg=COLORS['window'], highlightthickness=0, bd=0)
        scrollbar = ttk.Scrollbar(
            parent, orient=tk.VERTICAL, command=canvas.yview)
        canvas.configure(yscrollcommand=scrollbar.set)
        canvas.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        scrollbar.pack(side=tk.RIGHT, fill=tk.Y, padx=(8, 0))
        content = ttk.Frame(canvas, style='Content.TFrame')
        content_window = canvas.create_window((0, 0), window=content, anchor=tk.NW)

        def update_scroll_region(_event=None) -> None:
            canvas.configure(scrollregion=canvas.bbox('all'))

        def resize_content(event) -> None:
            canvas.itemconfigure(content_window, width=event.width)

        def scroll_options(event) -> str:
            delta = getattr(event, 'delta', 0)
            if delta:
                canvas.yview_scroll(-1 if delta > 0 else 1, 'units')
            elif getattr(event, 'num', 0) in (4, 5):
                canvas.yview_scroll(-1 if event.num == 4 else 1, 'units')
            return 'break'

        def bind_wheel(widget) -> None:
            widget.bind('<MouseWheel>', scroll_options, add='+')
            widget.bind('<Button-4>', scroll_options, add='+')
            widget.bind('<Button-5>', scroll_options, add='+')
            for child in widget.winfo_children():
                bind_wheel(child)

        content.bind('<Configure>', update_scroll_region)
        canvas.bind('<Configure>', resize_content)

        files = self._card(content, self.t('paths_filter'), fill=tk.X)
        files.columnconfigure(1, weight=1)
        self._field(files, 0, self.t('additional') + '  (--additional)', self.additional,
                    lambda: self.choose_file(self.additional))
        self._field(files, 1, self.t('reference') + '  (-ref, --reference)', self.reference,
                    self.choose_reference_files)
        self._field(files, 2, self.t('path_file') + '  (--paths)', self.path_file,
                    lambda: self.choose_file(self.path_file))
        self._field(files, 3, self.t('partial') + '  (--partial)', self.partial)
        self._field(files, 4, self.t('filter') + '  (--filter)', self.filter_text)
        self._field(files, 5, self.t('list_output') + '  (--list-output)', self.list_output,
                    lambda: self.choose_save_file(self.list_output))
        archive_tools = self._card(
            content, self.t('archive_tools'), fill=tk.X, pady=(10, 0))
        archive_tools.columnconfigure(1, weight=1)
        ttk.Checkbutton(
            archive_tools, text=self.t('edit_manifest') + '  (--manifest)',
            variable=self.edit_manifest).grid(
                row=0, column=0, sticky=tk.W, padx=(0, 18), pady=5)
        ttk.Checkbutton(
            archive_tools, text=self.t('compress_v1') + '  (--compress)',
            variable=self.compress_v1).grid(
                row=0, column=1, sticky=tk.W, pady=5)
        ttk.Checkbutton(
            archive_tools, text=self.t('compress_no_compact'),
            variable=self.compress_no_compact).grid(
                row=0, column=2, sticky=tk.W, padx=(18, 0), pady=5)
        replacement_header = ttk.Frame(archive_tools, style='Card.TFrame')
        replacement_header.grid(row=1, column=0, columnspan=3, sticky=tk.EW,
                                pady=(8, 4))
        ttk.Label(
            replacement_header,
            text=self.t('manifest_replace') + '  (--manifest-replace)',
            style='Card.TLabel').pack(side=tk.LEFT)
        ttk.Button(
            replacement_header, text=self.t('manifest_replace_add'),
            command=self._add_manifest_replacement).pack(side=tk.RIGHT)
        self.manifest_replacements_holder = ttk.Frame(
            archive_tools, style='Card.TFrame')
        self.manifest_replacements_holder.grid(
            row=2, column=0, columnspan=3, sticky=tk.EW)
        self._render_manifest_replacements()
        log = self._card(content, self.t('logging'), fill=tk.X, pady=(10, 0))
        ttk.Checkbutton(log, text=self.t('enable_log') + '  (--log)',
                        variable=self.log_enabled).pack(side=tk.LEFT)
        log_field = ttk.Frame(log, style='Card.TFrame')
        log_field.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=9)
        ttk.Entry(log_field, textvariable=self.log_file).pack(
            side=tk.LEFT, fill=tk.X, expand=True)
        ttk.Button(
            log_field, text='\N{MULTIPLICATION SIGN}', width=2,
            command=lambda: self.log_file.set('')).pack(side=tk.LEFT, padx=(5, 0))
        ttk.Button(log, text=self.t('browse'),
                   command=lambda: self.choose_save_file(self.log_file)).pack(side=tk.LEFT)
        flags = self._card(
            content, self.t('switches'), fill=tk.BOTH, expand=True, pady=(10, 0))
        checks = ((self.t('all') + '  (--all)', self.scan_folders),
                  (self.t('dry_run') + '  (--dry-run)', self.dry_run),
                  (self.t('quiet') + '  (--quiet)', self.quiet),
                  (self.t('skip_existing') + '  (--skip-existing)', self.skip_existing),
                  (self.t('raw') + '  (--raw)', self.raw),
                  (self.t('no_update') + '  (--no-update)', self.no_update),
                  (self.t('force_extract') + '  (--force-extract)', self.force_extract),
                  (self.t('table_at_end') + '  (--table-at-end)', self.table_at_end))
        for index, (label, variable) in enumerate(checks):
            ttk.Checkbutton(flags, text=label, variable=variable).grid(
                row=index // 2, column=index % 2, sticky=tk.W, padx=(0, 25), pady=5)
        ttk.Label(flags, text=self.t('salt') + '  (--salt)', style='Card.TLabel').grid(
            row=4, column=0, sticky=tk.W, pady=5)
        salt_field = ttk.Frame(flags, style='Card.TFrame')
        salt_field.grid(row=4, column=1, sticky=tk.W)
        ttk.Entry(salt_field, textvariable=self.salt, width=11).pack(side=tk.LEFT)
        ttk.Button(
            salt_field, text='\N{MULTIPLICATION SIGN}', width=2,
            command=lambda: self.salt.set('')).pack(side=tk.LEFT, padx=(5, 0))
        bind_wheel(content)
        update_scroll_region()

    def _build_settings_tab(self, parent: ttk.Frame) -> None:
        content = ttk.Frame(parent, style='Content.TFrame')
        content.pack(fill=tk.BOTH, expand=True)

        performance = self._card(content, self.t('performance'), fill=tk.X)
        performance.columnconfigure(1, weight=1)
        ttk.Label(performance, text=self.t('max_threads'),
                  style='Card.TLabel').grid(row=0, column=0, sticky=tk.W, pady=5)
        threads = ttk.Spinbox(performance, from_=1, to=256,
                              textvariable=self.max_threads, width=12)
        threads.grid(row=0, column=1, sticky=tk.W, padx=(18, 0), pady=5)
        threads.bind('<MouseWheel>', lambda _event: 'break')

        defaults = self._card(content, self.t('defaults'), fill=tk.X, pady=(10, 0))
        ttk.Checkbutton(
            defaults, text=self.t('always_deep'), variable=self.always_deep,
            command=self._apply_default_options).pack(anchor=tk.W, pady=3)
        ttk.Checkbutton(
            defaults, text=self.t('always_log'), variable=self.always_log,
            command=self._apply_default_options).pack(anchor=tk.W, pady=3)
        log_path = ttk.Frame(defaults, style='Card.TFrame')
        log_path.pack(fill=tk.X, pady=(8, 0))
        ttk.Label(log_path, text=self.t('default_log_path'),
                  style='Card.TLabel').pack(side=tk.LEFT, padx=(0, 12))
        log_entry = ttk.Entry(log_path, textvariable=self.log_file)
        log_entry.pack(side=tk.LEFT, fill=tk.X, expand=True)
        ttk.Button(
            log_path, text='\N{MULTIPLICATION SIGN}', width=2,
            command=lambda: self.log_file.set('')).pack(side=tk.LEFT, padx=(5, 0))
        ttk.Button(log_path, text=self.t('browse'),
                   command=lambda: self.choose_save_file(self.log_file)).pack(
                       side=tk.LEFT, padx=(8, 0))

        interface = self._card(content, self.t('language'), fill=tk.X, pady=(10, 0))
        self.settings_language_combo = ttk.Combobox(
            interface, textvariable=self.language_choice,
            values=('Русский', 'English'), state='readonly', width=18,
            takefocus=False)
        self.settings_language_combo.pack(anchor=tk.W)
        self.settings_language_combo.bind('<<ComboboxSelected>>', self.change_language)
        self.settings_language_combo.bind('<MouseWheel>', lambda _event: 'break')

        community = self._card(content, self.t('community'), fill=tk.X, pady=(10, 0))
        ttk.Checkbutton(community, text=self.t('community_share'),
                        variable=self.community_share).pack(anchor=tk.W, pady=3)
        ttk.Checkbutton(community, text=self.t('community_use'),
                        variable=self.community_use).pack(anchor=tk.W, pady=3)
        if not has_embedded_defaults():
            ttk.Button(community, text=self.t('community_setup'),
                       command=self.configure_community).pack(anchor=tk.W, pady=(6, 0))
        ttk.Button(content, text=self.t('save_settings'), style='Accent.TButton',
                   command=lambda: self._save_app_preferences(True)).pack(
                       anchor=tk.E, pady=(12, 0))

    def _add_manifest_replacement(self) -> None:
        old_value = tk.StringVar(master=self.root)
        new_value = tk.StringVar(master=self.root)
        old_value.trace_add('write', lambda *_args: self.refresh_command())
        new_value.trace_add('write', lambda *_args: self.refresh_command())
        self.manifest_replacements.append((old_value, new_value))
        # A replacement is itself a manifest editing operation.  Reflect that
        # implication in the visible checkbox as well as in the CLI parser.
        self.edit_manifest.set(True)
        self._render_manifest_replacements()
        self.refresh_command()

    def _remove_manifest_replacement(self, index: int) -> None:
        if 0 <= index < len(self.manifest_replacements):
            del self.manifest_replacements[index]
            self._render_manifest_replacements()
            self.refresh_command()

    def _render_manifest_replacements(self) -> None:
        holder = self.manifest_replacements_holder
        for child in holder.winfo_children():
            child.destroy()
        if not self.manifest_replacements:
            ttk.Label(holder, text=self.t('manifest_replace_empty'),
                      style='CardMuted.TLabel').pack(anchor=tk.W, pady=(2, 5))
            return

        headings = ttk.Frame(holder, style='Card.TFrame')
        headings.pack(fill=tk.X, pady=(0, 3))
        headings.columnconfigure(0, weight=1)
        headings.columnconfigure(1, weight=1)
        ttk.Label(headings, text=self.t('manifest_replace_from'),
                  style='CardMuted.TLabel').grid(row=0, column=0, sticky=tk.W)
        ttk.Label(headings, text=self.t('manifest_replace_to'),
                  style='CardMuted.TLabel').grid(row=0, column=1, sticky=tk.W,
                                                padx=(9, 0))

        for index, (old_value, new_value) in enumerate(self.manifest_replacements):
            row = tk.Frame(holder, bg=COLORS['surface2'], highlightthickness=1,
                           highlightbackground=COLORS['border'])
            row.pack(fill=tk.X, pady=3)
            row.columnconfigure(0, weight=1)
            row.columnconfigure(1, weight=1)
            old_entry = ttk.Entry(row, textvariable=old_value)
            old_entry.grid(row=0, column=0, sticky=tk.EW, padx=(7, 4), pady=6)
            ttk.Entry(row, textvariable=new_value).grid(
                row=0, column=1, sticky=tk.EW, padx=4, pady=6)
            tk.Button(
                row, text='×', command=lambda item=index:
                self._remove_manifest_replacement(item),
                bg=COLORS['surface2'], fg=COLORS['danger'],
                activebackground='#3a1c2a', activeforeground='#fecdd3',
                relief=tk.FLAT, bd=0, cursor='hand2',
                font=('Segoe UI', 12, 'bold'), padx=9).grid(
                    row=0, column=2, padx=(2, 5), pady=4)
            if index == len(self.manifest_replacements) - 1:
                old_entry.focus_set()

    def _build_plugins_tab(self, parent: ttk.Frame) -> None:
        card = self._card(parent, self.t('plugin_available'), fill=tk.BOTH, expand=True)
        columns = ('status', 'version', 'type')
        self.plugin_tree = ttk.Treeview(card, columns=columns, show='tree headings', height=13)
        self.plugin_tree.heading('#0', text=self.t('plugins'))
        self.plugin_tree.heading('status', text=self.t('plugin_status'))
        self.plugin_tree.heading('version', text=self.t('plugin_version'))
        self.plugin_tree.heading('type', text=self.t('plugin_type'))
        self.plugin_tree.column('#0', width=330, stretch=True)
        self.plugin_tree.column('status', width=130, anchor=tk.CENTER)
        self.plugin_tree.column('version', width=100, anchor=tk.CENTER)
        self.plugin_tree.column('type', width=80, anchor=tk.CENTER)
        self.plugin_tree.tag_configure('plugin_active', foreground='#4ade80')
        self.plugin_tree.tag_configure('plugin_inactive', foreground=COLORS['danger'])
        scrollbar = ttk.Scrollbar(card, orient=tk.VERTICAL, command=self.plugin_tree.yview)
        self.plugin_tree.configure(yscrollcommand=scrollbar.set)
        self.plugin_tree.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        scrollbar.pack(side=tk.LEFT, fill=tk.Y)
        self.plugin_tree.bind('<Double-1>', lambda _event: self._open_plugin_details())

        buttons = ttk.Frame(card, style='Card.TFrame')
        buttons.pack(side=tk.LEFT, fill=tk.Y, padx=(12, 0))
        ttk.Button(buttons, text=self.t('plugin_details'),
                   command=self._open_plugin_details).pack(fill=tk.X, pady=(0, 6))
        ttk.Button(buttons, text=self.t('plugin_activate'),
                   command=lambda: self._set_selected_plugin_active(True)).pack(fill=tk.X, pady=6)
        ttk.Button(buttons, text=self.t('plugin_deactivate'),
                   command=lambda: self._set_selected_plugin_active(False)).pack(fill=tk.X, pady=6)
        ttk.Button(buttons, text=self.t('plugin_load'),
                   command=self._load_plugin_files).pack(fill=tk.X, pady=6)
        ttk.Button(buttons, text=self.t('plugin_delete'), style='Danger.TButton',
                   command=self._delete_selected_plugin).pack(fill=tk.X, pady=6)
        self._populate_plugin_tree()

    def _populate_plugin_tree(self) -> None:
        self._plugin_rows = {}
        for item in self.plugin_tree.get_children():
            self.plugin_tree.delete(item)
        for index, plugin in enumerate(self.plugin_manager.plugins):
            active = self.plugin_manager.is_active(plugin)
            row = f'plugin-{index}'
            self._plugin_rows[row] = plugin
            self.plugin_tree.insert(
                '', tk.END, iid=row, text=plugin.info['name'], values=(
                    self.t('plugin_active') if active else self.t('plugin_inactive'),
                    plugin.info['version'], plugin.kind.upper()),
                tags=('plugin_active' if active else 'plugin_inactive',))

    def _selected_plugin(self):
        selection = self.plugin_tree.selection()
        return self._plugin_rows.get(selection[0]) if selection else None

    def _rebuild_after_plugin_change(self) -> None:
        paths = self.archive_paths()
        self.plugin_manager = PluginManager(_application_directory())
        self.plugin_symbols = gui_symbols(self.plugin_manager.execute(
            'gui.initialize', {'language': self.language_code, 'application': APP_NAME}))
        self.shell.destroy()
        self._build_ui()
        for path in paths:
            self.paths.insert(tk.END, path)
        self.current_page = 'plugins'
        self.show_page('plugins')
        self.refresh_command()

    def _set_selected_plugin_active(self, active: bool) -> None:
        plugin = self._selected_plugin()
        if plugin is None:
            return
        self.plugin_manager.set_active(plugin.info['id'], active)
        self._rebuild_after_plugin_change()

    def _load_plugin_files(self) -> None:
        filenames = filedialog.askopenfilenames(
            title=self.t('plugin_load'), initialdir=str(_application_directory()),
            filetypes=(('Plugins', '*.pyd *.dll'), (self.t('all_files'), '*.*')))
        if not filenames:
            return
        try:
            self.plugin_manager.install(list(filenames))
        except (OSError, ValueError) as exc:
            messagebox.showerror(APP_NAME, str(exc))
            return
        self._rebuild_after_plugin_change()

    def _delete_selected_plugin(self) -> None:
        plugin = self._selected_plugin()
        if plugin is None or not messagebox.askyesno(
                APP_NAME, self.t('plugin_delete_question').format(name=plugin.info['name'])):
            return
        try:
            deleted = self.plugin_manager.delete(plugin)
        except OSError as exc:
            messagebox.showerror(
                APP_NAME, self.t('plugin_delete_error').format(error=exc))
            return
        if not deleted:
            messagebox.showinfo(APP_NAME, self.t('plugin_delete_pending'))
        self._rebuild_after_plugin_change()

    def _open_plugin_details(self) -> None:
        plugin = self._selected_plugin()
        if plugin is None:
            return
        window = tk.Toplevel(self.root)
        window.title(plugin.info['name'])
        window.configure(bg=COLORS['window'])
        window.resizable(False, False)
        body = ttk.Frame(window, style='Content.TFrame', padding=18)
        body.pack(fill=tk.BOTH, expand=True)
        ttk.Label(body, text=plugin.info['name'], style='HelpTitle.TLabel').grid(
            row=0, column=0, columnspan=2, sticky=tk.W, pady=(0, 10))
        details = (
            (self.t('plugin_version'), plugin.info['version']),
            (self.t('plugin_author'), plugin.info.get('author', '—')),
            (self.t('plugin_type'), plugin.kind.upper()),
            (self.t('plugin_path'), str(plugin.path)),
        )
        row = 1
        for label, value in details:
            ttk.Label(body, text=label + ':', style='Muted.TLabel').grid(
                row=row, column=0, sticky=tk.NW, padx=(0, 12), pady=3)
            ttk.Label(body, text=value, wraplength=520).grid(row=row, column=1, sticky=tk.W, pady=3)
            row += 1
        description = str(plugin.info.get('description', ''))
        if description:
            ttk.Label(body, text=description, wraplength=620).grid(
                row=row, column=0, columnspan=2, sticky=tk.W, pady=(10, 8))
            row += 1

        values = self.plugin_manager.settings_for(plugin)
        variables = {}
        for key, definition in plugin.info.get('settings', {}).items():
            if not isinstance(definition, dict):
                continue
            label = str(definition.get('name') or key)
            setting_type = str(definition.get('type') or 'string')
            if setting_type == 'boolean':
                variable = tk.BooleanVar(window, value=bool(values.get(key)))
                ttk.Checkbutton(body, text=label, variable=variable).grid(
                    row=row, column=0, columnspan=2, sticky=tk.W, pady=4)
            else:
                variable = tk.StringVar(window, value=str(values.get(key, '')))
                ttk.Label(body, text=label + ':', style='Muted.TLabel').grid(
                    row=row, column=0, sticky=tk.W, padx=(0, 12), pady=4)
                choices = definition.get('choices')
                if isinstance(choices, list):
                    widget = ttk.Combobox(body, textvariable=variable,
                                          values=[str(value) for value in choices],
                                          state='readonly')
                else:
                    widget = ttk.Entry(body, textvariable=variable, width=34)
                widget.grid(row=row, column=1, sticky=tk.EW, pady=4)
            variables[key] = (variable, setting_type)
            row += 1

        def save_settings() -> None:
            converted = {}
            for key, (variable, setting_type) in variables.items():
                value = variable.get()
                try:
                    if setting_type == 'integer':
                        value = int(value)
                    elif setting_type == 'number':
                        value = float(value)
                except ValueError:
                    messagebox.showerror(APP_NAME, f'{key}: {value}')
                    return
                converted[key] = value
            self.plugin_manager.set_settings(plugin, converted)
            window.destroy()
            self._rebuild_after_plugin_change()

        ttk.Button(body, text=self.t('plugin_save'), style='Accent.TButton',
                   command=save_settings).grid(row=row, column=0, columnspan=2,
                                               sticky=tk.E, pady=(14, 0))
        body.columnconfigure(1, weight=1)

    def _build_output_tab(self, parent: ttk.Frame) -> None:
        toolbar = ttk.Frame(parent, style='Content.TFrame')
        toolbar.pack(fill=tk.X, pady=(0, 8))
        ttk.Label(toolbar, text=self.t('console_hint'), style='Muted.TLabel').pack(side=tk.LEFT)
        ttk.Button(toolbar, text=self.t('clear_output'), command=self.clear_output).pack(side=tk.RIGHT)
        ttk.Button(toolbar, text=self.t('close_tab'),
                   command=self.close_activity_tab).pack(side=tk.RIGHT, padx=(0, 8))
        ttk.Button(toolbar, text=self.t('copy_output'),
                   command=lambda: self._copy_text_widget(self.output, all_text=True)).pack(
                       side=tk.RIGHT, padx=(0, 8))
        self.activity_notebook = ttk.Notebook(parent, style='Activity.TNotebook')
        self.activity_notebook.pack(fill=tk.BOTH, expand=True)
        self.activity_notebook.bind('<<NotebookTabChanged>>', self._activity_tab_changed)
        self.output_widgets: list[tk.Text] = []
        for index in range(len(self._activity_tabs)):
            self._create_activity_widget(index)
        visible = [index for index, model in enumerate(self._activity_tabs)
                   if not model.get('closed', False)]
        self._active_activity_index = min(
            self._active_activity_index, len(self.output_widgets) - 1)
        if self._active_activity_index not in visible:
            self._active_activity_index = visible[-1]
        self.activity_notebook.select(self._active_activity_index)
        self.output = self.output_widgets[self._active_activity_index]
        self._restore_activity_output()

    def _create_activity_widget(self, index: int) -> tk.Text:
        frame = tk.Frame(self.activity_notebook, bg=COLORS['border'], padx=1, pady=1)
        self.activity_notebook.add(
            frame, text=self.t('log_tab').format(number=index + 1))
        output = tk.Text(frame, wrap=tk.NONE, bg=COLORS['surface'], fg=COLORS['text'],
                         insertbackground=COLORS['accent'],
                         font=('Segoe UI Semibold', 10),
                         selectbackground='#164e63', selectforeground='#ffffff',
                         state=tk.NORMAL, relief=tk.FLAT, padx=14, pady=12)
        activity_colors = {
            'info': '#67e8f9', 'debug': '#60a5fa', 'warn': '#fbbf24',
            'error': '#fb7185', 'success': '#4ade80',
        }
        for level, color in activity_colors.items():
            output.tag_configure(
                f'activity_{level}', foreground=color,
                font=('Segoe UI Semibold', 10),
                spacing1=3, spacing3=3)
        output.tag_configure(
            'activity_summary', foreground=activity_colors['success'],
            font=('Segoe UI Semibold', 10), spacing1=3, spacing3=3)
        output.tag_configure(
            'activity_view', foreground=activity_colors['success'],
            font=('Segoe UI Semibold', 10), spacing1=3, spacing3=3)
        output.tag_configure(
            # Keep branches aligned like a DOM tree, while retaining the
            # same green success colour as its collapsible header.
            'activity_view_tree', foreground=activity_colors['success'],
            font=('Cascadia Mono', 10), spacing1=1, spacing3=1)
        output.tag_configure(
            'activity_metric_success', foreground=activity_colors['success'],
            font=('Segoe UI Semibold', 10))
        output.tag_configure(
            'activity_metric_warn', foreground=activity_colors['warn'],
            font=('Segoe UI Semibold', 10))
        output.tag_configure(
            'activity_metric_error', foreground=activity_colors['error'],
            font=('Segoe UI Semibold', 10))
        output.tag_configure(
            'activity_total_summary', font=('Segoe UI Semibold', 11),
            spacing1=5, spacing3=5)
        def set_vertical(first: str, last: str) -> None:
            y_scroll.set(first, last)
            model = self._activity_tabs[index]
            if not model.get('updating_view', False):
                model['follow_tail'] = float(last) >= 0.999

        def scroll_vertical(*arguments) -> None:
            output.yview(*arguments)
            first, last = output.yview()
            self._activity_tabs[index]['follow_tail'] = last >= 0.999

        def jump_to_end(_event=None) -> str:
            self._activity_tabs[index]['follow_tail'] = True
            output.see(tk.END)
            return 'break'

        y_scroll = ttk.Scrollbar(frame, orient=tk.VERTICAL, command=scroll_vertical)
        x_scroll = ttk.Scrollbar(frame, orient=tk.HORIZONTAL, command=output.xview)
        output.configure(yscrollcommand=set_vertical, xscrollcommand=x_scroll.set)
        # A disabled Text widget does not dispatch tag mouse events. Keep the
        # journal interactive for fold arrows while blocking keyboard edits.
        output.bind('<KeyPress>', lambda _event: 'break')
        def click_fold(event):
            try:
                position = output.index(f'@{event.x},{event.y}')
            except tk.TclError:
                return
            for item in self._activity_tabs[index].get('groups', []):
                if not isinstance(item, dict):
                    continue
                if str(item['header_tag']) in output.tag_names(position):
                    return self._toggle_archive_group(index, item)
        output.bind('<Button-1>', click_fold, add='+')
        output.bind('<End>', jump_to_end)
        y_scroll.pack(side=tk.RIGHT, fill=tk.Y)
        x_scroll.pack(side=tk.BOTTOM, fill=tk.X)
        output.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        self._enable_text_copying(output)
        self.output_widgets.append(output)
        self._update_activity_tab_title(index)
        if self._activity_tabs[index].get('closed', False):
            self.activity_notebook.hide(frame)
        return output

    def _update_activity_tab_title(self, index: int) -> None:
        if index >= len(getattr(self, 'output_widgets', ())):
            return
        title = self.t('log_tab').format(number=index + 1)
        if index in self._processes:
            title += '   ' + self.t('tab_running')
        self.activity_notebook.tab(self.output_widgets[index].master, text=title)

    def _activity_tab_changed(self, _event=None) -> None:
        if not self.output_widgets:
            return
        try:
            self._active_activity_index = self.activity_notebook.index(
                self.activity_notebook.select())
        except tk.TclError:
            return
        self.output = self.output_widgets[self._active_activity_index]
        self._update_process_controls()

    def _create_activity_tab(self) -> None:
        self._activity_tabs.append({
            'records': [], 'fragment': '', 'follow_tail': True,
            'updating_view': False, 'closed': False,
            'announce_new_process': True, 'groups': [],
            'active_group': None, 'next_group_id': 0,
            'status_mark_created': False, 'aggregate': {},
            'total_rendered': False, 'operation_id': None,
            'render_operation_id': 0, 'show_extraction_total': False})
        index = len(self._activity_tabs) - 1
        self._create_activity_widget(index)
        self.activity_notebook.select(index)
        self._active_activity_index = index
        self.output = self.output_widgets[index]

    def close_activity_tab(self) -> None:
        index = self._active_activity_index
        process = self._processes.get(index)
        if process is not None:
            if not messagebox.askyesno(APP_NAME, self.t('stop_question')):
                return
            self._terminate_process(process)
        self._activity_tabs[index]['closed'] = True
        self.activity_notebook.hide(self.output_widgets[index].master)
        visible = [position for position, output in enumerate(self.output_widgets)
                   if self.activity_notebook.tab(output.master, 'state') != 'hidden']
        if not visible:
            self._create_activity_tab()
            return
        target = visible[-1]
        self.activity_notebook.select(self.output_widgets[target].master)
        self._active_activity_index = target
        self.output = self.output_widgets[target]
        self._update_process_controls()

    def _build_help_tab(self, parent: ttk.Frame) -> None:
        ttk.Label(parent, text=self.t('help_intro'), style='Muted.TLabel').pack(anchor=tk.W, pady=(0, 8))
        search = ttk.Entry(parent, textvariable=self.help_query)
        search.pack(fill=tk.X, pady=(0, 10))
        search.bind('<KeyRelease>', lambda _event: self._filter_help())
        body = ttk.Frame(parent, style='Content.TFrame')
        body.pack(fill=tk.BOTH, expand=True)
        self.help_list = tk.Listbox(
            body, width=29, bg=COLORS['surface'], fg=COLORS['text'],
            selectbackground=COLORS['accent2'], selectforeground='white', relief=tk.FLAT,
            highlightthickness=1, highlightbackground=COLORS['border'], font=('Cascadia Mono', 10))
        self.help_list.pack(side=tk.LEFT, fill=tk.Y)
        self.help_list.bind('<<ListboxSelect>>', self._show_help_item)
        detail = ttk.Frame(body, style='Card.TFrame', padding=18)
        detail.pack(side=tk.LEFT, fill=tk.BOTH, expand=True, padx=(10, 0))
        self.help_option = ttk.Label(detail, style='HelpTitle.TLabel')
        self.help_option.pack(anchor=tk.W)
        self.help_description = tk.Text(
            detail, wrap=tk.WORD, bg=COLORS['surface'], fg=COLORS['text'], relief=tk.FLAT,
            font=('Segoe UI', 11), height=12, padx=0, pady=14, state=tk.DISABLED)
        self.help_description.tag_configure(
            'section', font=('Segoe UI Semibold', 11), foreground=COLORS['accent'])
        self.help_description.tag_configure(
            'body', font=('Segoe UI', 11), foreground=COLORS['text'], spacing1=7,
            spacing3=13)
        self.help_description.tag_configure(
            'example', font=('Cascadia Mono', 10), foreground='#b8e8f2',
            background=COLORS['surface2'], lmargin1=10, lmargin2=10, spacing1=7,
            spacing3=7)
        self.help_description.pack(fill=tk.BOTH, expand=True)
        self._enable_text_copying(self.help_description)
        self._filter_help()

    def _filter_help(self) -> None:
        if not hasattr(self, 'help_list'):
            return
        query = self.help_query.get().strip().lower()
        self.filtered_help = [item for item in HELP_OPTIONS if not query or query in
                              (item[0] + ' ' + item[1 if self.language_code == 'ru' else 2]).lower()]
        self.help_list.delete(0, tk.END)
        for option, *_ in self.filtered_help:
            self.help_list.insert(tk.END, option)
        if self.filtered_help:
            self.help_list.selection_set(0)
            self._show_help_item()
        else:
            self.help_option.configure(text='')
            self._set_help_text('')

    def _show_help_item(self, _event=None) -> None:
        selection = self.help_list.curselection()
        if not selection:
            return
        option, ru, en, example = self.filtered_help[selection[0]]
        self.help_option.configure(text=option)
        description = ru if self.language_code == 'ru' else en
        self.help_description.configure(state=tk.NORMAL)
        self.help_description.delete('1.0', tk.END)
        self.help_description.insert(tk.END, self.t('description') + '\n', 'section')
        self.help_description.insert(tk.END, description + '\n', 'body')
        self.help_description.insert(tk.END, self.t('example') + '\n', 'section')
        self.help_description.insert(tk.END, example, 'example')
        self.help_description.configure(state=tk.DISABLED)

    def _set_help_text(self, value: str) -> None:
        self.help_description.configure(state=tk.NORMAL)
        self.help_description.delete('1.0', tk.END)
        self.help_description.insert('1.0', value)
        self.help_description.configure(state=tk.DISABLED)

    def show_page(self, key: str) -> None:
        if key not in self.pages:
            return
        self.current_page = key
        for page in self.pages.values():
            page.pack_forget()
        self.pages[key].pack(fill=tk.BOTH, expand=True)
        for name, button in self.nav_buttons.items():
            active = name == key
            button.configure(bg=COLORS['surface2'] if active else COLORS['sidebar'],
                             fg=COLORS['accent'] if active else COLORS['muted'])
        self.page_title.configure(text=self.t(key))
        self.page_subtitle.configure(text=self.t('help_title') if key == 'help'
                                     else self.t('subtitle'))

    def change_language(self, _event=None) -> None:
        new_code = 'en' if self.language_choice.get() == 'English' else 'ru'
        if new_code == self.language_code:
            return
        paths = self.archive_paths()
        page = self.current_page
        self.language_code = new_code
        self._app_settings['language'] = new_code
        save_app_settings(self._app_settings)
        self.shell.destroy()
        self._build_ui()
        for path in paths:
            self.paths.insert(tk.END, path)
        self.current_page = page
        self.show_page(page)
        if page == 'output':
            self.root.after_idle(self._jump_active_output_to_end)
        self._set_status(self.t('running') if self.process else self.t('ready'))
        self.refresh_command()

    def copy_command(self) -> None:
        super().copy_command()

    def run(self) -> None:
        super().run()

    def run_in_new_activity_tab(self) -> None:
        if not self._validate():
            return
        self._create_activity_tab()
        self.run()

    def _drain_output(self) -> None:
        super()._drain_output()


def run_gui(initial_paths: list[str] | None = None) -> int:
    _hide_standalone_console()
    _set_windows_app_identity()
    root = tk.Tk()
    try:
        if sys.platform == 'win32':
            root.tk.call('tk', 'scaling', max(1.0, root.winfo_fpixels('1i') / 72.0))
    except tk.TclError:
        pass
    gui = ModernExtractorGui(root, initial_paths or [])
    _apply_windows_window_icon(root, gui._asset('app-icon.ico'))
    root.mainloop()
    return 0


__all__ = ['run_gui', 'ExtractorGui', 'ModernExtractorGui']
