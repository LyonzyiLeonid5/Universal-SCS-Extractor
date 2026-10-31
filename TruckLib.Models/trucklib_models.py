"""Model-related readers needed by the Python extractor."""
from __future__ import annotations

from dataclasses import dataclass, field
from struct import pack, unpack, unpack_from
from enum import IntEnum, IntFlag
from pathlib import Path
from copy import deepcopy
from io import BytesIO
import re

try:
    from trucklib_core import Token, read_exact, read_vector3, read_vector4, read_matrix4x4, read_quaternion, write_vector, write_quaternion
except ImportError:
    Token = None

class TobjType(IntEnum): MAP_1D=1; MAP_2D=2; MAP_3D=3; CUBE_MAP=5
class TobjFilter(IntEnum): NEAREST=0; LINEAR=1; NO_MIPS=2; DEFAULT=3
class TobjMipFilter(IntEnum): NEAREST=0; TRILINEAR=1; NO_MIPS=2; DEFAULT=3
class TobjAddr(IntEnum): REPEAT=0; CLAMP=1; CLAMP_TO_EDGE=2; CLAMP_TO_BORDER=3; MIRROR=4; MIRROR_CLAMP=5; MIRROR_CLAMP_TO_EDGE=6
class TobjColorSpace(IntEnum): SRGB=0; LINEAR=1


@dataclass
class Tobj:
    texture_path: str
    prefix: bytes = bytes(40)
    suffix: bytes = b''

    SUPPORTED_VERSION = 0x70B10A01

    @classmethod
    def load(cls, data: bytes) -> 'Tobj':
        if len(data) < 48 or unpack_from('<I', data)[0] != 0x70b10a01:
            raise ValueError('Unsupported .tobj format')
        length = unpack_from('<Q', data, 40)[0]
        if length > len(data) - 48:
            raise ValueError('Truncated .tobj texture path')
        path = data[48:48 + length].decode('utf-8')
        return cls(path, data[:40], data[48 + length:])

    def serialize(self) -> bytes:
        encoded = self.texture_path.encode('utf-8')
        prefix = bytearray(self.prefix)
        if len(prefix) != 40: raise ValueError('TOBJ header must be 40 bytes')
        prefix[:4] = pack('<I', self.SUPPORTED_VERSION)
        return bytes(prefix) + pack('<Q', len(encoded)) + encoded + self.suffix

    @classmethod
    def open(cls, path: str) -> 'Tobj': return cls.load(Path(path).read_bytes())
    def save(self, path: str) -> None: Path(path).write_bytes(self.serialize())
    @property
    def bias(self): return self.prefix[22]
    @bias.setter
    def bias(self,v): self._set(22,v)
    @property
    def type(self): return TobjType(self.prefix[24])
    @type.setter
    def type(self,v): self._set(24,int(v))
    @property
    def mag_filter(self): return TobjFilter(self.prefix[26])
    @mag_filter.setter
    def mag_filter(self,v): self._set(26, 3 if int(v)==1 else int(v))
    @property
    def min_filter(self): return TobjFilter(self.prefix[27])
    @min_filter.setter
    def min_filter(self,v): self._set(27, 3 if int(v)==1 else int(v))
    @property
    def mip_filter(self): return TobjMipFilter(self.prefix[28])
    @mip_filter.setter
    def mip_filter(self,v): self._set(28, 3 if int(v)==1 else int(v))
    @property
    def addr_u(self): return TobjAddr(self.prefix[30])
    @addr_u.setter
    def addr_u(self,v): self._set(30,int(v))
    @property
    def addr_v(self): return TobjAddr(self.prefix[31])
    @addr_v.setter
    def addr_v(self,v): self._set(31,int(v))
    @property
    def addr_w(self): return TobjAddr(self.prefix[32])
    @addr_w.setter
    def addr_w(self,v): self._set(32,int(v))
    @property
    def compress(self): return self.prefix[33] != 1
    @compress.setter
    def compress(self,v): self._set(33, int(not v))
    @property
    def anisotropic(self): return self.prefix[35] != 1
    @anisotropic.setter
    def anisotropic(self,v): self._set(35, int(not v))
    @property
    def color_space(self): return TobjColorSpace(self.prefix[38])
    @color_space.setter
    def color_space(self,v): self._set(38,int(v))
    def _set(self,index,value):
        data=bytearray(self.prefix); data[index]=value; self.prefix=bytes(data)

def cstring_bytes_to_list(data: bytes, encoding='utf-8') -> list[str]:
    return [part.decode(encoding) for part in data.split(b'\0') if part]
def list_to_cstring_byte_list(strings: list[str], encoding='utf-8') -> list[bytes]:
    return [value.encode(encoding)+b'\0' for value in strings]

@dataclass
class AxisAlignedBox:
    start: tuple[float,float,float]=(0.,0.,0.); end: tuple[float,float,float]=(0.,0.,0.)
    @classmethod
    def read(cls,s): return cls(read_vector3(s),read_vector3(s))
    def write(self,s): write_vector(s,self.start); write_vector(s,self.end)

@dataclass
class Triangle:
    a:int=0; b:int=0; c:int=0
    def invert_order(self): self.a,self.c=self.c,self.a
    @classmethod
    def read(cls,s): return cls(*unpack('<3H',read_exact(s,6)))
    def write(self,s): s.write(pack('<3H',self.a,self.b,self.c))

@dataclass
class Vertex:
    position: tuple[float,float,float]=(0.,0.,0.); normal: tuple[float,float,float]=(0.,0.,0.)
    tangent: tuple[float,float,float,float]|None=None; color: tuple[int,int,int,int]=(0,0,0,0)
    secondary_color: tuple[int,int,int,int]|None=None; texture_coordinates:list[tuple[float,float]]|None=None
    bone_indexes:bytes|None=None; bone_weights:bytes|None=None
    def clone(self): return deepcopy(self)

@dataclass
class Locator:
    name: object=0; position:tuple[float,float,float]=(0.,0.,0.); scale:float=0.; rotation:tuple[float,float,float,float]=(0.,0.,0.,1.); hookup_offset:int=0
    @classmethod
    def read(cls,s):
        name=Token(unpack('<Q',read_exact(s,8))[0]); pos=read_vector3(s); scale=unpack('<f',read_exact(s,4))[0]; rot=read_quaternion(s); off=unpack('<i',read_exact(s,4))[0]
        return cls(name,pos,scale,rot,off)
    def write(self,s): s.write(pack('<Q',int(self.name))); write_vector(s,self.position); s.write(pack('<f',self.scale)); write_quaternion(s,self.rotation); s.write(pack('<i',self.hookup_offset))

@dataclass
class Bone:
    name:object=0; transformation:tuple=tuple([0.]*16); transformation_reserved:tuple=tuple([0.]*16)
    stretch:tuple=(0.,0.,0.,1.); rotation:tuple=(0.,0.,0.,1.); translation:tuple=(0.,0.,0.); scale:tuple=(1.,1.,1.); determinant_sign:float=1.; parent:int=-1
    @classmethod
    def read(cls,s):
        return cls(Token(unpack('<Q',read_exact(s,8))[0]),read_matrix4x4(s),read_matrix4x4(s),read_quaternion(s),read_quaternion(s),read_vector3(s),read_vector3(s),unpack('<f',read_exact(s,4))[0],unpack('<i',read_exact(s,4))[0])
    def write(self,s):
        s.write(pack('<Q',int(self.name))); write_vector(s,self.transformation); write_vector(s,self.transformation_reserved); write_quaternion(s,self.stretch); write_quaternion(s,self.rotation); write_vector(s,self.translation); write_vector(s,self.scale); s.write(pack('<fi',self.determinant_sign,self.parent))

@dataclass
class PartAttribute: type:int=0; tag:object=0; value:int=0
@dataclass
class Look: name:object=0; materials:list[str]=field(default_factory=list)
@dataclass
class Variant: name:object=0; attributes:list[PartAttribute]=field(default_factory=list)
@dataclass
class Part:
    name:object=field(default_factory=lambda: Token('untitled') if Token else 0); pieces:list=field(default_factory=list); locators:list[Locator]=field(default_factory=list)

class Model:
    """PMD/PMG container. Unknown fields are retained byte-for-byte on round-trip."""
    def __init__(self):
        self.pmd_bytes=b''; self.pmg_bytes=b''; self.looks=[]; self.variants=[]; self.parts=[]; self.pieces=[]; self.locators=[]; self.bones=[]; self.bounding_box=AxisAlignedBox()
    @classmethod
    def load(cls,pmd:bytes,pmg:bytes):
        obj=cls(); obj.pmd_bytes=bytes(pmd); obj.pmg_bytes=bytes(pmg)
        if pmd: obj._read_pmd()
        if pmg: obj._read_pmg_header()
        return obj
    @classmethod
    def open(cls,pmd_path:str,fs=None):
        pmg_path=str(Path(pmd_path).with_suffix('.pmg'))
        if fs is None: return cls.load(Path(pmd_path).read_bytes(),Path(pmg_path).read_bytes())
        return cls.load(fs.read_all_bytes(pmd_path),fs.read_all_bytes(pmg_path))
    def serialize_pmd(self,stream=None):
        if stream is not None: stream.write(self.pmd_bytes)
        return self.pmd_bytes
    def serialize_pmg(self,stream=None):
        if stream is not None: stream.write(self.pmg_bytes)
        return self.pmg_bytes
    def save(self,directory,name):
        Path(directory).mkdir(parents=True,exist_ok=True); Path(directory,f'{name}.pmd').write_bytes(self.pmd_bytes); Path(directory,f'{name}.pmg').write_bytes(self.pmg_bytes)
    def _read_pmd(self):
        data=self.pmd_bytes
        if len(data)<64 or unpack_from('<I',data)[0]!=4: raise ValueError('Unsupported PMD version')
        material_count,look_count,_piece_count,variant_count,part_count,attrib_count=unpack_from('<6I',data,4)
        material_paths_offset=unpack_from('<I',data,60)[0]; pos=64
        self.looks=[Look(Token(unpack_from('<Q',data,pos+i*8)[0])) for i in range(look_count)]; pos+=look_count*8
        self.variants=[Variant(Token(unpack_from('<Q',data,pos+i*8)[0])) for i in range(variant_count)]; pos+=variant_count*8+part_count*8
        headers=[]
        for _ in range(attrib_count):
            tag,kind,_offset=unpack_from('<Qii',data,pos); pos+=16; headers.append((Token(tag),kind))
        for variant in self.variants:
            variant.attributes=[PartAttribute(kind,tag,unpack_from('<I',data,pos+i*4)[0]) for i,(tag,kind) in enumerate(headers)]; pos+=attrib_count*4
        materials=cstring_bytes_to_list(data[material_paths_offset:])
        for i,look in enumerate(self.looks): look.materials=materials[i*material_count:(i+1)*material_count]
    def _read_pmg_header(self):
        data=self.pmg_bytes
        if len(data)<88 or data[0]!=21 or data[1:4]!=b'gmP': raise ValueError('Unsupported PMG version')
        piece_count,part_count,bone_count,_weight_width,locator_count=unpack_from('<IIIiI',data,4); pos=32
        self.bounding_box_center=unpack_from('<3f',data,pos); pos+=12; self.bounding_box_diagonal_size=unpack_from('<f',data,pos)[0]; pos+=4
        self.bounding_box=AxisAlignedBox(unpack_from('<3f',data,pos),unpack_from('<3f',data,pos+12)); pos+=24
        skeleton_offset,parts_offset,locators_offset,pieces_offset=unpack_from('<4I',data,pos)
        s=BytesIO(data); s.seek(skeleton_offset); self.bones=[Bone.read(s) for _ in range(bone_count)]
        s.seek(locators_offset); locators=[Locator.read(s) for _ in range(locator_count)]
        s.seek(parts_offset)
        for _ in range(part_count):
            name=Token(unpack('<Q',read_exact(s,8))[0]); pc,pi,lc,li=unpack('<4I',read_exact(s,16)); self.parts.append(Part(name,[],locators[li:li+lc]))

class AllowedVehicles(IntEnum): PLAYER_ONLY=0; SMALL_VEHICLES=1; LARGE_VEHICLES=2; ALL_VEHICLES=3
class BlinkerType(IntFlag): NO_BLINKER=0; NO_BLINKER_FORCED=1; RIGHT_BLINKER=2; LEFT_BLINKER=4
class SemaphoreType(IntEnum): USE_PROFILE=0; MODEL_ONLY=1; TRAFFIC_LIGHT=2; TRAFFIC_LIGHT_MINOR=3; TRAFFIC_LIGHT_MAJOR=4; BARRIER_MANUAL_TIMED=5; BARRIER_DISTANCE_ACTIVATED=6; TRAFFIC_LIGHT_BLOCKABLE=7; BARRIER_GAS=8; TRAFFIC_LIGHT_VIRTUAL=9; BARRIER_AUTOMATIC=10
class SpawnPointType(IntEnum): NONE=0; TRAILER=1; UNLOAD_EASY=2; GAS_STATION=3; SERVICE_STATION=4; TRUCK_STOP=5; WEIGH_STATION=6; TRUCK_DEALER=7; HOTEL=8; CUSTOM=9; PARKING=10; TASK=11; MEET=12; COMPANY_POINT=13; GARAGE_POINT=14; BUY_POINT=15; RECRUITMENT=16; CAMERA_POINT=17; BUS_STATION=18; UNLOAD_MEDIUM=19; UNLOAD_HARD=20; UNLOAD_RIGID=21; WEIGH_STATION_CAT=22; COMPANY_UNLOAD=23; TRAILER_SPAWN=24; LONG_TRAILER=25
class RoadSize(IntEnum): ONE_WAY=0; ONE_LANE=1; TWO_LANES=2; THREE_LANES=3; FOUR_LANES=4; TWO_LANE_SPLIT=5; THREE_LANE_SPLIT=6; FOUR_LANE_SPLIT=7; THREE_LANES_ONE_WAY=8; POLYGON=13; AUTO=14
class RoadOffset(IntEnum): METERS_0=0; METERS_1=1; METERS_2=2; METERS_5=3; METERS_10=4; METERS_15=5; METERS_20=6; METERS_25=7
class CustomColor(IntEnum): NONE=0; LIGHT=1; DARK=2; GREEN=4
class NavNodeType(IntEnum): PHYSICAL_NODE=0; AI_NODE=1

@dataclass
class TerrainPoint: position:tuple; normal:tuple
@dataclass
class TerrainPointVariant: start:int; length:int
@dataclass
class ControlNode:
    position:tuple=(0.,0.,0.); direction:tuple=(0.,0.,0.); input_lines:list[int]=field(default_factory=lambda:[0]*8); output_lines:list[int]=field(default_factory=lambda:[0]*8); terrain_points:dict[int,list[TerrainPoint]]=field(default_factory=dict); terrain_point_index:int=0; terrain_point_count:int=0; terrain_point_variant_index:int=0; terrain_point_variant_count:int=0
    @classmethod
    def read(cls,s):
        indices=unpack('<4I',read_exact(s,16)); pos=read_vector3(s); direction=read_vector3(s); inputs=list(unpack('<8i',read_exact(s,32))); outputs=list(unpack('<8i',read_exact(s,32)))
        return cls(pos,direction,inputs,outputs,{},*indices)
@dataclass
class Semaphore:
    profile:object=0; type:SemaphoreType=SemaphoreType.USE_PROFILE; position:tuple=(0.,0.,0.); rotation:tuple=(0.,0.,0.,1.); semaphore_id:int=0; intervals:tuple=(0.,0.,0.,0.); cycle_delay:float=0.
    @classmethod
    def read(cls,s,version):
        pos=read_vector3(s); rot=read_quaternion(s); kind,ident=unpack('<II',read_exact(s,8)); intervals=read_vector4(s); delay=unpack('<f',read_exact(s,4))[0]; profile=Token(unpack('<Q',read_exact(s,8))[0]); read_exact(s,4)
        if version==0x19:read_exact(s,16)
        return cls(profile,SemaphoreType(kind),pos,rot,ident,intervals,delay)
@dataclass
class SpawnPoint:
    position:tuple=(0.,0.,0.); rotation:tuple=(0.,0.,0.,1.); type:SpawnPointType=SpawnPointType.NONE; flags:int=0
    @classmethod
    def read(cls,s,version):
        pos=read_vector3(s); rot=read_quaternion(s); kind=unpack('<I',read_exact(s,4))[0]; flags=unpack('<I',read_exact(s,4))[0] if version>=0x18 else 0
        return cls(pos,rot,SpawnPointType(kind),flags)
@dataclass
class Sign: name:object=0; position:tuple=(0.,0.,0.); rotation:tuple=(0.,0.,0.,1.); model:object=0; part:object=0
@dataclass
class Intersection: curve_id:int=0; position:float=0.; radius:float=0.; flags:int=0
@dataclass
class MapPoint: position:tuple=(0.,0.,0.); neighbors:list[int]=field(default_factory=list); visibility_flags:int=0; navigation_flags:int=0
@dataclass
class TriggerPoint: trigger_id:int=0; action:object=0; position:tuple=(0.,0.,0.); range:float=0.; reset_delay:float=0.; reset_distance:float=0.; neighbors:list[int]=field(default_factory=lambda:[0,0]); flags:int=0
@dataclass
class NavNodeConnectionInfo: target_node_index:int=65535; length:float=3.4028235e38; curve_indices:list[int]=field(default_factory=list)
@dataclass
class NavNode: type:NavNodeType=NavNodeType.PHYSICAL_NODE; index:int=0; connections:list[NavNodeConnectionInfo]=field(default_factory=list)
@dataclass
class NavCurve: next_lines:list[int]=field(default_factory=list); previous_lines:list[int]=field(default_factory=list)

class PrefabDescriptor:
    def __init__(self):
        self.version=0; self.nodes=[]; self.nav_curves=[]; self.signs=[]; self.semaphores=[]; self.spawn_points=[]; self.map_points=[]; self.trigger_points=[]; self.intersections=[]; self.nav_nodes=[]; self._raw=b''
    @classmethod
    def open(cls,path,fs=None): return cls.load(Path(path).read_bytes() if fs is None else fs.read_all_bytes(path))
    @classmethod
    def load(cls,data):
        if hasattr(data,'read'): data=data.read()
        obj=cls(); obj._raw=bytes(data); s=BytesIO(data); obj.version=unpack('<I',read_exact(s,4))[0]
        if obj.version not in range(0x15,0x1a): raise ValueError(f'Version {obj.version} is not supported')
        count_len=10 if obj.version==0x15 else 11; counts=unpack('<'+'I'*count_len,read_exact(s,4*count_len)); offsets=unpack('<'+'I'*(count_len+1),read_exact(s,4*(count_len+1)))
        node_count,nav_count,sign_count,sem_count,spawn_count,terrain_count,variant_count,map_count,trigger_count,intersection_count=counts[:10]; nav_node_count=counts[10] if count_len==11 else 0
        s.seek(offsets[0]); obj.nodes=[ControlNode.read(s) for _ in range(node_count)]
        # Offset table makes independent sections parseable even when a structure evolves.
        s.seek(offsets[3]); obj.semaphores=[Semaphore.read(s,obj.version) for _ in range(sem_count)]
        s.seek(offsets[4]); obj.spawn_points=[SpawnPoint.read(s,obj.version) for _ in range(spawn_count)]
        s.seek(offsets[5]); positions=[read_vector3(s) for _ in range(terrain_count)]
        s.seek(offsets[6]); normals=[read_vector3(s) for _ in range(terrain_count)]
        s.seek(offsets[7]); variants=[TerrainPointVariant(*unpack('<II',read_exact(s,8))) for _ in range(variant_count)]
        for node in obj.nodes:
            if node.terrain_point_variant_count:
                for idx,mapping in enumerate(variants[node.terrain_point_variant_index:node.terrain_point_variant_index+node.terrain_point_variant_count]):
                    start=node.terrain_point_index+mapping.start; node.terrain_points[idx]=[TerrainPoint(positions[i],normals[i]) for i in range(start,start+mapping.length)]
            else:
                start=node.terrain_point_index; node.terrain_points[0]=[TerrainPoint(positions[i],normals[i]) for i in range(start,start+node.terrain_point_count)]
        return obj
    def serialize(self,stream=None):
        # Unmodified descriptors retain every currently unknown field exactly.
        if stream is not None: stream.write(self._raw)
        return self._raw
    def save(self,path): Path(path).write_bytes(self.serialize())


def find_model_paths(data: bytes) -> set[str]:
    """Read PMD v4 look material paths; fall back to scanning unknown versions."""
    if len(data) >= 64 and unpack_from('<I', data)[0] == 4:
        material_count, look_count = unpack_from('<II', data, 4)
        material_paths_offset = unpack_from('<I', data, 60)[0]
        if material_count * look_count <= 100_000 and material_paths_offset < len(data):
            names = data[material_paths_offset:].split(b'\0')
            if len(names) > material_count * look_count:
                return {name.decode('ascii', 'replace') for name in
                        names[:material_count * look_count] if name}
    # Older PMD variants keep look-material names as NUL-terminated strings
    # but do not expose the v4 table offset.  A material can be relative to
    # the model (``paint.mat``), not only absolute.  Returning it lets the
    # extractor resolve the reference against the already verified PMD path.
    return {
        value.decode('utf-8', 'ignore')
        for value in data.split(b'\0')
        if re.fullmatch(rb'(?:/?[A-Za-z0-9_ +./\-]+)\.mat', value,
                        flags=re.IGNORECASE)
    }


__all__ = [name for name in globals() if not name.startswith('_')]
