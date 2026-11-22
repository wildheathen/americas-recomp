"""Convert a Wavefront OBJ into the two raw buffers rehydrate_cpu expects: vb.bin (float3, stride 12) and ib.bin (u32 triangles).

usage: python -I obj_to_geometry.py in.obj out_vb.bin out_ib.bin
Only 'v' and 'f' are read; polygons are triangulated as a fan. Useful for feeding a mesh exported by a model viewer (for example
PercyStar's --extract-models) to the CPU rehydration tool.
"""
import struct, sys


def main():
    obj, vb_path, ib_path = sys.argv[1], sys.argv[2], sys.argv[3]
    verts, tris = [], []
    for line in open(obj, encoding="utf-8", errors="ignore"):
        p = line.split()
        if not p:
            continue
        if p[0] == "v":
            verts.append((float(p[1]), float(p[2]), float(p[3])))
        elif p[0] == "f":
            idx = []
            for t in p[1:]:
                i = int(t.split("/")[0])
                idx.append(i - 1 if i > 0 else len(verts) + i)
            for k in range(1, len(idx) - 1):
                tris.append((idx[0], idx[k], idx[k + 1]))
    vb = bytearray()
    for v in verts:
        vb.extend(struct.pack("<3f", *v))
    ib = bytearray()
    for t in tris:
        ib.extend(struct.pack("<3I", *t))
    open(vb_path, "wb").write(vb)
    open(ib_path, "wb").write(ib)
    print("wrote %s (%d vertices) and %s (%d triangles)" % (vb_path, len(verts), ib_path, len(tris)))


if __name__ == "__main__":
    main()
