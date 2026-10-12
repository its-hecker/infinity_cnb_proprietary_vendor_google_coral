"""ELF64 AArch64 reader for Keymaster dependency verification."""

import struct

class Elf:
    def __init__(self, data):
        self.data = data
        assert data[:6] == b'\x7fELF\x02\x01'
        assert struct.unpack_from('<H', data, 18)[0] == 183
        self.phoff = struct.unpack_from('<Q', data, 32)[0]
        self.shoff = struct.unpack_from('<Q', data, 40)[0]
        self.phsize, self.phnum, self.shsize, self.shnum, self.shstr = struct.unpack_from('<5H', data, 54)
        self.ph = [struct.unpack_from('<II6Q', data, self.phoff + i*self.phsize)
                   for i in range(self.phnum)]
        self.sh = [struct.unpack_from('<II4QII2Q', data, self.shoff + i*self.shsize)
                   for i in range(self.shnum)]
        name_section = self.sh[self.shstr]
        names = data[name_section[4]:name_section[4]+name_section[5]]
        self.sections = {names[s[0]:].split(b'\0', 1)[0].decode(): (i,s)
                         for i,s in enumerate(self.sh)}
        self.dyn = [(off, *struct.unpack_from('<qQ', data, off))
                    for p in self.ph if p[0] == 2
                    for off in range(p[2], p[2]+p[5], 16)]
        self.strtab_va = next(v for _,tag,v in self.dyn if tag == 5)
        self.strtab_off = self.offset(self.strtab_va)
        self.strsz = next(v for _,tag,v in self.dyn if tag == 10)

    def offset(self, va):
        return next(p[2]+va-p[3] for p in self.ph
                    if p[0] == 1 and p[3] <= va < p[3]+p[5])

    def string(self, index):
        return self.data[self.strtab_off+index:].split(b'\0', 1)[0].decode()

    def needed(self):
        return [self.string(v) for _,tag,v in self.dyn if tag == 1]

    def symbols(self):
        _,section = self.sections['.dynsym']
        _,strings = self.sh[section[6]], self.sh[section[6]]
        string_data = self.data[strings[4]:strings[4]+strings[5]]
        result = []
        for off in range(section[4],section[4]+section[5],section[9]):
            name,info,other,index,value,size = struct.unpack_from('<IBBHQQ', self.data, off)
            symbol = string_data[name:].split(b'\0',1)[0].decode()
            result.append((symbol,index,info,value,size))
        return result

