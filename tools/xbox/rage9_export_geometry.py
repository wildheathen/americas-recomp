"""Decode a drawable's RAGE9 geometries into plain float3 vertices and a uint32 triangle list.

The rehydration library only knows the standard DXGI vertex/index formats; it does not decode the RAGE9 quantized-8 vertex form
(format 16) or the packed meshlet index form (index format 53). This tool converts both:
  * positions: the vertex block keeps each attribute in its own stream. The quantized-8 positions are one contiguous stream of
    8-byte records starting at the vertex-format position offset (128), after the 32-byte origin/axis/scale header at 96; the
    other attributes follow it, so the per-vertex byte count of the whole block (20/24/36/...) is not the position stride;
  * triangles: the meshlet decoder of the CPU model, applied primitive by primitive, so the triangle list keeps the primitive order
    the BVH leaves refer to.
With --instance, the 3x4 transform of the BVH's 208-byte instance descriptor (at +0x50, row major) is applied to the positions:
the BLAS is built from the geometry in that frame (the header bounds and the stored box overrides only match after it).
Writes g<i>_f3.bin, g<i>_tri.bin and export.json (the arguments for rehydrate_cpu --geom / compare_bvh_geometry.py) next to the
buffers produced by rage9_dump_cell.py.

usage: python -I rage9_export_geometry.py <geo_dir> [indices] [--instance <descriptor.bin>]
"""
import json
import os
import struct
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import bvh_rehydrate_cpu as bvh

QUANTIZED8_RECORD = 8
INSTANCE_TRANSFORM_OFFSET = 0x50


def decode_positions(vb, positions, count, stride, fmt, transform=None):
    """float3 positions. Quantized-8 reads the separate position stream (8 bytes per vertex); float3 uses `stride`."""
    out = bytearray()
    for i in range(count):
        if fmt == bvh.VERTEX_FORMAT_RAGE9_QUANTIZED8:
            desc = bvh.TriangleGeometryDesc(vertex_buffer=vb, vertex_stride=QUANTIZED8_RECORD, vertex_format=fmt,
                                            vertex_offset=positions)
            p = bvh._read_vertex(desc, i)
        elif fmt == bvh.VERTEX_FORMAT_R32G32B32_FLOAT:
            p = struct.unpack_from("<3f", vb, positions + i * stride)
        else:
            raise ValueError("unsupported vertex format %d" % fmt)
        if transform is not None:
            p = bvh._apply_transform_3x4(p, transform)
        out += struct.pack("<3f", *p)
    return bytes(out)


def read_instance_transform(path):
    with open(path, "rb") as f:
        m = struct.unpack_from("<12f", f.read(), INSTANCE_TRANSFORM_OFFSET)
    return tuple(tuple(m[r * 4:r * 4 + 4]) for r in range(3))


def decode_triangles(ib, prims):
    desc = bvh.TriangleGeometryDesc(vertex_buffer=b"", vertex_stride=8,
                                    vertex_format=bvh.VERTEX_FORMAT_RAGE9_QUANTIZED8,
                                    index_buffer=ib, index_format=bvh.INDEX_FORMAT_RAGE9_MESHLET)
    out = []
    for prim in range(prims):
        out.extend(bvh._read_triangle_indices(desc, prim))
    return struct.pack("<%dI" % len(out), *out)


def main(argv):
    transform = None
    if "--instance" in argv:
        k = argv.index("--instance")
        transform = read_instance_transform(argv[k + 1])
        argv = argv[:k] + argv[k + 2:]
    geo = argv[0]
    only = set(int(x) for x in argv[1].split(",")) if len(argv) > 1 else None
    man = json.load(open(os.path.join(geo, "manifest.json")))
    export = []
    for g in man:
        if only is not None and g["index"] not in only:
            continue
        vb = open(os.path.join(geo, g["vb"]), "rb").read()
        ib = open(os.path.join(geo, g["ib"]), "rb").read()
        f3 = decode_positions(vb, g["voffset"], g["count"], g["stride"], g["vfmt"], transform)
        tri = decode_triangles(ib, g["triangles"])
        f3_path = os.path.join(geo, "g%d_f3.bin" % g["index"])
        tri_path = os.path.join(geo, "g%d_tri.bin" % g["index"])
        open(f3_path, "wb").write(f3)
        open(tri_path, "wb").write(tri)
        export.append({"index": g["index"], "vertices": g["count"], "triangles": g["triangles"],
                       "vertex_format": bvh.VERTEX_FORMAT_R32G32B32_FLOAT, "index_format": bvh.INDEX_FORMAT_R32_UINT,
                       "stride": 12, "vb": os.path.basename(f3_path), "ib": os.path.basename(tri_path),
                       "instance_transform": transform is not None})
        print("geom %-2d %d vertices, %d triangles -> %s, %s" %
              (g["index"], g["count"], g["triangles"], os.path.basename(f3_path), os.path.basename(tri_path)))
    json.dump(export, open(os.path.join(geo, "export.json"), "w"), indent=1)


if __name__ == "__main__":
    main(sys.argv[1:])
