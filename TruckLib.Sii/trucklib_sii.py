"""SII decoding and reference extraction for the Python extractor."""
from __future__ import annotations

from pathlib import PurePosixPath
from struct import pack, unpack, unpack_from
from dataclasses import dataclass, field
from itertools import product
import re
from string import ascii_lowercase, digits
import zlib
from pathlib import Path
from typing import BinaryIO

from aes import decrypt_cbc

SII_KEY = bytes.fromhex('2a5fcb1791d22fb60245b3d8369ed0b2c27371563fbf1f3c9edf6b11825a5d0a')
KEY_TABLE = bytes.fromhex('''
f8d1aa835c750e27b099e2cb143d466f68413a13cce59eb72009725b84add6ff
d8f18aa37c552e0790b9c2eb341d664f48611a33ecc5be970029527ba48df6df
b891eac31c354e67f0d9a28b547d062f28017a538ca5def76049321bc4ed96bf
98b1cae33c156e47d0f982ab745d260f08215a73ac85fed74069123be4cdb69f
78512a03dcf58ea73019624b94bdc6efe8c1ba934c651e37a089f2db042d567f
58710a23fcd5ae871039426bb49de6cfc8e19ab36c453e1780a9d2fb240d765f
38116a439cb5cee77059220bd4fd86afa881fad30c255e77e0c9b29b446d163f
18314a63bc95eec75079022bf4dda68f88a1daf32c057e57c0e992bb644d361f
''')


def decode(data: bytes) -> bytes:
    """Decode 3nK and ScsC layers, mirroring SiiFile.Decode."""
    for _ in range(8):
        if data.startswith(b'3nK'):
            if len(data) < 6:
                raise ValueError('Truncated 3nK header')
            seed = data[5]
            data = bytes(byte ^ KEY_TABLE[(seed + i) & 255] for i, byte in enumerate(data[6:]))
        elif data.startswith(b'ScsC'):
            if len(data) < 56:
                raise ValueError('Truncated ScsC header')
            iv = data[36:52]
            declared = unpack_from('<I', data, 52)[0]
            data = zlib.decompress(decrypt_cbc(data[56:], SII_KEY, iv))
            if declared and len(data) != declared:
                raise ValueError('ScsC decoded size mismatch')
        else:
            return data
    raise ValueError('Too many nested SII encoding layers')


# ``!``, brackets and commas are unusual but legal in SCS asset names and are
# used by shipped sign definitions. Keep this tighter than a generic quoted
# string so UI text and URLs do not become fake paths.
ABSOLUTE_PATH = re.compile(
    r'(?<![\w.])/[A-Za-z0-9_+.!\[\],/\-]+\.[A-Za-z0-9_]+')
QUOTED_ABSOLUTE_PATH = re.compile(
    r'["\'](/[^"\'\r\n]+\.[A-Za-z0-9_]+)["\']')
INCLUDE = re.compile(r'@include\s+"([^"]+)"', re.IGNORECASE)

MOD_DESCRIPTION_LOCALES = (
    'bg_bg', 'ca_es', 'cs_cz', 'da_dk', 'de_de', 'el_gr', 'en_gb', 'en_us',
    'es_es', 'es_la', 'et_ee', 'eu_es', 'fi_fi', 'fr_ca', 'fr_fr', 'gl_es',
    'hr_hr', 'hu_hu', 'it_it', 'ja_jp', 'ka_ge', 'ko_kr', 'lt_lt', 'lv_lv',
    'mk_mk', 'nl_nl', 'no_no', 'pl_pl', 'pl_si', 'pt_br', 'pt_pt', 'ro_ro',
    'ru_ru', 'sk_sk', 'sl_sl', 'sr_sp', 'sr_sr', 'sv_se', 'tr_tr', 'uk_uk',
    'vi_vn', 'zh_cn', 'zh_tw',
)


def _strip_comments(source: str) -> str:
    """Remove SII's #, // and /* */ comments while preserving quoted strings."""
    output = []
    position = 0
    quoted = False
    while position < len(source):
        char = source[position]
        pair = source[position:position + 2]
        if char == '"' and (position == 0 or source[position - 1] != '\\'):
            quoted = not quoted
        if not quoted and pair == '/*':
            end = source.find('*/', position + 2)
            position = len(source) if end < 0 else end + 2
            continue
        if not quoted and (pair == '//' or char == '#'):
            end = source.find('\n', position)
            position = len(source) if end < 0 else end
            continue
        output.append(char)
        position += 1
    return ''.join(output)


@dataclass
class Unit:
    class_name: str
    name: str
    attributes: dict[str, str | list[str]] = field(default_factory=dict)

    @property
    def Class(self): return self.class_name

    @Class.setter
    def Class(self, value): self.class_name = value


@dataclass
class SiiFile:
    units: list[Unit] = field(default_factory=list)
    includes: set[str] = field(default_factory=set)

    @classmethod
    def load(cls, data: bytes | str, archive_path: str = '', read_include=None,
             _visited: set[str] | None = None, ignore_missing_includes: bool = False) -> 'SiiFile':
        if isinstance(data, bytes) and len(data) < 4: raise ValueError('SII buffer is too short')
        text = decode(data).decode('utf-8-sig') if isinstance(data, bytes) else data
        result = cls()
        text = _strip_comments(text)
        _visited = set() if _visited is None else _visited
        _visited.add(archive_path)
        parent = PurePosixPath(archive_path).parent
        for relative in INCLUDE.findall(text):
            name = str(parent / relative) if not relative.startswith('/') else relative
            result.includes.add(name)
            if read_include is not None and name not in _visited:
                try:
                    included = cls.load(read_include(name), name, read_include, _visited, ignore_missing_includes)
                    result.units.extend(included.units)
                    result.includes.update(included.includes)
                except (FileNotFoundError, ValueError):
                    if not ignore_missing_includes: raise
        unit_header = re.compile(r'([\w:]+)\s*:\s*([^\s{}]+)\s*\{')
        position = 0
        while match := unit_header.search(text, position):
            depth = 1
            end = match.end()
            while end < len(text) and depth:
                depth += (text[end] == '{') - (text[end] == '}')
                end += 1
            body = text[match.end():end - 1]
            unit = Unit(match.group(1), match.group(2), _parse_attributes(body))
            result.units.append(unit)
            position = end
        return result

    @classmethod
    def open(cls, path: str, fs=None, ignore_missing_includes: bool = False) -> 'SiiFile':
        if fs is None:
            raw = Path(path).read_bytes()
            reader = lambda name: Path(name).read_bytes()
        else:
            raw = fs.read_all_bytes(path)
            reader = fs.read_all_bytes
        return cls.load(raw, path, reader, ignore_missing_includes=ignore_missing_includes)

    def serialize(self, indentation: str = '\t') -> str:
        lines = ['SiiNunit', '{', '']
        for unit in self.units:
            lines += [f'{unit.class_name} : {unit.name}', '{']
            lines += _serialize_attributes(unit.attributes, indentation, False)
            lines += ['}', '']
        lines.append('}')
        return '\n'.join(lines) + '\n'

    def save(self, path: str, indentation: str = '\t') -> None:
        Path(path).write_text(self.serialize(indentation), encoding='utf-8')


@dataclass
class MatFile:
    effect: str = ''
    texture_paths: list[str] = field(default_factory=list)
    attributes: dict[str, object] = field(default_factory=dict)
    textures: list['Texture'] = field(default_factory=list)

    @classmethod
    def load(cls, data: bytes | str) -> 'MatFile':
        text = data.decode('utf-8-sig') if isinstance(data, bytes) else data
        text = _strip_comments(text)
        effect = re.search(r'\b(?:effect|material)\s*:\s*"?([^"\s{]+)', text)
        result = cls(effect.group(1) if effect else '')
        current=None; indexed:dict[str,list]= {}
        for line in text.splitlines()[1:]:
            stripped=line.strip()
            texture_match=re.match(r'texture\s*:\s*"([^"]+)"\s*\{',stripped)
            if texture_match:
                current=Texture(texture_match.group(1)); result.textures.append(current); continue
            if stripped=='}':
                if current is not None: current=None
                continue
            match=re.match(r'([\w.]+)(?:\[(\d*)\])?\s*:\s*(.+)',stripped)
            if not match: continue
            key,index,raw=match.groups(); value=_parse_scalar(raw.strip())
            target=current.attributes if current else result.attributes
            if index is not None:
                values=target.get(key)
                if not isinstance(values, list):
                    # Some generated/protected materials mix ``key: value``
                    # with later ``key[n]: value`` declarations.  Preserve
                    # the first value and promote the attribute to an array.
                    values=[] if values is None else [values]
                    target[key]=values
                idx=len(values) if index=='' else int(index)
                if len(values)<=idx: values.extend([None]*(idx+1-len(values)))
                values[idx]=value
            else: target[key]=value
        legacy=result.attributes.get('texture'); names=result.attributes.get('texture_name')
        if legacy is not None and names is not None:
            legacy=legacy if isinstance(legacy,list) else [legacy]; names=names if isinstance(names,list) else [names]
            result.textures=[Texture(str(names[min(i,len(names)-1)]),{'source':path}) for i,path in enumerate(legacy)]
            result.attributes.pop('texture',None); result.attributes.pop('texture_name',None)
        result.texture_paths=[str(t.attributes['source']) for t in result.textures if 'source' in t.attributes]
        return result

    @classmethod
    def open(cls, path: str, fs=None) -> 'MatFile':
        data = Path(path).read_text(encoding='utf-8-sig') if fs is None else fs.read_all_text(path)
        return cls.load(data)

    def serialize(self, indentation: str = '\t') -> str:
        lines = [f'effect : "{self.effect}" {{']
        lines += _serialize_attributes(self.attributes, indentation, True)
        for texture in self.textures:
            lines.append(f'{indentation}texture: "{texture.name}" {{')
            lines += _serialize_attributes(texture.attributes, indentation * 2, True)
            lines.append(f'{indentation}}}')
        return '\n'.join(lines + ['}', ''])

    def save(self, path: str, indentation: str = '\t') -> None:
        Path(path).write_text(self.serialize(indentation), encoding='utf-8')


@dataclass(frozen=True)
class LinkPointer:
    value: str
    def __str__(self): return self.value

@dataclass(frozen=True)
class OwnerPointer:
    value: str
    def __str__(self): return self.value

@dataclass(frozen=True)
class Placement:
    position: tuple[float, float, float]
    rotation: tuple[float, float, float, float]

@dataclass
class Texture:
    name: str = ''
    attributes: dict[str, object] = field(default_factory=dict)

@dataclass
class ThreeNKHeader:
    seed: int = 0
    MAGIC = b'3nK\x01\x00'
    @classmethod
    def read(cls, stream: BinaryIO):
        if stream.read(5) != cls.MAGIC: raise ValueError('Invalid 3nK header')
        seed = stream.read(1)
        if not seed: raise EOFError('Truncated 3nK header')
        return cls(seed[0])
    def write(self, stream: BinaryIO): stream.write(self.MAGIC + bytes([self.seed]))

class ThreeNK:
    @staticmethod
    def transcode(payload: bytes, seed: int) -> bytes:
        return bytes(b ^ KEY_TABLE[(seed + i) & 255] for i, b in enumerate(payload))
    @classmethod
    def encode(cls, payload: bytes, seed: int = 0) -> bytes:
        return ThreeNKHeader.MAGIC + bytes([seed]) + cls.transcode(payload, seed)
    @classmethod
    def decode(cls, payload: bytes) -> bytes:
        if not payload.startswith(ThreeNKHeader.MAGIC) or len(payload) < 6: raise ValueError('Invalid 3nK data')
        return cls.transcode(payload[6:], payload[5])

@dataclass
class EncryptedSiiHeader:
    iv: bytes
    decoded_length: int
    MAGIC = b'ScsC'
    @classmethod
    def read(cls, data: bytes):
        if len(data) < 56 or data[:4] != cls.MAGIC: raise ValueError('Invalid ScsC header')
        return cls(data[36:52], unpack_from('<I', data, 52)[0])

class EncryptedSii:
    @staticmethod
    def decrypt(data: bytes) -> bytes:
        header = EncryptedSiiHeader.read(data)
        result = zlib.decompress(decrypt_cbc(data[56:], SII_KEY, header.iv))
        if header.decoded_length and len(result) != header.decoded_length: raise ValueError('ScsC decoded size mismatch')
        return result

def trim_byte_order_mark(value: str) -> str: return value.removeprefix('\ufeff')
def remove_comments(value: str) -> str: return _strip_comments(value)

def _ignore_missing(reader):
    def wrapped(path):
        try: return reader(path)
        except FileNotFoundError: return b''
    return wrapped

def _parse_scalar(raw: str):
    value = raw.strip()
    if value.startswith('"') and value.endswith('"'): return value[1:-1]
    if value.startswith('"'): return value[1:]
    if value == 'true': return True
    if value == 'false': return False
    if re.fullmatch(r'[+-]?0x[\da-fA-F]+', value): return int(value, 16)
    if re.fullmatch(r'&[\da-fA-F]{8}', value): return unpack('>f', bytes.fromhex(value[1:]))[0]
    if re.fullmatch(r'[+-]?\d+', value): return int(value)
    if re.fullmatch(r'[+-]?(?:\d+(?:\.\d*)?)(?:e[+-]?\d+)?f?', value, re.I): return float(value.rstrip('fF'))
    def flt(x):
        x=x.strip()
        if x.startswith('&') and len(x)==9: return unpack('>f', bytes.fromhex(x[1:]))[0]
        return float(x.rstrip('fF'))
    tuples = re.findall(r'[({]([^)}]+)[)}]', value)
    if len(tuples) == 2 and ';' in tuples[1]:
        p=tuple(flt(x) for x in tuples[0].split(',')); qv=re.split(r'[;,]', tuples[1]); w,x,y,z=(flt(n) for n in qv)
        return Placement(p, (x,y,z,w))
    if len(tuples) == 1:
        parts=re.split(r'[;,]', tuples[0]); vals=tuple(flt(x) for x in parts)
        if ';' in tuples[0] and len(vals)==4: return (vals[1],vals[2],vals[3],vals[0])
        return vals
    if value.startswith('.'): return OwnerPointer(value)
    if re.fullmatch(r'[0-9a-z_]{1,12}', value):
        try:
            sys_path = str(Path(__file__).parent.parent / 'TruckLib.Core')
            if sys_path not in __import__('sys').path: __import__('sys').path.insert(0, sys_path)
            from trucklib_core import Token
            return Token(value)
        except ValueError: pass
    return LinkPointer(value)

def _parse_attributes(body: str, mat: bool = False) -> dict[str, object]:
    attrs: dict[str, object] = {}; array_sizes = {}; pos=0
    pair = re.compile(r'(?m)^\s*([\w.]+)(\[\d*\])?\s*:\s*')
    while (m := pair.search(body, pos)):
        key, bracket = m.group(1), m.group(2); start=m.end(); nextm=pair.search(body,start)
        end=nextm.start() if nextm else len(body); raw=body[start:end].strip()
        if mat and key == 'texture' and raw.startswith('"') and '{' in raw:
            name=raw[1:raw.find('"',1)]; op=raw.find('{'); depth=1; i=op+1
            while i<len(raw) and depth: depth += (raw[i]=='{')-(raw[i]=='}'); i+=1
            attrs.setdefault('texture', []).append(Texture(name, _parse_attributes(raw[op+1:i-1], True))); pos=end; continue
        value=_parse_scalar(raw.splitlines()[0])
        if bracket is None: attrs[key]=value
        elif bracket == '[]':
            current=attrs.get(key)
            if not isinstance(current,list): current=[]; attrs[key]=current
            current.append(value)
        else:
            idx=int(bracket[1:-1]); current=attrs.get(key)
            if not isinstance(current,list):
                previous=None if current is None or key in array_sizes else current
                current=[None]*max(array_sizes.get(key,0),idx+1); attrs[key]=current
                if previous is not None: current[0]=previous
            if len(current)<=idx: current.extend([None]*(idx+1-len(current)))
            current[idx]=value
        if bracket is None and isinstance(value,int): array_sizes[key]=value
        pos=end
    return attrs

def _format_value(value, mat=False):
    if isinstance(value,str): return f'"{value}"'
    if isinstance(value,bool): return str(value).lower()
    if isinstance(value,(LinkPointer,OwnerPointer)): return value.value
    try:
        from trucklib_core import Token
        if isinstance(value,Token): return value.string
    except ImportError: pass
    if isinstance(value,Placement): return f'{_format_value(value.position)} {_format_quaternion(value.rotation)}'
    if isinstance(value,tuple): return ('{' if mat else '(')+', '.join(_format_value(x) for x in value)+('}' if mat else ')')
    if isinstance(value,float): return f'{value:.1f}' if value.is_integer() else str(value)
    return str(value)
def _format_quaternion(q):
    x,y,z,w=q; return f'({_format_value(w)}; {_format_value(x)}, {_format_value(y)}, {_format_value(z)})'
def _serialize_attributes(attrs, indentation, mat):
    lines=[]
    for key,value in attrs.items():
        if isinstance(value,list):
            for item in value: lines.append(f'{indentation}{key}[]: {_format_value(item,mat)}')
        else: lines.append(f'{indentation}{key}: {_format_value(value,mat)}')
    return lines


def find_paths(data: bytes, archive_path: str) -> set[str]:
    decoded_text = decode(data).decode('utf-8-sig', 'replace')
    text = _strip_comments(decoded_text)
    paths = set(ABSOLUTE_PATH.findall(text))
    # Quoted SCS paths may legally contain spaces.  The stricter unquoted
    # expression above intentionally rejects them to avoid treating prose as
    # paths, while quoted values have an unambiguous boundary.
    paths.update(QUOTED_ABSOLUTE_PATH.findall(text))
    # Commented-out definitions are still valuable candidate paths when a
    # protected archive keeps the corresponding payload.  The HashFS caller
    # verifies every candidate against the real salted table before accepting
    # it, so this cannot turn arbitrary comments into extracted files.
    paths.update(ABSOLUTE_PATH.findall(decoded_text))
    paths.update(QUOTED_ABSOLUTE_PATH.findall(decoded_text))
    parent = PurePosixPath(archive_path).parent
    is_mod_manifest = bool(re.search(r'\bmod_package\s*:', text))
    for relative in INCLUDE.findall(text):
        paths.add(str(parent / relative) if not relative.startswith('/') else relative)
    for icon in re.findall(r'\bicon\s*:\s*"?([\w.!\[\],/+-]+)', text):
        if is_mod_manifest:
            paths.add(icon)
        elif not icon.startswith('/'):
            paths.add('/material/ui/accessory/' + icon +
                      ('' if icon.lower().endswith('.mat') else '.mat'))
    for sound in re.findall(r'\b(?:sounds|sound_path|sound_sfx|scene_music)(?:\[\])?\s*:\s*"?([^"\s]+)', text):
        sound = sound.split('|')[-1].split('#')[0]
        if sound.startswith('/'):
            paths.add(sound)
            if sound.endswith('.bank'):
                paths.add(sound + '.guids')
    for company in re.findall(r'\bcompany_permanent\s*:\s*([\w.]+)', text):
        paths.add('/material/ui/company/small/' + company.split('.')[-1] + '.mat')
    for country in re.findall(r'\bcountry_data\s*:\s*([\w.]+)', text):
        name = country.split('.')[-1]
        paths.add('/font/license_plate/' + name + '.font')
        for material in ('trailer', 'truck_front', 'truck_rear',
                         'police_front', 'police_rear'):
            paths.add(f'/material/ui/lp/{name}/{material}.mat')

    # Some path-bearing attributes use engine conventions instead of literal
    # filenames. These rules are deliberately restricted to known keys.
    for value in re.findall(r'\bgps_path\s*:\s*"?(/[\w.!\[\],/+-]+)', text):
        paths.add(value if PurePosixPath(value).suffix else value + '.sii')
    for value in re.findall(
            r'\bcargo_custom_loads_path\s*:\s*"?(/[\w.!\[\],/+-]+)', text):
        # This attribute names a directory. Once its hash matches, normal
        # HashFS traversal discovers every custom-load definition below it.
        paths.add(value.rstrip('/'))
    for kind, value in re.findall(
            r'\bfallback(?:\[\])?\s*:\s*"([^"|]+)\|([^"|]+\.sii)"', text):
        kind = kind.strip().strip('/')
        value = value.strip().lstrip('/')
        if str(parent) not in ('', '.', '/'):
            paths.add(str(parent / kind / value))
            paths.add(str(parent / 'accessory' / kind / value))
        paths.add('/def/vehicle/' + kind + '/' + value)

    # UI and license-plate strings contain paths inside lightweight HTML.
    for value in re.findall(r'\b(?:src|face)=([^\s>]+)', text):
        if value.startswith('/'):
            paths.add(value.rstrip('"'))

    # Full unit parsing is only needed for rules which depend on a unit class
    # or on several attributes of the same unit. Most definition files are
    # handled by the fast expressions above.
    structured_classes = ('mod_package', 'overlay_def', 'license_plate_data')
    if any(re.search(rf'\b{name}\s*:', text) for name in structured_classes):
        try:
            sii = SiiFile.load(text, archive_path)
        except (ValueError, UnicodeError, EOFError, IndexError, TypeError):
            sii = None
    else:
        sii = None
    if sii is not None:
        license_country = None
        archive_parts = PurePosixPath(archive_path).parts
        if (len(archive_parts) >= 4 and archive_parts[-3] == 'country'
                and PurePosixPath(archive_path).name.startswith('license_plates')):
            license_country = archive_parts[-2]

        for unit in sii.units:
            class_name = unit.class_name
            attrs = unit.attributes
            if class_name == 'mod_package':
                description = attrs.get('description_file')
                if isinstance(description, str):
                    description_path = PurePosixPath(description)
                    suffix = description_path.suffix
                    base = (str(description_path)[:-len(suffix)]
                            if suffix else str(description_path))
                    for locale in MOD_DESCRIPTION_LOCALES:
                        paths.add(f'{base}.{locale}{suffix}')
            elif class_name == 'overlay_def':
                for key, values in attrs.items():
                    values = values if isinstance(values, list) else [values]
                    for value in values:
                        if not isinstance(value, str) or not value:
                            continue
                        paths.add(f'/material/overlay/{value}.mat')
                        if key == 'road_names':
                            paths.add(f'/material/ui/map/road/road_{value}.mat')
            elif class_name == 'license_plate_data' and license_country:
                plate_type = attrs.get('type')
                if plate_type is None:
                    continue
                plate_type = str(plate_type)
                if plate_type == 'trailer':
                    continue
                prefix = '' if plate_type == 'car' else plate_type + '_'
                front = attrs.get('background_front')
                rear = attrs.get('background_rear')
                front = str(front) if front is not None else prefix + 'front'
                rear = str(rear) if rear is not None else prefix + 'rear'
                paths.add(f'/material/ui/lp/{license_country}/{front}.mat')
                paths.add(f'/material/ui/lp/{license_country}/{rear}.mat')
    return paths


def find_self_path_candidates(data: bytes):
    """Yield conventional paths which an unnamed SII may have had.

    Most SII files only reference other assets and therefore cannot state
    their own filename.  A few engine-owned unit classes do, however, have a
    fixed directory and a stable filename vocabulary.  Keep these guesses
    narrow: the HashFS caller must still compare every candidate's hash with
    the entry being identified before accepting it.

    Mod authors frequently append a one- or two-character namespace to a
    stock-style filename.  Enumerating that small namespace is inexpensive
    and avoids embedding names of individual mods in the extractor.
    """
    try:
        text = _strip_comments(decode(data).decode('utf-8-sig', 'replace'))
    except (ValueError, UnicodeError, EOFError):
        return

    def separator_variants(value: str):
        """Yield conservative filename spellings used by SCS definition tools."""
        value = value.strip().strip('"').lower()
        if not value:
            yield ''
            return
        positions = [index for index, char in enumerate(value)
                     if char.isspace() or char == '.']
        # Sign editors freely preserve or replace individual separators.  The
        # names involved are short; cap the combinatorial part defensively.
        if len(positions) <= 10:
            for mask in range(1 << len(positions)):
                chars = list(value)
                for bit, index in enumerate(positions):
                    if mask & (1 << bit):
                        chars[index] = '_'
                yield ''.join(chars)
        yield re.sub(r'[\s.]+', '_', value)

    # Sign editor output carries a human filename in these properties even
    # when its HashFS directory listing has been removed.
    for property_name, parent in (
            ('board_name', '/def/sign/board'),
            ('template_name', '/def/sign/template')):
        for match in re.finditer(
                rf'(?m)^\s*{property_name}\s*:\s*'
                rf'(?:"([^"\r\n]*)"|([^\s#\r\n]+))', text):
            value = match.group(1) if match.group(1) is not None else match.group(2)
            for stem in separator_variants(value):
                yield f'{parent}/{stem}.sii'

    unit_headers = re.findall(
        r'(?m)^\s*([A-Za-z_]\w*)\s*:\s*([^\s{{]+)\s*\{', text)

    for unit_type, unit_name in unit_headers:
        lowered_type = unit_type.lower()
        lowered_name = unit_name.lower()
        if lowered_type == 'image_atlas' and lowered_name.startswith('atlas.'):
            atlas_stems = set(separator_variants(lowered_name[6:]))
            # Atlas exporters also use '-' for selected underscore-separated
            # numeric ranges (ip16_21 -> ip16-21).
            for stem in tuple(atlas_stems):
                positions = [index for index, char in enumerate(stem)
                             if char == '_']
                if len(positions) <= 8:
                    for mask in range(1 << len(positions)):
                        chars = list(stem)
                        for bit, index in enumerate(positions):
                            if mask & (1 << bit):
                                chars[index] = '-'
                        atlas_stems.add(''.join(chars))
            # Some atlases use a generic unit ID but retain their real family
            # in the material path.  Combine the exporter prefix, material
            # parent and material stem; the HashFS lookup validates the result.
            exporter = lowered_name[6:].split('_', 1)[0].split('.', 1)[0]
            atlas_name_match = re.search(
                r'(?m)^\s*atlas_name\s*:\s*"([^"\r\n]+)"', text,
                flags=re.IGNORECASE)
            if (atlas_name_match
                    and 'color' in atlas_name_match.group(1).lower()):
                for stem in tuple(atlas_stems):
                    if stem.endswith('_c'):
                        atlas_stems.add(stem[:-2] + '_color')
            for material in re.findall(
                    r'(?m)^\s*material(?:_path)?\s*:\s*"(/[^"\r\n]+\.mat)"',
                    text, flags=re.IGNORECASE):
                material_path = PurePosixPath(material)
                atlas_stems.add(
                    f'{exporter}_{material_path.parent.name}_{material_path.stem}')
            for stem in atlas_stems:
                yield f'/def/sign/atlas/{stem}.sii'
        elif (lowered_type == 'sign_frame_template'
              and lowered_name.startswith('frame.')):
            raw_stem = lowered_name[6:]
            stems = set(separator_variants(raw_stem))
            # Exporters often store a radius as r60/r220 in the unit name but
            # write r6/r22 (or r2.2) in the filename.
            for stem in tuple(stems):
                match = re.search(r'(r\d+)0$', stem)
                if match:
                    shortened = stem[:-1]
                    stems.add(shortened)
                    radius = match.group(1)[:-1]
                    if len(radius) > 1:
                        stems.add(stem[:match.start(1)] + radius[:-1]
                                  + '.' + radius[-1])
                match = re.search(r'r(\d{2})$', stem)
                if match:
                    stems.add(stem[:match.start(1)] + match.group(1)[0]
                              + '.' + match.group(1)[1])
            for stem in stems:
                yield f'/def/sign/frame/{stem}.sii'
        elif lowered_name.startswith('flare.beacon.'):
            stem = lowered_name[len('flare.'):]
            stem = re.sub(r'(?<=[a-z])(?=\d)', '_', stem)
            for variant in separator_variants(stem):
                yield f'/unit/hookup/{variant}.sii'
        elif lowered_type == 'light_source':
            for stem in separator_variants(lowered_name):
                yield f'/unit/hookup/{stem}.sii'
        elif (lowered_type == 'sign_editable_area'
              and lowered_name.startswith('sign.')):
            parts = lowered_name.split('.')
            if len(parts) >= 3 and not re.fullmatch(r'\d+x\d+', parts[1]):
                yield f'/unit/hookup/sign_area.{parts[1]}.sii'
        elif lowered_type == 'gate_trigger' and lowered_name.startswith('border.'):
            # Border hookups use small ordinal filenames while their unit ID
            # stores the country.  Hash verification chooses the real one.
            for index in range(1, 10):
                yield f'/unit/hookup/border{index}.sii'

    flare_units = re.findall(
        r'(?m)^\s*flare_vehicle\s*:\s*flare\.vehicle\.([A-Za-z0-9_.-]+)\s*$',
        text)
    if not flare_units:
        return

    # Names such as lp_side_lamp and lp_top_lamp describe a related group.
    # SCS stores these groups below unit/hookup/vehicle/flare, commonly as
    # vehicle_lp_lights[_xx].sii (older mods also use lamp/lamps).
    token_rows = [re.split(r'[_\-.]+', name.lower()) for name in flare_units]
    shared = []
    for columns in zip(*token_rows):
        if len(set(columns)) != 1:
            break
        shared.append(columns[0])
    group = '_'.join(shared) or token_rows[0][0]
    stems = {
        f'vehicle_{group}_light', f'vehicle_{group}_lights',
        f'vehicle_{group}_lamp', f'vehicle_{group}_lamps',
    }
    parent = '/unit/hookup/vehicle/flare'
    alphabet = ascii_lowercase + digits
    suffixes = ['']
    suffixes.extend('_' + char for char in alphabet)
    suffixes.extend('_' + ''.join(chars) for chars in product(alphabet, repeat=2))
    for stem in stems:
        for suffix in suffixes:
            yield f'{parent}/{stem}{suffix}.sii'


def find_mat_paths(data: bytes, archive_path: str) -> set[str]:
    parent = PurePosixPath(archive_path).parent
    result = set()
    for path in MatFile.load(data).texture_paths:
        result.add(path if path.startswith('/') else str(parent / path))
    # Materials can also reference controller definitions and auxiliary files
    # outside texture blocks (for example ``controller : "...sii"``).
    # Preserve relative-path semantics just like texture sources.
    text = data.decode('utf-8', 'replace')
    for match in re.finditer(
            r'["\']([^"\']+\.(?:sii|sui|tobj|dds|mat|soundref))["\']',
            text, flags=re.IGNORECASE):
        path = match.group(1).replace('\\', '/')
        result.add(path if path.startswith('/') else str(parent / path))
    return result


__all__ = [name for name in globals() if not name.startswith('_')]
