"""Write a synthetic compressed BLAS for bvh_test's BVH_REHYDRATE path: no game data, only the layout from BVH_CONTAINER.md.
The three packers mirror the ones in test_bvh_rehydrate_cpu.py; the expected output comes from the CPU model itself.

usage: python -I make_bvh_fixture.py <out_dir>
Writes src.bin (compressed BLAS, the 64-byte header first), vb.bin (8 triangles, R32G32B32_FLOAT), expected.bin (the CPU model's
rehydrated BVH) and manifest.json (sizes and the geometry description the C++ side has to pass).
"""
import json, os, struct, sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import bvh_rehydrate_cpu as bvh


def pack_leaf_chunk(entries):
    n = len(entries)
    p0, p1, p2, p3, p4 = bytearray(), bytearray(), bytearray(), bytearray(), bytearray()
    for geom_idx, flags, prim0, prim1, vorder in entries:
        p0 += bytes((geom_idx & 0xFF, (geom_idx >> 8) & 0xFF, (geom_idx >> 16) & 0xFF))
        p1.append(flags & 0xFF)
        p2 += bytes((prim0 & 0xFF, (prim0 >> 8) & 0xFF, (prim0 >> 16) & 0xFF))
        pr1 = 0xFFFFFF if prim1 == bvh.INVALID_NODE else (prim1 & 0xFFFFFF)
        p3 += bytes((pr1 & 0xFF, (pr1 >> 8) & 0xFF, (pr1 >> 16) & 0xFF))
        p4.append(vorder & 0xFF)
    return bytes(p0 + p1 + p2 + p3 + p4)


def pack_box_group(nodes, is_box32):
    hwords, cids, ovs = [], [], []
    for children, overrides in nodes:
        mask, vals = 0, []
        for bit_idx in sorted(overrides.keys()):
            mask |= 1 << bit_idx
            vals.append(overrides[bit_idx])
        hwords.append((len(children) << 24) | mask)
        cids.extend(children)
        ovs.extend(vals)
    out = bytearray()
    out += struct.pack("<%dI" % len(hwords), *hwords)
    if cids:
        out += struct.pack("<%dI" % len(cids), *cids)
    if ovs:
        out += struct.pack("<%df" % len(ovs), *ovs) if is_box32 else struct.pack("<%dH" % len(ovs), *ovs)
    return bytes(out)


def header(total_bytes, num_leaves, aabb_min, aabb_max, hier_off, leaf_off, root_id):
    return struct.pack(
        "<4I6f6I",
        bvh.BVH_FLAG_BLAS | bvh.BVH_FLAG_COMPRESSED | bvh.BVH_FLAG_UPDATABLE | 0x20006,
        (total_bytes >> 6) - 1, 0x12345678, num_leaves,
        *aabb_min, *aabb_max,
        hier_off, 0, root_id, leaf_off, 0x40,
        bvh.DEFAULT_TRANSFORM_WORD)


def main():
    out = sys.argv[1] if len(sys.argv) > 1 else "."
    os.makedirs(out, exist_ok=True)
    tris = [
        ((0.0, 0.0, 0.0), (1.0, 0.0, 0.0), (0.0, 1.0, 0.0)),
        ((2.0, 0.0, 0.0), (3.0, 0.0, 0.0), (2.0, 1.0, 0.0)),
        ((0.0, 4.0, 0.0), (1.0, 4.0, 0.0), (0.0, 5.0, 0.0)),
        ((2.0, 4.0, 0.0), (3.0, 4.0, 0.0), (2.0, 5.0, 0.0)),
        ((10.0, 0.0, 1.0), (11.0, 0.0, 1.0), (10.0, 1.0, 2.0)),
        ((12.0, 0.0, 1.0), (13.0, 0.0, 1.0), (12.0, 1.0, 2.0)),
        ((10.0, 4.0, 1.0), (11.0, 4.0, 1.0), (10.0, 5.0, 2.0)),
        ((12.0, 4.0, 1.0), (13.0, 4.0, 1.0), (12.0, 5.0, 2.0)),
    ]
    vb = b"".join(struct.pack("<3f", *p) for t in tris for p in t)

    leaf_off, hier_off = 0x100, 0x300
    lv0_b32, lv0_b16, lv1_b16 = hier_off, hier_off + 128, hier_off + 192
    total_bytes = 0x440
    leaf_ids = [((leaf_off + i * 64) >> 6) << 3 | bvh.NODE_TYPE_TRI0 for i in range(8)]
    b32_id = ((lv0_b32 >> 6) << 3) | bvh.NODE_TYPE_BOX32
    b16_id = ((lv0_b16 >> 6) << 3) | bvh.NODE_TYPE_BOX16
    root_id = ((lv1_b16 >> 6) << 3) | bvh.NODE_TYPE_BOX16

    src = bytearray(total_bytes)
    src[:64] = header(total_bytes, 8, (0.0, 0.0, 0.0), (13.0, 5.0, 2.0), hier_off, leaf_off, root_id)
    chunk = pack_leaf_chunk([(0, 0x01, i, bvh.INVALID_NODE, 0x09) for i in range(8)])
    src[leaf_off:leaf_off + len(chunk)] = chunk
    g0_b32 = pack_box_group([(leaf_ids[0:4], {})], True)
    src[lv0_b32:lv0_b32 + len(g0_b32)] = g0_b32
    g0_b16 = pack_box_group([(leaf_ids[4:8], {})], False)
    src[lv0_b16:lv0_b16 + len(g0_b16)] = g0_b16
    g1_b16 = pack_box_group([([b32_id, b16_id], {})], False)
    src[lv1_b16:lv1_b16 + len(g1_b16)] = g1_b16
    table = struct.pack("<5I", 128, 192, 192, 256, 2)
    src[total_bytes - len(table):] = table

    geom = bvh.TriangleGeometryDesc(vertex_buffer=vb, vertex_stride=12)
    dst = bvh.rehydrate_bvh(bytes(src), geometries=[geom])
    open(os.path.join(out, "src.bin"), "wb").write(src)
    open(os.path.join(out, "vb.bin"), "wb").write(vb)
    open(os.path.join(out, "expected.bin"), "wb").write(dst)
    json.dump({"src_bytes": len(src), "vb_bytes": len(vb), "expected_bytes": len(dst),
               "vertex_count": 8 * 3, "vertex_stride": 12, "vertex_format": 6,   # DXGI_FORMAT_R32G32B32_FLOAT
               "num_leaves": 8, "total_bytes": total_bytes},
              open(os.path.join(out, "manifest.json"), "w"), indent=1)
    print("wrote", out, "src", len(src), "expected", len(dst))


if __name__ == "__main__":
    main()
