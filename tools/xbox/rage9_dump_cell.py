"""Dump the per-geometry buffers of a drawable out of the levels window, resolving the graphics segment.

The graphics segment is a concatenation of 42 40 00 00 pages in stored order. This finds the page sequence that validates every
vertex-buffer header of the drawable, then writes for each geometry:
  g<i>_vb.bin  the whole vertex block: the 32-byte origin/axis/scale header before voff, then one stream per attribute, starting
               with the quantized positions (8 bytes per vertex) at voff
  g<i>_ib.bin  the packed meshlet index buffer
and a manifest with what the rehydration tools need, including voff and "stride", the block's bytes per vertex over all streams
(20 to 44), which is not the position stride.

usage: python -I rage9_dump_cell.py <window.rpf> <drawable name> <out_dir> [indices]
"""
import json, os, struct, sys, zlib

BASE_SYS, BASE_GFX = 0x50000000, 0x60000000
MAGIC = b"\x42\x40\x00\x00"


def streams(path):
    d = open(path, "rb").read()
    i = 0
    while True:
        i = d.find(b"\x78\xda", i)
        if i < 0:
            break
        if i % 16 == 0:
            try:
                z = zlib.decompressobj()
                raw = z.decompress(d[i:i + (8 << 20)], 64 << 20)
                if z.eof and raw:
                    yield i, raw
            except zlib.error:
                pass
        i += 1


def find_system(path, name):
    for off, raw in streams(path):
        j = raw.find(name)
        if j < 0:
            continue
        ptr = struct.pack("<Q", BASE_SYS + j)
        p = raw.find(ptr)
        while p >= 0:
            d = p - 168
            if 0 <= d < len(raw) - 192:
                return off, raw, d
            p = raw.find(ptr, p + 1)
    return None


class W:
    def __init__(self, s):
        self.s = s

    def u16(self, a): return struct.unpack_from("<H", self.s, a)[0]

    def u32(self, a): return struct.unpack_from("<I", self.s, a)[0]

    def u64(self, a): return struct.unpack_from("<Q", self.s, a)[0]

    def S(self, p, size=1):
        v = self.u64(p)
        if not (BASE_SYS <= v <= BASE_SYS + len(self.s)):
            raise ValueError("bad pointer")
        at = v - BASE_SYS
        if at + size > len(self.s):
            raise ValueError("range")
        return at

    def geos(self, drawable):
        lod = self.S(drawable + 80, 16)
        models = self.u16(lod + 8)
        lst = self.S(lod, models * 8)
        out = []
        for mi in range(models):
            model = self.S(lst + mi * 8, 56)
            count = self.u16(model + 16)
            geo = self.S(model + 8, count * 8)
            for gi in range(count):
                g = self.S(geo + gi * 8, 160)
                vb = self.S(g + 8, 80)
                fvf = self.S(vb + 64, 320)
                flags = self.u32(fvf + 0x138)
                desc = self.S(g + 64, 16)
                ib = self.S(desc, 64)
                out.append({"index": gi, "vertices": flags >> 10, "triangles": self.u16(desc + 10),
                            "clusters": self.u16(desc + 8),
                            "vb": self.u64(vb + 24) - BASE_GFX, "vb_len": self.u32(vb + 8) * self.u16(vb + 12),
                            "pos_off": self.u32(fvf), "pos_size": self.s[fvf + 0xD0],
                            "pos_fmt": self.s[fvf + 0x104],
                            "ib": self.u64(ib + 24) - BASE_GFX, "ib_len": self.u32(ib + 8) * self.u16(ib + 12)})
        for g in out:   # bytes per vertex over all attribute streams; positions are their own 8-byte stream
            g["stride"] = (g["vb_len"] - g["pos_off"]) // g["vertices"] if g["vertices"] else 0
        return out


def main():
    window, name, out = sys.argv[1], sys.argv[2].encode(), sys.argv[3]
    only = [int(x) for x in sys.argv[4].split(",")] if len(sys.argv) > 4 else None
    os.makedirs(out, exist_ok=True)
    found = find_system(window, name)
    if not found:
        print("drawable not found")
        return 1
    _, sysb, drawable = found
    geos = W(sysb).geos(drawable)
    offs = [g["vb"] for g in geos]
    pages = sorted([(o, r) for o, r in streams(window) if r[:4] == MAGIC], key=lambda x: x[0])
    best = None
    for i in range(len(pages)):
        concat = b""
        for k in range(6):
            if i + k >= len(pages):
                break
            concat += pages[i + k][1]
            if k >= 1:
                ok = sum(1 for o in offs if o + 4 <= len(concat) and (struct.unpack_from("<I", concat, o)[0] & 0x7F) == 0x42)
                if ok == len(offs):
                    best = (concat, pages[i][0], k + 1)
                    break
        if best:
            break
    if not best:
        print("no page sequence validates every vertex header")
        return 1
    gfx, first_off, npages = best
    print("graphics segment: %d pages from 0x%x, %d bytes, all %d vertex headers valid" % (npages, first_off, len(gfx), len(offs)))
    manifest = []
    for g in geos:
        if only is not None and g["index"] not in only:
            continue
        vb = gfx[g["vb"]:g["vb"] + g["vb_len"]]
        ib = gfx[g["ib"]:g["ib"] + g["ib_len"]]
        open(os.path.join(out, "g%d_vb.bin" % g["index"]), "wb").write(vb)
        open(os.path.join(out, "g%d_ib.bin" % g["index"]), "wb").write(ib)
        manifest.append({"index": g["index"], "vb": "g%d_vb.bin" % g["index"], "ib": "g%d_ib.bin" % g["index"],
                         "stride": g["stride"], "count": g["vertices"], "vfmt": g["pos_fmt"], "ifmt": 53,
                         "voffset": g["pos_off"], "triangles": g["triangles"], "clusters": g["clusters"]})
        print("geom %-2d v %-6d t %-6d vb %-7d ib %-7d stride %-3d voff %d" %
              (g["index"], g["vertices"], g["triangles"], len(vb), len(ib), g["stride"], g["pos_off"]))
    json.dump(manifest, open(os.path.join(out, "manifest.json"), "w"), indent=1)
    print("wrote", out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
