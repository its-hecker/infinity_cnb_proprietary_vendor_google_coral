"""Read compiled APK XML and resource tables for picker validation (stdlib only)."""

from __future__ import annotations
import struct
import zipfile
from pathlib import Path

def chunks(data, start=0, end=None):
    end = len(data) if end is None else end
    while start < end:
        kind, header, size = struct.unpack_from('<HHI', data, start)
        if size < header or header < 8 or start+size > end:
            raise ValueError((hex(start), kind, header, size))
        yield start, kind, header, size
        start += size

def pool(data, off):
    _, header, _ = struct.unpack_from('<HHI', data, off)
    count, _, flags, start, _ = struct.unpack_from('<5I', data, off+8)
    def length(pos, utf8):
        if utf8:
            n = data[pos]; pos += 1
            if n & 128: n = ((n & 127)<<8) | data[pos]; pos += 1
        else:
            n = struct.unpack_from('<H', data, pos)[0]; pos += 2
            if n & 32768: n = ((n & 32767)<<16)|struct.unpack_from('<H',data,pos)[0]; pos += 2
        return n,pos
    result = []
    for index in range(count):
        relative = struct.unpack_from('<I',data,off+header+index*4)[0]
        pos = off+start+relative
        if flags&256:
            _,pos = length(pos,True); n,pos = length(pos,True)
            result.append(data[pos:pos+n].decode('utf-8'))
        else:
            n,pos = length(pos,False)
            result.append(data[pos:pos+n*2].decode('utf-16le'))
    return result

def resources(data, default_only=True):
    _, header, size = struct.unpack_from('<HHI',data)
    strings = []; entries = {}
    for off,kind,head,total in chunks(data,header,size):
        if kind == 1: strings = pool(data,off)
        if kind != 512: continue
        package = struct.unpack_from('<I',data,off+8)[0]
        typeoff,keyoff = struct.unpack_from('<I',data,off+268)[0],struct.unpack_from('<I',data,off+276)[0]
        types,keys = pool(data,off+typeoff),pool(data,off+keyoff)
        for pos,k,h,s in chunks(data,off+head,off+total):
            if k != 513: continue
            typ,flags,_,count,base = struct.unpack_from('<BBHII',data,pos+8)
            config_size = struct.unpack_from('<I',data,pos+20)[0]
            config = data[pos+24:pos+20+config_size]
            if default_only and any(config): continue
            for index in range(count):
                if flags&1:
                    index,rel = struct.unpack_from('<HH',data,pos+h+index*4); rel *= 4
                elif flags&2:
                    rel = struct.unpack_from('<H',data,pos+h+index*2)[0]
                    if rel == 65535: continue
                    rel *= 4
                else:
                    rel = struct.unpack_from('<I',data,pos+h+index*4)[0]
                    if rel == 0xffffffff: continue
                loc = pos+base+rel
                length,eflags,key = struct.unpack_from('<HHI',data,loc)
                def value(v):
                    _,_,t,d = struct.unpack_from('<HBBI',data,v)
                    return (t, strings[d] if t == 3 else d)
                if eflags&1:
                    parent,n = struct.unpack_from('<II',data,loc+8)
                    values = [(struct.unpack_from('<I',data,loc+length+j*12)[0],value(loc+length+j*12+4)) for j in range(n)]
                else: values = value(loc+length)
                entries[(package<<24)|(typ<<16)|index] = (types[typ-1],keys[key],values)
    return entries

def xml(data):
    import xml.etree.ElementTree as ET
    _,header,size = struct.unpack_from('<HHI',data)
    strings = []; stack = []; root = None
    for off,kind,head,total in chunks(data,header,size):
        if kind == 1: strings = pool(data,off)
        if kind == 258:
            namespace,name = struct.unpack_from('<II',data,off+16)
            start,step,count = struct.unpack_from('<HHH',data,off+24)
            tag = strings[name]
            if namespace != 0xffffffff: tag = '{'+strings[namespace]+'}'+tag
            node = ET.Element(tag)
            for i in range(count):
                p=off+16+start+i*step
                ns,key,raw,_,_,t,v=struct.unpack_from('<IIIHBBI',data,p)
                attr = strings[key]
                if ns != 0xffffffff: attr='{'+strings[ns]+'}'+attr
                val = strings[raw] if raw != 0xffffffff else strings[v] if t == 3 else '@ref/0x%08x'%v if t == 1 else 'true' if t == 18 and v else 'false' if t == 18 else str(v)
                node.set(attr,val)
            if stack: stack[-1].append(node)
            else: root=node
            stack.append(node)
        elif kind == 259: stack.pop()
    assert root is not None and not stack
    return root

