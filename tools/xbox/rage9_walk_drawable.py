"""Milestone 2 probe: locate a drawable by its name inside the inflated system pages of a levels window and walk primary LOD ->
models -> geometry list, printing the vertex/triangle counts of every geometry. Counts come from the system segment only
(vertex format descriptor at vb+64, index descriptor at geometry+64), so no graphics segment is needed for this.

Layout facts (from the referenced decoder, independently reimplemented here): system pointers are u64 values based at 0x50000000.
drawable name ptr at +168, bounds at +48/+64, LOD ptr at +80. LOD: u16 count at +8, capacity +10, model pointer list at LOD.
model: geometry pointer list at +8, count at +16 (== +18 == +54). geometry: vertex buffer at +8, index descriptor at +64.
vb: +8 u32 elements, +12 u16 stride, +64 -> vertex format descriptor (320 bytes). fvf: +0x138 flags (vertex count = flags>>10),
+0 u32 position offset, +0xD0 u8 position size, +0x104 u8 format. index descriptor: +0 -> ib, +8 u16 clusters, +10 u16 triangles,
+12 u32 binding. ib: +8 u32 elements, +12 u16 stride.

usage: python -I rage9_walk_drawable.py <window.rpf> <name> [<name> ...]
"""
import struct, sys, zlib

BASE_SYS = 0x50000000


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
    def __init__(self, sys_bytes, tag):
        self.sys = sys_bytes
        self.tag = tag

    def u16(self, at):
        return struct.unpack_from("<H", self.sys, at)[0]

    def u32(self, at):
        return struct.unpack_from("<I", self.sys, at)[0]

    def u64(self, at):
        return struct.unpack_from("<Q", self.sys, at)[0]

    def S(self, pointer_at, size=1):
        p = self.u64(pointer_at)
        if not (BASE_SYS <= p <= BASE_SYS + len(self.sys)):
            raise ValueError("bad system pointer 0x%x at 0x%x" % (p, pointer_at))
        at = p - BASE_SYS
        if at + size > len(self.sys):
            raise ValueError("pointer out of range")
        return at

    def walk(self, drawable):
        minv = struct.unpack_from("<3f", self.sys, drawable + 48)
        maxv = struct.unpack_from("<3f", self.sys, drawable + 64)
        lod = self.S(drawable + 80, 16)
        models = self.u16(lod + 8)
        cap = self.u16(lod + 10)
        out = {"drawable": drawable, "bounds": (minv, maxv), "models": models, "capacity": cap, "geometries": []}
        if not (0 < models <= 256) or models != cap:
            raise ValueError("bad LOD count %d/%d" % (models, cap))
        lst = self.S(lod, models * 8)
        for mi in range(models):
            model = self.S(lst + mi * 8, 56)
            count = self.u16(model + 16)
            if not (0 < count <= 1024) or count != self.u16(model + 18) or count != self.u16(model + 54):
                raise ValueError("bad geometry count %d" % count)
            geo = self.S(model + 8, count * 8)
            for gi in range(count):
                g = self.S(geo + gi * 8, 160)
                vb = self.S(g + 8, 80)
                fvf = self.S(vb + 64, 320)
                flags = self.u32(fvf + 0x138)
                vcount = flags >> 10
                desc = self.S(g + 64, 16)
                ib = self.S(desc, 64)
                clusters = self.u16(desc + 8)
                tris = self.u16(desc + 10)
                stride = self.u16(ib + 12)
                vb_data = self.u64(vb + 24)
                vb_elems = self.u32(vb + 8)
                vb_stride = self.u16(vb + 12)
                ib_data = self.u64(ib + 24)
                ib_elems = self.u32(ib + 8)
                out["geometries"].append({"model": mi, "index": gi, "vertices": vcount, "triangles": tris,
                                          "clusters": clusters, "index_stride": stride,
                                          "vb_ptr": vb_data, "vb_elems": vb_elems, "vb_stride": vb_stride,
                                          "ib_ptr": ib_data, "ib_elems": ib_elems,
                                          "position_size": self.sys[fvf + 0xD0], "format": self.sys[fvf + 0x104]})
        return out


def main():
    path = sys.argv[1]
    names = [n.encode() for n in sys.argv[2:]] or sys.exit("usage: rage9_walk_drawable.py <stream> <drawable-name>...")
    found = 0
    for off, raw in streams(path):
        for name in names:
            j = raw.find(name)
            if j < 0:
                continue
            ptr = struct.pack("<Q", BASE_SYS + j)
            p = raw.find(ptr)
            while p >= 0 and found < 20:
                drawable = p - 168
                if 0 <= drawable < len(raw) - 192:
                    try:
                        info = Reader(raw, off).walk(drawable)
                        print("== %s: stream 0x%x name at +0x%x, drawable +0x%x, bounds %s..%s, models %d"
                              % (name.decode(), off, j, drawable,
                                 tuple(round(v, 3) for v in info["bounds"][0]),
                                 tuple(round(v, 3) for v in info["bounds"][1]), info["models"]))
                        for g in info["geometries"]:
                            print("   geom %-2d vertices %-6d triangles %-6d clusters %-4d vb_ptr %08x elems %-5d stride %-2d ib_ptr %08x elems %-5d"
                                  % (g["index"], g["vertices"], g["triangles"], g["clusters"],
                                     g["vb_ptr"] & 0xFFFFFFFF, g["vb_elems"], g["vb_stride"],
                                     g["ib_ptr"] & 0xFFFFFFFF, g["ib_elems"]))
                        found += 1
                    except Exception as e:
                        pass
                p = raw.find(ptr, p + 1)
    print("drawables decoded:", found)


if __name__ == "__main__":
    main()
