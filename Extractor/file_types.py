"""Content and extension inference used by deep extraction."""
from enum import Enum, auto
from pathlib import Path
from struct import unpack_from
import re

class FileType(Enum):
    UNKNOWN=auto(); BMP=auto(); DDS=auto(); FONT=auto(); FBX=auto(); INI=auto(); FSO=auto(); GIMP_XCF=auto(); JPEG=auto(); MAP_ROOT=auto(); MAP_BASE=auto(); MAP_AUX=auto(); MAP_SND=auto(); MAP_DATA=auto(); MAP_DESC=auto(); MAP_LAYER=auto(); MAP_SELECTION=auto(); MATERIAL=auto(); MAYA_ASCII_SCENE=auto(); MAYA_BINARY_SCENE=auto(); MAYA_FILE=auto(); MAYA_SWATCH=auto(); MAYA_SWATCHES=auto(); OGG=auto(); OPEN_EXR=auto(); PDN=auto(); PMA=auto(); PMC=auto(); PMD=auto(); PMG=auto(); PNG=auto(); PPD=auto(); PSD=auto(); RAR=auto(); REPLAY=auto(); RFX=auto(); SCT=auto(); SII=auto(); SOUND_BANK=auto(); SOUND_BANK_GUIDS=auto(); SOUND_REF=auto(); SUBSTANCE_DESIGNER_SOURCE=auto(); SUI=auto(); SVG=auto(); TGA_MASK=auto(); TOBJ=auto(); VSO=auto(); WX_WIDGETS_RESOURCE=auto(); ZLIB_BLOB=auto(); SEVEN_Z=auto()

EXT={'.7z':FileType.SEVEN_Z,'.bank':FileType.SOUND_BANK,'.bmp':FileType.BMP,'.dds':FileType.DDS,'.exr':FileType.OPEN_EXR,'.fbx':FileType.FBX,'.font':FileType.FONT,'.fso':FileType.FSO,'.guids':FileType.SOUND_BANK_GUIDS,'.bank.guids':FileType.SOUND_BANK_GUIDS,'.ini':FileType.INI,'.jpg':FileType.JPEG,'.mask':FileType.TGA_MASK,'.mat':FileType.MATERIAL,'.ma':FileType.MAYA_ASCII_SCENE,'.mb':FileType.MAYA_BINARY_SCENE,'.oga':FileType.OGG,'.ogg':FileType.OGG,'.ogv':FileType.OGG,'.pdn':FileType.PDN,'.pma':FileType.PMA,'.pmc':FileType.PMC,'.pmd':FileType.PMD,'.pmg':FileType.PMG,'.png':FileType.PNG,'.ppd':FileType.PPD,'.psd':FileType.PSD,'.rar':FileType.RAR,'.rfx':FileType.RFX,'.rpl':FileType.REPLAY,'.sbs':FileType.SUBSTANCE_DESIGNER_SOURCE,'.sct':FileType.SCT,'.sii':FileType.SII,'.vso':FileType.VSO,'.xcf':FileType.GIMP_XCF,'.xrc':FileType.WX_WIDGETS_RESOURCE,'.soundref':FileType.SOUND_REF,'.swatch':FileType.MAYA_SWATCH,'.swatches':FileType.MAYA_SWATCHES,'.sui':FileType.SUI,'.svg':FileType.SVG,'.tobj':FileType.TOBJ,'.mbd':FileType.MAP_ROOT,'.base':FileType.MAP_BASE,'.aux':FileType.MAP_AUX,'.snd':FileType.MAP_SND,'.data':FileType.MAP_DATA,'.desc':FileType.MAP_DESC,'.layer':FileType.MAP_LAYER,'.sbd':FileType.MAP_SELECTION}

def extension_to_file_type(extension):
    extension=extension.lower()
    return FileType.MAYA_ASCII_SCENE if extension.startswith('.maa') else EXT.get(extension,FileType.UNKNOWN)
def path_to_file_type(path):
    if Path(path).suffix.lower()=='.dat' and 'glass' in path:return FileType.SUI
    if path.lower().endswith('.bank.guids'):return FileType.SOUND_BANK_GUIDS
    return extension_to_file_type(Path(path).suffix)
def file_type_to_extension(kind):
    return next((ext for ext,value in EXT.items() if value==kind),'')
def infer(data:bytes):
    if not data:return FileType.UNKNOWN
    signatures=[(b'BM',FileType.BMP),(b'DDS',FileType.DDS),(b'v/1\x01',FileType.OPEN_EXR),(b'Kaydara FBX Binary',FileType.FBX),(b'# SCS Font',FileType.FONT),(b'\xff\xd8',FileType.JPEG),(b'FOR4',FileType.MAYA_FILE),(b'OggS',FileType.OGG),(b'PDN',FileType.PDN),(b'\x89PNG\r\n\x1a\n',FileType.PNG),(b'8BPS',FileType.PSD),(b'Rar!',FileType.RAR),(b'RIFF',FileType.SOUND_BANK),(b'gimp xcf file',FileType.GIMP_XCF),(b'7z\xbc\xaf\x27\x1c',FileType.SEVEN_Z)]
    for magic,kind in signatures:
        if data.startswith(magic):return kind
    if data[:4] in (b'SiiN',b'ScsC') or data[:3]==b'3nK' or data.startswith(b'\xef\xbb\xbfS'):return FileType.SII
    if data[:10].decode('utf-8','ignore').lstrip('\ufeff').startswith(('material','effect')):return FileType.MATERIAL
    if len(data)>18 and data[-18:-2]==b'TRUEVISION-XFILE':return FileType.TGA_MASK
    if len(data)>=4:
        version=unpack_from('<I',data)[0]
        if version==0x70b10a01:return FileType.TOBJ
        if version==6:return FileType.PMC
        if 0x15<=version<=0x19:return FileType.PPD
        if version in (4,5) and len(data)>=16:
            count=unpack_from('<I',data,12)[0]
            if version==4 and count<=5000:return FileType.PMD
            if count==0 or count>5000:return FileType.PMA
    if len(data)>4 and data[1:4]==b'gmP':return FileType.PMG
    if len(data)>2 and data[:2] in (b'x\xda',b'x\x9c',b'x^',b'x\x01'):return FileType.ZLIB_BLOB
    if len(data)<=10000 and any(line.startswith('source=') for line in data.decode('utf-8','ignore').strip('\ufeff\0').splitlines()):return FileType.SOUND_REF
    if len(data)>38 and re.match(rb'\{?[0-9a-fA-F]{8}-[0-9a-fA-F-]{27}\}?',data):return FileType.SOUND_BANK_GUIDS
    return FileType.UNKNOWN
