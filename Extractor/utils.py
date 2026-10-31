"""Ports of ExtractorFork BinaryUtils, PathUtils, TextUtils and Tree helpers."""
from __future__ import annotations
from dataclasses import dataclass, field
from pathlib import Path
import re

PROBLEMATIC_CONTROL_CHARS='\x07\x08\x09\x0a\x0d\x1b\u200b'
PROBLEMATIC_UNICODE_CHARS='\u00a0\u00ad\u034f\u061c\u180e\u2000\u2001\u2002\u2003\u2004\u2005\u2006\u2007\u2008\u2009\u200a\u200b\u200c\u200d\u202f\u205f\u2060\u2061\u2062\u2063\u2064\u2065\u2800\u3000\ufeff'
INVALID_PATH_CHARS='<>:"\\|?*'+PROBLEMATIC_UNICODE_CHARS
RESERVED_NAMES={'con','prn','aux','nul',*(f'com{i}' for i in range(1,10)),*(f'lpt{i}' for i in range(1,10)),'com¹','com²','com³','lpt¹','lpt²','lpt³'}

def find_bytes_backwards(reader,pattern:bytes,max_bytes=50_000_800):
    original=reader.tell(); end=reader.seek(0,2); start=max(0,end-max_bytes); reader.seek(start); data=reader.read(end-start); found=data.rfind(pattern)
    if found<0: reader.seek(original); return -1
    reader.seek(start+found); return start+found
def copy_stream(input_stream,output_stream,count):
    while count:
        data=input_stream.read(min(32768,count))
        if not data: raise EOFError(f'Stream ended {count} bytes early')
        output_stream.write(data); count-=len(data)
def replace_chars(value,chars,replacement): return ''.join(replacement if c in chars else c for c in value)
def replace_control_chars(value): return replace_chars(value,PROBLEMATIC_CONTROL_CHARS,'�')
def replace_chars_unambiguously(value,chars): return ''.join(f'x{ord(c):02X}' if c in chars else c for c in value)
def append_before_extension(path,addition):
    dot=path.rfind('.'); return path+addition if dot<0 else path[:dot]+addition+path[dot:]
def sanitize_path(path,invalid_path_chars=None,is_windows=None):
    result=replace_chars_unambiguously(path,INVALID_PATH_CHARS if invalid_path_chars is None else invalid_path_chars); windows=__import__('os').name=='nt' if is_windows is None else is_windows; parts=result.split('/')
    for i,part in enumerate(parts):
        if part=='..': parts[i]='__'
        elif windows and Path(part).stem.lower() in RESERVED_NAMES: parts[i]=append_before_extension(part,'_')
    return '/'.join(parts)
def remove_initial_slash(path): return path[1:] if path.startswith('/') and path!='/' else path
def remove_trailing_slash(path): return path[:-1] if path.endswith('/') and path!='/' else path
def ensure_initial_slash(path): return path if path.startswith('/') else '/'+path
def remove_non_ascii_or_invalid_chars(value): return ''.join(c for c in value if c.isascii() and c.isprintable() and c not in INVALID_PATH_CHARS)
def get_parent(path):
    path=remove_trailing_slash(path)
    if not path or '/' not in path:return ''
    result=path.rsplit('/',1)[0]; return result or '/'
def combine(root,path): return root+path if path.startswith('/') else root+'/'+path
def resembles_path(value): return bool(value) and ('/' in value or '.' in value)
def lines_to_hash_set(value): return PotentialPaths(value.splitlines())
def get_all_scs_files(directory): return Path(directory).glob('*.scs')
def increment_path_if_exists(path):
    if not Path(path).exists(): return path
    for i in range(2,9999):
        candidate=append_before_extension(path,str(i))
        if not Path(candidate).exists(): return candidate
    raise OSError('No available incremented path')

class PotentialPaths(set):
    def add_path(self,value,visited=None):
        if (visited and value in visited) or not resembles_path(value): return
        value=ensure_initial_slash(value)
        if value in self:return
        super().add(value)
        variants={'.pmd':['.pmg','.pmc','.pma','.ppd'],'.pmg':['.pmd','.pmc','.pma','.ppd'],'.bank':['.bank.guids'],'.mat':['.tobj','.dds'],'.tobj':['.mat','.dds'],'.dds':['.mat','.tobj']}
        suffix=Path(value).suffix.lower()
        for ext in variants.get(suffix,[]): self.add_path(str(Path(value).with_suffix(ext)).replace('\\','/'),visited)
        parent=get_parent(value)
        while parent not in ('','/'): super().add(parent); parent=get_parent(parent)

@dataclass
class Directory:
    path:str='/'; subdirectories:dict[str,'Directory']=field(default_factory=dict); files:list[str]=field(default_factory=list)
def path_list_to_tree(root,paths):
    out=Directory()
    for path in paths:
        if root!='/' and not path.startswith(root):continue
        parts=[x for x in path.split('/') if x]; node=out
        for i,part in enumerate(parts[:-1]): node=node.subdirectories.setdefault(part,Directory('/'+'/'.join(parts[:i+1])))
        if not path.endswith('/') and parts: node.files.append(path)
    return out
def tree_lines(root,indent=0):
    lines=[]
    for name,node in sorted(root.subdirectories.items()): lines.append(' │ '*indent+' ├╴['+replace_control_chars(name)+']'); lines.extend(tree_lines(node,indent+1))
    for i,path in enumerate(root.files): lines.append(' │ '*indent+(' └╴' if i==len(root.files)-1 else ' ├╴')+replace_control_chars(path.rsplit('/',1)[-1]))
    return lines

def find_quoted_paths(text):
    ranges=[]; i=0
    while i<len(text):
        if text.startswith('//',i) or text[i]=='#':
            i=text.find('\n',i); i=len(text) if i<0 else i; continue
        if text.startswith('/*',i):
            i=text.find('*/',i+2); i=len(text) if i<0 else i+2; continue
        if text[i]=='"' and (i==0 or text[i-1]!='\\'):
            start=i+1; i=start
            while i<len(text) and text[i] not in '\r\n' and not (text[i]=='"' and text[i-1]!='\\'): i+=1
            if any(c in text[start:i] for c in './'): ranges.append((start,i))
        i+=1
    return ranges
def replace_strings(text,ranges,substitutions):
    out=[]; prev=0; modified=False
    for start,end in ranges:
        out.append(text[prev:start]); target=text[start:end]
        if target in substitutions: out.append(substitutions[target]); prev=end; modified=True
        else: prev=start
    out.append(text[prev:]); return ''.join(out),modified
def replace_renamed_paths(text,substitutions): return replace_strings(text,find_quoted_paths(text),substitutions)
def wildcard_string_to_regex(value): return re.compile('^'+re.escape(value).replace(r'\*','.*').replace(r'\?','.')+'$')
def matches_filters(filters,value): return filters is None or any(pattern.search(value) for pattern in filters)
