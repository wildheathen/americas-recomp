"""Resolve the graphics segment of a drawable and dump the raw per-geometry vertex/index buffers for the rehydration tool.

The system page gives 0x60000000-based pointers and lengths (see rage9_walk_drawable.py). This finds the inflated stream that
starts with 42 40 00 00 and covers those offsets, checks each vertex buffer's header byte, and writes <out>/g<i>_vb.bin and
g<i>_ib.bin plus a manifest the caller can turn into --geom arguments.

usage: python -I rage9_dump_geometry.py <window.rpf> <drawable name> <out_dir> [segments=0,1,2,...]
"""
import json, os, struct, sys, zlib

BASE_SYS, BASE_GFX = 0x50000000, 0x60000000


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
                if z.eof and len(raw) > 0x100:
                    yield i, raw
            except zlib.error:
                pass
        i += 1


class Reader:
    def __init__(self, s):
        self.sys = s

    def u16(self, at): return struct.unpack_from("<H", self.sys, at)[0]

    def u32(self, at): return struct.unpack_from("<I", self.sys, at)[0]

    def u64(self, at): return struct.unpack_from("<Q", self.sys, at)[0]

    def S(self, ptr_at, size=1):
        p = self.u64(ptr_at)
        if not (BASE_SYS <= p <= BASE_SYS + len(self.sys)):
            raise ValueError("bad pointer")
        at = p - BASE_SYS
        if at + size > len(self.sys):
            raise ValueError("out of range")
        return at

    def walk(self, drawable):
        lod = self.S(drawable + 80, 16)
        models = self.u16(lod + 8)
        if not (0 < models <= 256) or models != self.u16(lod + 10):
            raise ValueError("bad lod")
        lst = self.S(lod, models * 8)
        geos = []
        for mi in range(models):
            model = self.S(lst + mi * 8, 56)
            count = self.u16(model + 16)
            if not (0 < count <= 1024):
                raise ValueError("bad count")
            geo = self.S(model + 8, count * 8)
            for gi in range(count):
                g = self.S(geo + gi * 8, 160)
                vb = self.S(g + 8, 80)
                fvf = self.S(vb + 64, 320)
                flags = self.u32(fvf + 0x138)
                desc = self.S(g + 64, 16)
                ib = self.S(desc, 64)
                geos.append({
                    "index": gi, "vertices": flags >> 10,
                    "triangles": self.u16(desc + 10), "clusters": self.u16(desc + 8),
                    "vb": self.u64(vb + 24) - BASE_GFX, "vb_len": self.u32(vb + 8) * self.u16(vb + 12),
                    "vb_stride": self.u16(vb + 12), "pos_off": self.u32(fvf),
                    "pos_size": self.sys[fvf + 0xD0], "pos_fmt": self.sys[fvf + 0x104],
                    "ib": self.u64(ib + 24) - BASE_GFX, "ib_len": self.u32(ib + 8) * self.u16(ib + 12),
                })
        return geos


def main():
    window, name, out = sys.argv[1], sys.argv[2].encode(), sys.argv[3]
    only = [int(x) for x in sys.argv[4].split(",")] if len(sys.argv) > 4 else None
    os.makedirs(out, exist_ok=True)
    sys_page = drawable = None
    for off, raw in streams(window):
        j = raw.find(name)
        if j < 0:
            continue
        p = raw.find(struct.pack("<Q", BASE_SYS + j))
        while p >= 0:
            d = p - 168
            if 0 <= d < len(raw) - 192:
                sys_page, drawable = raw, d
                break
            p = raw.find(struct.pack("<Q", BASE_SYS + j), p + 1)
        if sys_page is not None:
            break
    if sys_page is None:
        print("drawable not found")
        return 1
    geos = Reader(sys_page).walk(drawable)
    need = max(g["vb"] + g["vb_len"] for g in geos)
    gfx, best_ok = None, -1
    for off, raw in streams(window):
        if raw[:4] != b"\x42\x40\x00\x00" or len(raw) < need:
            continue
        ok = sum(1 for g in geos if (struct.unpack_from("<I", raw, g["vb"])[0] & 0x7F) == 0x42)
        if ok > best_ok:
            gfx, best_ok = raw, ok
    if gfx is None:
        print("graphics segment not found (need 0x%x bytes)" % need)
        return 1
    print("graphics segment 0x%x bytes, %d of %d vertex headers validated" % (len(gfx), best_ok, len(geos)))
    manifest = []
    for g in geos:
        if only is not None and g["index"] not in only:
            continue
        vb = gfx[g["vb"]:g["vb"] + g["vb_len"]]
        ib = gfx[g["ib"]:g["ib"] + g["ib_len"]]
        ok = len(vb) >= g["pos_off"] + g["vertices"] * g["pos_size"]
        entry = {"index": g["index"], "vertices": g["vertices"], "triangles": g["triangles"],
                 "vertex_format": g["pos_fmt"], "vertex_stride": g["pos_size"],
                 "vertex_offset": g["pos_off"], "index_bytes": len(ib), "header_ok": ok}
        manifest.append(entry)
        open(os.path.join(out, "g%d_vb.bin" % g["index"]), "wb").write(vb)
        open(os.path.join(out, "g%d_ib.bin" % g["index"]), "wb").write(ib)
        print("geom %-2d vertices %-6d tris %-6d vb %d bytes (pos off %d size %d fmt %d hdr %d) ib %d bytes %s"
              % (g["index"], g["vertices"], g["triangles"], len(vb), g["pos_off"], g["pos_size"], g["pos_fmt"],
                 vb[0] & 0x7F if vb else -1, len(ib), "ok" if ok else "POSITIONS OUT OF RANGE"))
    json.dump(manifest, open(os.path.join(out, "manifest.json"), "w"), indent=1)
    print("wrote", out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
