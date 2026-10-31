# Universal SCS Extractor

[Русский](#русская-версия) · [English](#english-version)

<a id="русская-версия"></a>

## Русская версия

Universal SCS Extractor — графический и консольный инструмент для чтения, анализа и распаковки архивов Euro Truck Simulator 2 и American Truck Simulator. Приложение написано на Python, не требует .NET и объединяет распаковщик, поиск скрытых HashFS-путей, просмотр структуры, обслуживание архивов и менеджер плагинов в одном исполняемом файле.

Интерфейс и встроенная справка доступны на русском и английском языках. При запуске без параметров открывается GUI; передача консольных параметров включает CLI. Если EXE открыть вместе с одним архивом без дополнительных параметров, этот файл будет добавлен в графический интерфейс.

## Поддерживаемые форматы

- **HashFS v1** — чтение таблицы, восстановление скрытых путей, распаковка, raw-дамп, просмотр записей, изменение манифеста и повторное zlib-сжатие.
- **HashFS v2** — чтение раздельных таблиц путей и метаданных, salted CityHash64, GDeflate-текстуры TOBJ+DDS, глубокий поиск и распаковка.
- **ZIP/eZIP** — обычные и намеренно нестандартные ZIP-пакеты SCS, включая архивы с ложными служебными структурами.
- **AEM** — чтение и распаковка AEM-пакетов, а также изменение `manifest.sii`.

Повреждённые, дублирующиеся и подозрительные HashFS-записи классифицируются по причине и учитываются как junk. Если путь корректной записи восстановить невозможно, содержимое сохраняется в `_unknown`, а не теряется.

## Основные возможности

- Параллельная распаковка и параллельный анализ содержимого с автоматически подобранным числом потоков; предел можно задать в настройках.
- Совместный анализ нескольких архивов: таблицы, каталоги и ссылки объединяются до глубокого поиска.
- Быстрый поиск сначала по стартовой и публичной базе, затем анализ SII/SUI, моделей, материалов, TOBJ и других файлов со ссылками.
- Восстановление абсолютных и относительных цепочек `SII → model → MAT → TOBJ → DDS`, включая родственные расширения моделей и файлов секторов карты.
- Поддержка `--reference`: зависимые архивы участвуют в расшифровке путей, но сами не распаковываются.
- Распаковка известных файлов, частичных каталогов, выбранных масок, raw-записей или всего архива.
- Единые списки путей, файлов, дерево каталогов и таблица HashFS-записей для одного или нескольких архивов.
- Добавление результата списка в существующий файл без дубликатов.
- Раздельные выходные папки, пропуск существующих файлов, dry run и подробный лог.
- Изменение `manifest.sii` в HashFS v1/v2, ZIP и AEM с атомарной заменой исходного архива.
- Сжатие несжатых записей HashFS v1 с уплотнением архива или без него.
- Опциональный обмен найденными путями через настраиваемое сообщественное хранилище. Получение и публикация включаются пользователем отдельно; сетевую операцию можно отменить.
- Менеджер внешних PYD/DLL-плагинов: установка, включение, отключение, удаление и настройки.

## Графический интерфейс

GUI содержит страницы архивов, параметров, настроек, плагинов, журнала и справки. Команда запуска всегда видна до выполнения. Журнал поддерживает несколько вкладок операций, цветовые уровни, копирование текста, сворачиваемые подробности по каждому архиву и сохранение полного вывода без ограничения на видимое число строк.

Настройки сохраняются рядом с приложением. В них находятся язык, максимальное число потоков, значения `--deep` и `--log` по умолчанию, путь журнала и сетевые параметры. Значения по умолчанию лишь устанавливают галочки при новом запуске и не мешают отключить их для текущей операции.

## Быстрый старт

```powershell
# Обычная распаковка
.\UniversalSCSExtractor.exe .\mod.scs --dest=.\extracted

# Глубокое восстановление скрытых HashFS-путей
.\UniversalSCSExtractor.exe .\part1.scs .\part2.scs --deep --dest=.\extracted --log

# Использование зависимостей только как справочных архивов
.\UniversalSCSExtractor.exe .\addon.scs --reference .\base.scs .\def.scs --deep

# Список всех найденных файлов и каталогов без распаковки
.\UniversalSCSExtractor.exe .\mod.scs --list-all --list-output=.\paths.txt

# Дерево каталогов
.\UniversalSCSExtractor.exe .\mod.scs --tree
```

Без EXE приложение можно запустить из исходников:

```powershell
python .\Extractor\extractor.py .\mod.scs --deep
```

## Параметры CLI

### Вход, поиск и распаковка

| Параметр | Назначение |
| --- | --- |
| `path...` | Один или несколько архивов. |
| `-a`, `--all` | Считать входные пути папками и обработать все `.scs` непосредственно внутри них. |
| `-D`, `--deep` | Восстановить скрытые HashFS-пути по таблицам, содержимому и ссылкам; ненайденное сохранить в `_unknown`. |
| `-d DIR`, `--dest DIR` | Папка назначения; по умолчанию `extracted`. Явное указание включает распаковку. |
| `--additional FILE` | Дополнительный UTF-8/UTF-16 список кандидатов путей, по одному в строке. |
| `-ref`, `--reference ARCHIVE...` | Архивы-зависимости для поиска путей без их распаковки; включает `--deep`. |
| `-f`, `--filter PATTERNS` | Маски через запятую (`*.sii,/def/*`) или регулярное выражение `r/.../`. Можно повторять. |
| `-p`, `--partial PATHS` | Распаковать только перечисленные через запятую файлы или каталоги. |
| `-P`, `--paths FILE` | То же, но начальные пути читаются из файла. |
| `-S`, `--separate` | Для нескольких архивов создать отдельную папку каждому. |
| `-s`, `--skip-existing` | Не заменять уже существующие файлы. |
| `-r`, `--raw` | Сохранить HashFS-записи под шестнадцатеричными хешами без обхода дерева. |
| `--salt NUMBER` | Принудительно задать HashFS salt от 0 до 65535. |
| `--no-update` | Не исправлять внутренние ссылки после переименования недопустимых путей. |
| `-q`, `--quiet` | Скрыть построчный вывод, сохранив этапы и итоговую статистику. |
| `--dry-run` | Выполнить чтение, анализ и проверку без записи файлов. |

### Просмотр структуры

| Параметр | Назначение |
| --- | --- |
| `--list` | Вывести пути реально содержащихся файлов. |
| `--list-all` | Вывести файлы и каталоги, включая найденные ссылки; для HashFS включает глубокий поиск. |
| `--list-output FILE` | Записать список в файл, дополняя существующий без дубликатов. |
| `--list-entries` | Вывести хеши, смещения, размеры, флаги и типы HashFS-записей. |
| `--tree` | Построить общее дерево каталогов выбранных архивов. |
| `--force-extract` | Одновременно выполнить выбранный режим просмотра и распаковку. Распаковку также включают явно заданные `--dest`, `--separate`, `--skip-existing`, `--raw`, `--paths`, `--partial` и `--no-update`. |

### Обслуживание архивов

| Параметр | Назначение |
| --- | --- |
| `--manifest` | Удалить `compatible_versions[]` из `manifest.sii` и атомарно заменить архив. |
| `--manifest-replace OLD=NEW` | Дополнительно заменить текст; параметр можно повторять или разделять пары `;`. |
| `--edit` | Интерактивный ввод замен манифеста в консоли. |
| `--compress` | Сжать несжатые записи HashFS v1 и уплотнить архив. |
| `-jc`, `--jc` | Сжать HashFS v1 без удаления освободившихся областей. |
| `--table-at-end` | Для нестандартного HashFS v1 прочитать таблицу с конца, игнорируя offset заголовка. |
| `--log [FILE]` | Включить лог; без имени используется `_extraction.log`. |

### Плагины

| Параметр | Назначение |
| --- | --- |
| `--plugin-load FILE` | Установить `.pyd` или native `.dll` рядом с приложением. |
| `--plugin-list` | Показать активные и доступные плагины. |
| `--plugin-activate NAME` | Включить плагин по ID, имени или файлу; `ALL` включает все. |
| `--plugin-disable NAME` | Отключить плагин; `ALL` отключает все. |
| `-?`, `-h`, `--help` | Полная актуальная консольная справка. |

## Как создать плагин

Экстрактор загружает плагины из каталога рядом с приложением и рекурсивно из `Plugins`. Поддерживаются расширения `.pyd` и `.dll`; исходный `.py` напрямую не загружается.

Минимальный PYD-плагин экспортирует словарь `PLUGIN_INFO` и функцию `execute`. Исходник можно собрать в extension-модуль через Cython или другой совместимый инструмент; версия CPython и архитектура PYD должны совпадать с приложением.

```python
PLUGIN_INFO = {
    "api_version": 1,
    "id": "my-plugin",
    "name": "My plugin",
    "version": "1.0.0",
    "active_by_default": False,
    "settings": {},
}

def execute(event: str, context: dict) -> dict:
    # События API v1: gui.initialize, console.initialize, console.complete.
    return {}
```

Native DLL использует C ABI и возвращает UTF-8 JSON:

```c
const char *use_plugin_info(void);
const char *use_plugin_execute(const char *event_name, const char *context_json);
```

`use_plugin_info()` должен вернуть объект с `api_version`, `id`, `name` и `version`; `use_plugin_execute()` — JSON-объект результата. Память возвращаемых строк должна оставаться действительной до следующего вызова этой DLL. Состояние активации и пользовательские настройки хранятся в `plugins.json`.

## Сборка

Требуется Python 3.11+; зависимости сборки перечислены в `requirements-build.txt`.

```powershell
# Автоматический формат текущей ОС
python .\build.py

# Windows EXE
python .\build.py --format exe

# Linux DEB/RPM или macOS APP — выполнять на соответствующей ОС
python .\build.py --format deb
python .\build.py --format rpm
python .\build.py --format app
```

`--skip-install` запрещает автоматическую установку сборочных зависимостей. Для служебной сборки с заранее заданным сообщественным источником путей предусмотрены `--community-webhook` и `--community-message-id`; без них пользователь вводит подключение в GUI.

В Windows также доступен `build.ps1`. Результат помещается в `dist`; Linux-пакеты больше не складываются во вложенный `dist/packages`.

## Структура исходников

| Каталог | Назначение |
| --- | --- |
| `Extractor` | CLI, GUI, ZIP/AEM, глубокий поиск, журнал, плагины и обслуживание архивов. |
| `TruckLib.Core` | Базовые типы, двоичный ввод-вывод и абстракции файловых систем. |
| `TruckLib.HashFs` | CityHash64 и чтение HashFS v1/v2. |
| `TruckLib.Models` | Форматы моделей, материалов, TOBJ и связанные структуры. |
| `TruckLib.Sii` | SII/SUI/MAT, includes, 3nK и ScsC. |
| `GisDeflate` | Декодирование GDeflate-текстур. |

## Лицензия

Основной проект распространяется на условиях **GNU General Public License v2.0** — см. [LICENSE](LICENSE). Сторонние компоненты сохраняют собственные уведомления и лицензии в своих каталогах; в частности, для `GisDeflate` действует отдельный файл `GisDeflate/LICENSE_GPLv3.txt`.

Проект не связан с SCS Software. Названия Euro Truck Simulator, American Truck Simulator и SCS используются только для описания совместимости.

---

<a id="english-version"></a>

## English version

Universal SCS Extractor is a graphical and command-line application for reading, inspecting, and extracting Euro Truck Simulator 2 and American Truck Simulator archives. It is written in Python, does not require .NET, and combines archive extraction, hidden HashFS path discovery, structure inspection, archive maintenance, and plugin management in one executable.

The interface and built-in help are available in English and Russian. Starting the application without arguments opens the GUI. Supplying command-line options starts the CLI. Opening a single archive with the executable, without additional options, adds that file to the graphical interface.

### Supported formats

- **HashFS v1** — table reading, hidden-path recovery, extraction, raw dumps, entry inspection, manifest editing, and zlib recompression.
- **HashFS v2** — separate path and metadata tables, salted CityHash64, GDeflate TOBJ+DDS textures, deep discovery, and extraction.
- **ZIP/eZIP** — ordinary and deliberately non-standard SCS ZIP packages, including archives containing misleading service structures.
- **AEM** — AEM package reading, extraction, and `manifest.sii` editing.

Malformed, duplicated, and suspicious HashFS entries are classified by reason and counted as junk. If the path of an otherwise valid entry cannot be recovered, its payload is written to `_unknown` instead of being discarded.

### Main features

- Parallel extraction and parallel content analysis with an automatically selected worker count and a configurable upper limit.
- Combined analysis of multiple archives: tables, directories, and references are merged before deep discovery.
- Fast lookup through the start-path and public databases before falling back to SII/SUI, model, material, TOBJ, and other payload analysis.
- Recovery of absolute and relative `SII → model → MAT → TOBJ → DDS` chains, related model extensions, and map-sector file families.
- Dependency archives through `--reference`: their tables and path-bearing files participate in discovery without being extracted.
- Extraction of all known files, selected subtrees, filtered paths, raw entries, or complete archives.
- Unified file lists, all-path lists, directory trees, and HashFS entry tables for one or many archives.
- Appending list results to an existing file without duplicates.
- Separate output directories, existing-file skipping, dry runs, and detailed logging.
- Atomic `manifest.sii` editing in HashFS v1/v2, ZIP, and AEM archives.
- Recompression of uncompressed HashFS v1 entries, with or without compacting unused space.
- Optional community path exchange. Downloading and publishing are enabled separately by the user, and network operations can be cancelled.
- External PYD/DLL plugins with installation, activation, deactivation, deletion, and settings management.

### Graphical interface

The GUI contains Archives, Options, Settings, Plugins, Activity, and Help pages. The generated command line remains visible before execution. The activity journal supports multiple operation tabs, colored severity levels, text copying, collapsible per-archive details, and complete output retention independently of the visible widget size.

Settings are stored beside the application. They include language, maximum worker count, default `--deep` and `--log` states, the default log path, and network options. Default switches only initialize the controls for a new launch and can still be disabled for the current operation.

### Quick start

```powershell
# Normal extraction
.\UniversalSCSExtractor.exe .\mod.scs --dest=.\extracted

# Deep hidden-path recovery from several HashFS archives
.\UniversalSCSExtractor.exe .\part1.scs .\part2.scs --deep --dest=.\extracted --log

# Use dependencies only as discovery references
.\UniversalSCSExtractor.exe .\addon.scs --reference .\base.scs .\def.scs --deep

# Save every discovered file and directory without extraction
.\UniversalSCSExtractor.exe .\mod.scs --list-all --list-output=.\paths.txt

# Print the directory tree
.\UniversalSCSExtractor.exe .\mod.scs --tree
```

Run directly from the sources:

```powershell
python .\Extractor\extractor.py .\mod.scs --deep
```

### Command-line options

#### Input, discovery, and extraction

| Option | Description |
| --- | --- |
| `path...` | One or more archives. |
| `-a`, `--all` | Treat every input as a directory and process the `.scs` files directly inside it. |
| `-D`, `--deep` | Recover hidden HashFS paths from tables, payloads, and references; unresolved entries are written to `_unknown`. |
| `-d DIR`, `--dest DIR` | Destination directory; default: `extracted`. Specifying it explicitly enables extraction. |
| `--additional FILE` | Additional UTF-8/UTF-16 candidate paths, one per line. |
| `-ref`, `--reference ARCHIVE...` | Dependency archives used for path discovery but not extracted; implies `--deep`. |
| `-f`, `--filter PATTERNS` | Comma-separated wildcards such as `*.sii,/def/*`, or an `r/.../` regular expression. Repeatable. |
| `-p`, `--partial PATHS` | Extract only the comma-separated files or directories. |
| `-P`, `--paths FILE` | Read partial-extraction start paths from a file. |
| `-S`, `--separate` | Create an individual output directory for every input archive. |
| `-s`, `--skip-existing` | Do not replace existing destination files. |
| `-r`, `--raw` | Write HashFS entries under hexadecimal hashes without traversing the directory tree. |
| `--salt NUMBER` | Override the HashFS path-hashing salt with a value from 0 to 65535. |
| `--no-update` | Do not repair internal references after invalid path characters cause a rename. |
| `-q`, `--quiet` | Hide per-file output while retaining phases and final statistics. |
| `--dry-run` | Read, discover, and validate without writing extracted files. |

#### Structure inspection

| Option | Description |
| --- | --- |
| `--list` | Print the paths of files actually contained in the archive. |
| `--list-all` | Print files and directories, including discovered references; enables deep HashFS discovery. |
| `--list-output FILE` | Save the list and extend an existing file without duplicates. |
| `--list-entries` | Print HashFS hashes, offsets, sizes, flags, and entry types. |
| `--tree` | Build one combined directory tree for the selected archives. |
| `--force-extract` | Extract while also running a list or tree operation. Explicit `--dest`, `--separate`, `--skip-existing`, `--raw`, `--paths`, `--partial`, and `--no-update` options also force extraction. |

#### Archive maintenance

| Option | Description |
| --- | --- |
| `--manifest` | Remove `compatible_versions[]` from `manifest.sii` and atomically replace the archive. |
| `--manifest-replace OLD=NEW` | Apply text replacements after manifest cleanup; repeat the option or separate pairs with `;`. |
| `--edit` | Enter manifest replacements interactively in the console. |
| `--compress` | Compress uncompressed HashFS v1 entries and compact the archive. |
| `-jc`, `--jc` | Compress HashFS v1 entries without removing unused archive regions. |
| `--table-at-end` | Read a non-standard HashFS v1 table from the end, ignoring the header offset. |
| `--log [FILE]` | Enable logging; without a filename, `_extraction.log` is used. |

#### Plugins

| Option | Description |
| --- | --- |
| `--plugin-load FILE` | Install a `.pyd` or native `.dll` beside the application. |
| `--plugin-list` | List active and available plugins. |
| `--plugin-activate NAME` | Enable a plugin by ID, name, or filename; `ALL` enables every plugin. |
| `--plugin-disable NAME` | Disable a plugin; `ALL` disables every plugin. |
| `-?`, `-h`, `--help` | Print the complete current console help. |

### Creating a plugin

The extractor discovers plugins beside the application and recursively inside `Plugins`. It loads `.pyd` and `.dll` files; plain `.py` source files are not loaded directly.

A minimal PYD plugin exports `PLUGIN_INFO` and `execute`. Compile the source as a Python extension with Cython or another compatible tool. The PYD architecture and CPython version must match the application.

```python
PLUGIN_INFO = {
    "api_version": 1,
    "id": "my-plugin",
    "name": "My plugin",
    "version": "1.0.0",
    "active_by_default": False,
    "settings": {},
}

def execute(event: str, context: dict) -> dict:
    # API v1 events: gui.initialize, console.initialize, console.complete.
    return {}
```

A native DLL uses a UTF-8 JSON C ABI:

```c
const char *use_plugin_info(void);
const char *use_plugin_execute(const char *event_name, const char *context_json);
```

`use_plugin_info()` returns an object containing at least `api_version`, `id`, `name`, and `version`. `use_plugin_execute()` returns a JSON object. Returned string storage must remain valid until the next call into the same DLL. Activation state and user settings are stored in `plugins.json`.

### Building

Python 3.11 or newer is required. Build dependencies are listed in `requirements-build.txt`.

```powershell
# Native format for the current operating system
python .\build.py

# Windows EXE
python .\build.py --format exe

# Run these on the corresponding target operating system
python .\build.py --format deb
python .\build.py --format rpm
python .\build.py --format app
```

`--skip-install` prevents automatic installation of build dependencies. Release builds may preconfigure the community path source with `--community-webhook` and `--community-message-id`; otherwise the connection is configured in the GUI.

`build.ps1` is also available on Windows. Build artifacts are written directly to `dist`; Linux packages are not placed in a nested `dist/packages` directory.

### Source layout

| Directory | Purpose |
| --- | --- |
| `Extractor` | CLI, GUI, ZIP/AEM support, deep discovery, activity journal, plugins, and archive maintenance. |
| `TruckLib.Core` | Core types, binary I/O, and filesystem abstractions. |
| `TruckLib.HashFs` | CityHash64 and HashFS v1/v2 readers. |
| `TruckLib.Models` | Model, material, TOBJ, and related structures. |
| `TruckLib.Sii` | SII/SUI/MAT parsing, includes, 3nK, and ScsC. |
| `GisDeflate` | GDeflate texture decompression. |

### License

The main project is distributed under the **GNU General Public License v2.0**; see [LICENSE](LICENSE). Third-party components retain their own notices and licenses in their respective directories. In particular, `GisDeflate/LICENSE_GPLv3.txt` applies to the GisDeflate component.

This project is not affiliated with SCS Software. Euro Truck Simulator, American Truck Simulator, and SCS names are used only to describe compatibility.
