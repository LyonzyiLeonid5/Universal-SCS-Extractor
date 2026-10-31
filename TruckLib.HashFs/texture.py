"""Recreate packed HashFS v2 texture objects and DDS payloads."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import PurePosixPath
from struct import pack, unpack

import sys
from pathlib import Path
_GIS = Path(__file__).resolve().parent.parent / 'GisDeflate'
if str(_GIS) not in sys.path:
    sys.path.insert(0, str(_GIS))

from gisdeflate import decompress as decompress_gdeflate


@dataclass(frozen=True)
class TextureMeta:
    width: int
    height: int
    image_flags: int
    sample_flags: int

    @classmethod
    def parse(cls, data: bytes) -> 'TextureMeta':
        width, height, image_flags, sample_flags = unpack('<HHII', data)
        return cls(width + 1, height + 1, image_flags, sample_flags)

    @property
    def mip_count(self) -> int:
        return (self.image_flags & 15) + 1

    @property
    def format(self) -> int:
        return (self.image_flags >> 4) & 255

    @property
    def cube(self) -> bool:
        return bool((self.image_flags >> 12) & 3)

    @property
    def faces(self) -> int:
        return ((self.image_flags >> 14) & 63) + 1

    @property
    def pitch_alignment(self) -> int:
        return 1 << ((self.image_flags >> 20) & 15)

    @property
    def image_alignment(self) -> int:
        return 1 << ((self.image_flags >> 24) & 15)


def tobj(meta: TextureMeta, path: str) -> bytes:
    dds_path = str(PurePosixPath(path).with_suffix('.dds')).encode('utf-8')
    header = bytearray(40)
    header[:4] = pack('<I', 0x70b10a01)
    header[20:22] = pack('<H', 1)
    header[24] = 5 if meta.cube else 2
    header[26] = 3 if meta.sample_flags & 1 else 0
    header[27] = 3 if meta.sample_flags & 2 else 0
    mip_filter = (meta.sample_flags >> 2) & 3
    header[28] = 3 if mip_filter == 1 else mip_filter
    header[30] = (meta.sample_flags >> 4) & 7
    header[31] = (meta.sample_flags >> 7) & 7
    header[32] = (meta.sample_flags >> 10) & 7
    header[37] = 1  # unknown10
    return bytes(header) + pack('<Q', len(dds_path)) + dds_path


def _align(value: int, alignment: int) -> int:
    return (value + alignment - 1) // alignment * alignment


def _surface(width: int, height: int, fmt: int) -> tuple[int, int]:
    if fmt in (70, 71, 72, 79, 80, 81):
        row = max(1, (width + 3) // 4) * 8
        return row, row * max(1, (height + 3) // 4)
    if fmt in (*range(73, 79), *range(82, 85), *range(94, 100)):
        row = max(1, (width + 3) // 4) * 16
        return row, row * max(1, (height + 3) // 4)
    bytes_per_pixel = 4 if fmt in (27, 28, 29, 30, 31, 32, 87, 88, 90, 91, 92, 93) else 1
    row = width * bytes_per_pixel
    return row, row * height


def dds(meta: TextureMeta, packed: bytes, compressed: bool) -> bytes:
    surface = decompress_gdeflate(packed) if compressed else packed
    output = bytearray()
    use_raw_surface = (meta.width <= 0 or meta.height <= 0 or meta.mip_count <= 0
                       or meta.faces <= 0 or meta.format <= 0
                       or meta.pitch_alignment <= 0 or meta.image_alignment <= 0)
    source_offset = 0
    for _face in range(meta.faces if not use_raw_surface else 0):
        width, height = meta.width, meta.height
        for _mip in range(meta.mip_count):
            source_offset = _align(source_offset, meta.image_alignment)
            row_size, slice_size = _surface(width, height, meta.format)
            if row_size <= 0 or slice_size <= 0:
                use_raw_surface = True
                break
            for _ in range(slice_size // row_size):
                source_offset = _align(source_offset, meta.pitch_alignment)
                end = source_offset + row_size
                if end > len(surface):
                    use_raw_surface = True
                    break
                output.extend(surface[source_offset:end])
                source_offset = end
            if use_raw_surface:
                break
            width, height = max(1, width // 2), max(1, height // 2)
        if use_raw_surface:
            break
    if use_raw_surface:
        output = bytearray(surface)
    flags = 0x1007 | (0x20000 if meta.mip_count > 1 else 0)
    caps = 0x1000 | (0x400008 if meta.mip_count > 1 else 0)
    caps2 = 0xfe00 if meta.cube else 0
    if meta.cube:
        caps |= 8
    header = bytearray()
    header += b'DDS '
    header += pack('<7I', 124, flags, meta.height, meta.width, 0, 0, meta.mip_count)
    header += bytes(44)
    header += pack('<II4s5I', 32, 4, b'DX10', 0, 0, 0, 0, 0)
    header += pack('<5I', caps, caps2, 0, 0, 0)
    header += pack('<5I', meta.format, 3, 4 if meta.cube else 0, 1, 0)
    if len(header) != 148:
        raise AssertionError('Invalid DDS header length')
    return bytes(header) + output


@dataclass
class DdsFile:
    """DDS header and payload with lossless load/serialize support."""
    width: int
    height: int
    mip_count: int
    format: int | None
    data: bytes
    header: bytes
    cube: bool = False
    array_size: int = 1

    @classmethod
    def load(cls, source) -> 'DdsFile':
        raw = source.read() if hasattr(source, 'read') else bytes(source)
        if len(raw) < 128 or raw[:4] != b'DDS ' or unpack('<I', raw[4:8])[0] != 124:
            raise ValueError('Not a DDS file')
        height, width, mip_count = unpack('<III', raw[12:20] + raw[28:32])
        fourcc = raw[84:88]
        dx10 = fourcc == b'DX10'
        if dx10:
            if len(raw) < 148: raise ValueError('Truncated DX10 DDS')
            fmt, _dimension, misc, array_size, _misc2 = unpack('<5I', raw[128:148]); header_size = 148
        else:
            fmt=None; misc=0; array_size=1; header_size=128
        caps2=unpack('<I',raw[112:116])[0]
        return cls(width,height,mip_count or 1,fmt,raw[header_size:],raw[:header_size],bool(caps2&0x200 or misc&4),array_size)
    @classmethod
    def open(cls,path): return cls.load(Path(path).read_bytes())
    def serialize(self): return self.header+self.data
    def save(self,path): Path(path).write_bytes(self.serialize())

def convert_surface_data(dds_file: DdsFile, pitch_alignment=256, image_alignment=512) -> bytes:
    if dds_file.format is None: raise ValueError('DDS is not in DX10 format')
    out=bytearray(); src=0; faces=6 if dds_file.cube else 1
    for _ in range(faces):
        width,height=dds_file.width,dds_file.height
        for _ in range(dds_file.mip_count):
            while len(out)%image_alignment: out.append(0)
            row,total=_surface(width,height,dds_file.format)
            for _ in range(total//row):
                while len(out)%pitch_alignment: out.append(0)
                if src+row>len(dds_file.data): raise EOFError('DDS surface is truncated')
                out += dds_file.data[src:src+row]; src += row
            width,height=max(1,width//2),max(1,height//2)
    return bytes(out)

def texture_metadata(tobj_bytes: bytes, dds_bytes: bytes) -> tuple[TextureMeta, bytes]:
    # Header byte positions are stable in TOBJ 0x70b10a01.
    if len(tobj_bytes)<48 or unpack('<I',tobj_bytes[:4])[0]!=0x70b10a01: raise ValueError('Unsupported TOBJ')
    image=DdsFile.load(dds_bytes)
    if image.format is None: raise ValueError('DDS is not in DX10 format')
    mip=max(1,image.mip_count); faces=6 if image.cube else 1
    flags=(mip-1)|(image.format<<4)|((1 if image.cube else 0)<<12)|((faces-1)<<14)|(8<<20)|(9<<24)
    mag=1 if tobj_bytes[26] in (1,3) else 0; mini=1 if tobj_bytes[27] in (1,3) else 0; mipfilter=1 if tobj_bytes[28]==3 else tobj_bytes[28]
    samples=mag|(mini<<1)|(mipfilter<<2)|(tobj_bytes[30]<<4)|(tobj_bytes[31]<<7)|(tobj_bytes[32]<<10)
    meta=TextureMeta(image.width,image.height,flags,samples)
    return meta,convert_surface_data(image)
