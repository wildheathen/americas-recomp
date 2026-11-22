"""Match a dehydrated BLAS to its drawable without knowing the drawable's name.

The BVH leaves carry the geometry index and at most two primitive indices, so the number of primitives per geometry index is exact and
equals that geometry's triangle count. A drawable's geometry list gives the same counts in geometry order, so comparing the two
signatures identifies the drawable. The enumeration recognizes a drawable by a system pointer at +168 to a printable name in the same
page and by a valid LOD -> model -> geometry walk.

usage: python -I rage9_match_drawables.py <window.rpf> <blas.bin> [<blas.bin> ...]
prints, per BLAS, the drawables whose geometry counts start with the BLAS signature.
"""
import collections
import os
import struct
import sys
import zlib

BASE_SYS = 0x50000000

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import bvh_rehydrate_cpu as bvh


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


class Walker:
    def __init__(self, s):
        self.s = s

    def u16(self, a): return struct.unpack_from("<H", self.s, a)[0]

    def u64(self, a): return struct.unpack_from("<Q", self.s, a)[0]

    def ptr(self, p, size=1):
        v = self.u64(p)
        if not (BASE_SYS <= v <= BASE_SYS + len(self.s)):
            raise ValueError
        at = v - BASE_SYS
        if at + size > len(self.s):
            raise ValueError
        return at

    def geometry_triangles(self, drawable):
        lod = self.ptr(drawable + 80, 16)
        models = self.u16(lod + 8)
        if not (0 < models <= 64) or models != self.u16(lod + 10):
            raise ValueError
        lst = self.ptr(lod, models * 8)
        out = []
        for mi in range(models):
            model = self.ptr(lst + mi * 8, 56)
            count = self.u16(model + 16)
            if not (0 < count <= 1024) or count != self.u16(model + 18) or count != self.u16(model + 54):
                raise ValueError
            geo = self.ptr(model + 8, count * 8)
            for gi in range(count):
                g = self.ptr(geo + gi * 8, 160)
                desc = self.ptr(g + 64, 16)
                self.ptr(desc, 64)          # the index buffer must be readable too
                out.append(self.u16(desc + 10))
        return out


def drawables(path):
    seen = set()
    for off, raw in streams(path):
        w = Walker(raw)
        for i in range(0, len(raw) - 8, 4):
            p = struct.unpack_from("<Q", raw, i)[0]
            if not (BASE_SYS <= p < BASE_SYS + len(raw)):
                continue
            name_at = p - BASE_SYS
            end = raw.find(b"\0", name_at, name_at + 128)
            if end - name_at < 3:
                continue
            name = raw[name_at:end]
            if not all(32 <= c < 127 for c in name):
                continue
            d = i - 168
            if d < 0:
                continue
            try:
                counts = w.geometry_triangles(d)
            except Exception:
                continue
            key = (bytes(name), tuple(counts))
            if key not in seen:
                seen.add(key)
                yield off, name.decode(), counts


def leaf_triangles(path):
    leaves = bvh.decode_compressed_leaves(open(path, "rb").read())
    per = collections.Counter()
    for leaf in leaves:
        per[leaf.geom_index] += 1 if leaf.prim_index_1 == bvh.INVALID_NODE else 2
    return [per[i] for i in range(max(per) + 1)]


def main():
    window = sys.argv[1]
    sigs = [(os.path.basename(p), leaf_triangles(p)) for p in sys.argv[2:]]
    for name, sig in sigs:
        print("%s: triangles per geometry %s" % (name, sig))
    hits = {name: [] for name, _ in sigs}
    total = 0
    for off, name, counts in drawables(window):
        total += 1
        for blas, sig in sigs:
            if counts[:len(sig)] == sig:
                hits[blas].append((off, name, counts))
    print("drawables enumerated:", total)
    for blas, _ in sigs:
        print("== %s" % blas)
        for off, name, counts in hits[blas]:
            print("   stream 0x%-8x %-48s %s" % (off, name, counts))
        if not hits[blas]:
            print("   no exact match")


if __name__ == "__main__":
    main()
