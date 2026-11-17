"""Quick .NET assembly analysis via dnfile/dncil (allowlisted under python).

Usage: python scripts/dotnet_disasm.py <assembly.dll> [TypeNameFilter] [MethodNameFilter]
Prints IL of matching methods with tokens resolved to member names.
"""
import sys
from pathlib import Path

import dnfile
from dncil.cil.body import CilMethodBody
from dncil.cil.error import MethodBodyFormatError
from dncil.clr.token import Token, StringToken
from dncil.cil.body.reader import CilMethodBodyReaderBase


class Reader(CilMethodBodyReaderBase):
    def __init__(self, pe, row):
        self.pe = pe
        self.offset = self.pe.get_offset_from_rva(row.Rva)

    def read(self, n):
        data = self.pe.get_data(self.pe.get_rva_from_offset(self.offset), n)
        self.offset += n
        return data

    def tell(self):
        return self.offset

    def seek(self, offset):
        self.offset = offset
        return self.offset


def build_token_map(pe):
    """token value -> readable name."""
    m = {}
    tables = {}
    def add(table, rid_key, name_fn):
        for i, r in enumerate(table or [], start=1):
            try:
                m[rid_key(i)] = name_fn(r)
            except Exception:
                pass
    md = pe.net.mdtables
    add(md.TypeDef, lambda i: (0x02 << 24) | i, lambda r: f"{r.TypeNamespace}.{r.TypeName}")
    # TypeDef token is 0x02, TypeRef is 0x01 — fix below
    m.clear()
    add(md.TypeDef, lambda i: (0x02 << 24) | i, lambda r: f"TypeDef:{r.TypeNamespace}.{r.TypeName}")
    add(md.TypeRef, lambda i: (0x01 << 24) | i, lambda r: f"TypeRef:{r.TypeNamespace}.{r.TypeName}")
    add(md.Field, lambda i: (0x04 << 24) | i, lambda r: f"field {r.Name}")
    add(md.MethodDef, lambda i: (0x06 << 24) | i, lambda r: f"method {r.Name}")
    add(md.MemberRef, lambda i: (0x0A << 24) | i, lambda r: f"memberref {getattr(r.Class, 'row', r.Class)}.{r.Name}")
    return m


def main() -> int:
    path = Path(sys.argv[1])
    type_filter = (sys.argv[2].lower() if len(sys.argv) > 2 else "")
    method_filter = (sys.argv[3].lower() if len(sys.argv) > 3 else "")

    pe = dnfile.dnPE(str(path))
    toks = build_token_map(pe)

    for row in pe.net.mdtables.TypeDef:
        name = f"{row.TypeNamespace}.{row.TypeName}"
        if type_filter and type_filter not in name.lower():
            continue
        for mrow in row.MethodList:
            real = getattr(mrow, "row", mrow)
            if real is None:
                continue
            mname = real.Name
            if method_filter and method_filter not in str(mname).lower():
                continue
            print(f"\n=== {name}::{mname} ===")
            if not real.Rva:
                continue
            try:
                body = CilMethodBody(Reader(pe, real))
            except MethodBodyFormatError as e:
                print("  <unreadable>", e)
                continue
            for insn in body.instructions:
                op = str(insn.opcode)
                operand = ""
                if insn.operand is not None:
                    if isinstance(insn.operand, StringToken):
                        try:
                            row = pe.net.user_strings.get(insn.operand.rid)
                            operand = '"' + (row.value if hasattr(row, 'value') else str(row)) + '"'
                        except Exception:
                            operand = f"us(0x{insn.operand.rid:X})"
                    elif isinstance(insn.operand, Token):
                        operand = toks.get(insn.operand.value) or f"token(0x{insn.operand.value:08X})"
                    else:
                        operand = str(insn.operand)
                print(f"  {op} {operand}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
