"""Decode a drawable's RAGE9 geometries into plain float3 vertices and a uint32 triangle list.

The rehydration library only knows the standard DXGI vertex/index formats; it does not decode the RAGE9 quantized-8 vertex form
(format 16) or the packed meshlet index form (index format 53). This tool converts both:
  * positions: 8-byte quantized vertex at the vertex-format offset inside the per-geometry record stride (20/24/36 bytes), using
    the 32-byte origin/axis/scale header that precedes them;
  * triangles: the meshlet decoder of the CPU model, applied primitive by primitive, so the triangle list keeps the primitive order
    the BVH leaves refer to.
Writes g<i>_f3.bin, g<i>_tri.bin and export.json (the arguments for rehydrate_cpu --geom / compare_bvh_geometry.py) next to the
buffers produced by rage9_dump_cell.py.

usage: python -I rage9_export_geometry.py <geo_dir> [indices]
"""
import json
import os
import struct
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import bvh_rehydrate_cpu as bvh


def decode_positions(vb, positions, count, stride, fmt):
    out = bytearray()
    if fmt == bvh.VERTEX_FORMAT_RAGE9_QUANTIZED8:   # 32-byte origin/axis/scale header right before the positions
        ox, oy, oz, axis = struct.unpack_from("<3fI", vb, positions - 32)
        sx, sy, sz = struct.unpack_from("<3f", vb, positions - 16)
    for i in range(count):
        off = positions + i * stride
        if fmt == bvh.VERTEX_FORMAT_RAGE9_QUANTIZED8:
            w0, w1 = struct.unpack_from("<2I", vb, off)
            ix = (w0 >> 8) & 0xFFFFFF
            iy = (w0 & 0xFF) | ((w1 & 0xFFFF) << 8)
            iz = (w1 >> 16) & 0xFFFF
            a = bvh._f32(bvh._f32(bvh._f32(float(ix)) * sx) + ox)
            b = bvh._f32(bvh._f32(bvh._f32(float(iy)) * sy) + oy)
            c = bvh._f32(bvh._f32(bvh._f32(float(iz)) * sz) + oz)
            p = (c, b, a) if axis == 0 else (b, c, a) if axis == 1 else (a, b, c)
        elif fmt == bvh.VERTEX_FORMAT_R32G32B32_FLOAT:
            p = struct.unpack_from("<3f", vb, off)
        else:
            raise ValueError("unsupported vertex format %d" % fmt)
        out += struct.pack("<3f", *p)
    return bytes(out)


def decode_triangles(ib, prims):
    desc = bvh.TriangleGeometryDesc(vertex_buffer=b"", vertex_stride=8,
                                    vertex_format=bvh.VERTEX_FORMAT_RAGE9_QUANTIZED8,
                                    index_buffer=ib, index_format=bvh.INDEX_FORMAT_RAGE9_MESHLET)
    out = []
    for prim in range(prims):
        out.extend(bvh._read_triangle_indices(desc, prim))
    return struct.pack("<%dI" % len(out), *out)


def main():
    geo = sys.argv[1]
    only = set(int(x) for x in sys.argv[2].split(",")) if len(sys.argv) > 2 else None
    man = json.load(open(os.path.join(geo, "manifest.json")))
    export = []
    for g in man:
        if only is not None and g["index"] not in only:
            continue
        vb = open(os.path.join(geo, g["vb"]), "rb").read()
        ib = open(os.path.join(geo, g["ib"]), "rb").read()
        f3 = decode_positions(vb, g["voffset"], g["count"], g["stride"], g["vfmt"])
        tri = decode_triangles(ib, g["triangles"])
        f3_path = os.path.join(geo, "g%d_f3.bin" % g["index"])
        tri_path = os.path.join(geo, "g%d_tri.bin" % g["index"])
        open(f3_path, "wb").write(f3)
        open(tri_path, "wb").write(tri)
        export.append({"index": g["index"], "vertices": g["count"], "triangles": g["triangles"],
                       "vertex_format": bvh.VERTEX_FORMAT_R32G32B32_FLOAT, "index_format": bvh.INDEX_FORMAT_R32_UINT,
                       "stride": 12, "vb": os.path.basename(f3_path), "ib": os.path.basename(tri_path)})
        print("geom %-2d %d vertices, %d triangles -> %s, %s" %
              (g["index"], g["count"], g["triangles"], os.path.basename(f3_path), os.path.basename(tri_path)))
    json.dump(export, open(os.path.join(geo, "export.json"), "w"), indent=1)


if __name__ == "__main__":
    main()
