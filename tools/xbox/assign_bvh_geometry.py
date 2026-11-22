"""Assign a mesh to every geometry index of a dehydrated BLAS, by leaf bounds.

In one model run all geometry indices point at the same mesh, so the leaves of each index carry that mesh's bounds; repeating the run
for every candidate mesh gives a (index, mesh) table of leaf AABBs. Each index then takes the mesh whose own AABB matches those leaves.
Finally the chosen assignment is rehydrated once and validated.

usage: python -I assign_bvh_geometry.py <bvh.bin> <models dir> [--stride 12] [--index-format 42] [--out report.json]
Asset names are printed for the local user; the tool does not write anything except the optional report.
"""
import argparse, glob, json, math, os, struct, sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import bvh_rehydrate_cpu as bvh

INVALID = bvh.INVALID_NODE


def read_obj(path):
    verts, tris = [], []
    for line in open(path, encoding="utf-8", errors="ignore"):
        p = line.split()
        if not p:
            continue
        if p[0] == "v":
            verts.append((float(p[1]), float(p[2]), float(p[3])))
        elif p[0] == "f":
            idx = [int(t.split("/")[0]) for t in p[1:]]
            idx = [i - 1 if i > 0 else len(verts) + i for i in idx]
            for k in range(1, len(idx) - 1):
                tris.append((idx[0], idx[k], idx[k + 1]))
    vb = bytearray()
    for v in verts:
        vb.extend(struct.pack("<3f", *v))
    ib = bytearray()
    for t in tris:
        ib.extend(struct.pack("<3I", *t))
    return bytes(vb), bytes(ib), len(verts), len(tris)


def mesh_aabb(vb):
    n = len(vb) // 12
    mn = [math.inf] * 3
    mx = [-math.inf] * 3
    for i in range(n):
        v = struct.unpack_from("<3f", vb, i * 12)
        for k in range(3):
            mn[k] = min(mn[k], v[k])
            mx[k] = max(mx[k], v[k])
    return mn, mx


def leaf_bounds_by_geometry(dst, header):
    """geom_index -> [min xyz, max xyz, count] for the rehydrated leaves."""
    out = {}
    for i in range(header.num_leaves):
        base = header.offset_to_leaves + (i << 6)
        if base + 64 > len(dst):
            break
        v = struct.unpack_from("<12f", dst, base)
        flags = struct.unpack_from("<I", dst, base + 48)[0]
        gi = (flags >> 8) & 0xFFFFFF
        pts = [v[0:3], v[3:6], v[6:9]] + ([v[9:12]] if (flags & 0x80) else [])
        if gi not in out:
            out[gi] = [math.inf, math.inf, math.inf, -math.inf, -math.inf, -math.inf, 0]
        e = out[gi]
        for p in pts:
            for k in range(3):
                e[k] = min(e[k], p[k])
                e[3 + k] = max(e[3 + k], p[k])
        e[6] += 1
    return out


def score(a, b):     # a = mesh AABB, b = leaf AABB
    size_a = [a[1][k] - a[0][k] for k in range(3)]
    size_b = [b[3 + k] - b[k] for k in range(3)]
    ds = max((abs(size_a[k] - size_b[k]) / size_a[k]) if size_a[k] > 1e-6 else 0.0 for k in range(3))
    off = max(abs((a[0][k] + a[1][k]) / 2 - (b[k] + b[3 + k]) / 2) / max(size_a[k], 1e-6) for k in range(3))
    return ds + off


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("bvh")
    ap.add_argument("models")
    ap.add_argument("--stride", type=int, default=12)
    ap.add_argument("--index-format", type=int, default=42)
    ap.add_argument("--out", default="")
    a = ap.parse_args()
    src = open(a.bvh, "rb").read()
    header = bvh.parse_bvh_header(src)
    leaves = bvh.decode_compressed_leaves(src)
    max_geom = max(l.geom_index for l in leaves)
    ifmt = bvh.INDEX_FORMAT_R32_UINT if a.index_format == 42 else bvh.INDEX_FORMAT_R16_UINT
    meshes = []
    for f in sorted(glob.glob(os.path.join(a.models, "*.obj"))):
        vb, ib, nv, nt = read_obj(f)
        if nv and nt:
            meshes.append({"name": os.path.basename(f), "vb": vb, "ib": ib, "tris": nt, "aabb": mesh_aabb(vb)})
    max_prim = max((max(l.prim_index_0, l.prim_index_1) for l in leaves
                    if max(l.prim_index_0, l.prim_index_1) != INVALID), default=0)
    per_tri = 6 if a.index_format == 57 else 12
    need = (max_prim + 1) * per_tri
    print("BVH", os.path.basename(a.bvh), "leaves", header.num_leaves, "geometry indices", max_geom + 1,
          "candidate meshes", len(meshes), "max primitive", max_prim)
    for m in meshes:                      # a run puts one mesh on every index, so leaves may reference past its own triangles
        if len(m["ib"]) < need:
            m["ib"] = m["ib"] + b"\0" * (need - len(m["ib"]))

    # one run per mesh, with that mesh on every index
    table = {}
    for j, m in enumerate(meshes):
        g = [bvh.TriangleGeometryDesc(vertex_buffer=m["vb"], vertex_stride=a.stride, index_buffer=m["ib"], index_format=ifmt)
             for _ in range(max_geom + 1)]
        dst = bvh.rehydrate_bvh(src, geometries=g)
        lb = leaf_bounds_by_geometry(dst, header)
        for gi, e in lb.items():
            table[(gi, j)] = (score(m["aabb"], e), e[6], e)
    assign = {}
    for gi in range(max_geom + 1):
        cands = [(table[(gi, j)][0], j) for j in range(len(meshes)) if (gi, j) in table]
        if not cands:
            continue
        cands.sort()
        assign[gi] = {"mesh": meshes[cands[0][1]]["name"], "score": round(cands[0][0], 4),
                      "leaves": table[(gi, cands[0][1])][1],
                      "runner_up": meshes[cands[1][1]]["name"] if len(cands) > 1 else None}
        print("  index %-2d -> %-50s score %.3f leaves %d" % (gi, assign[gi]["mesh"], assign[gi]["score"], assign[gi]["leaves"]))

    # final run with the chosen mesh per index
    g = []
    for gi in range(max_geom + 1):
        name = assign.get(gi, {}).get("mesh")
        m = next((x for x in meshes if x["name"] == name), meshes[0])
        g.append(bvh.TriangleGeometryDesc(vertex_buffer=m["vb"], vertex_stride=a.stride, index_buffer=m["ib"], index_format=ifmt))
    dst = bytes(bvh.rehydrate_bvh(src, geometries=g))
    rep = bvh.validate_rehydrated_bvh(dst, check_aabb_enclosure=True, check_parent_pointers=True)
    kinds = sorted({("does not enclose" if "does not enclose" in e else "non-finite" if "Non-finite" in e else
                     "inverted" if "Inverted" in e else e.split(" ")[0]) for e in rep.errors})
    print("assigned run: errors", len(rep.errors), "kinds", kinds)
    if a.out:
        json.dump({"bvh": os.path.basename(a.bvh), "assign": assign,
                   "errors": len(rep.errors), "error_kinds": kinds}, open(a.out, "w"), indent=1)
        print("wrote", a.out)


if __name__ == "__main__":
    main()
