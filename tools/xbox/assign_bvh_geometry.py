"""Assign a mesh to every geometry index of a dehydrated BLAS, by leaf bounds.

LIMITATION, measured: the signal is degenerate. A run puts one mesh on every index, so the leaves of each index are produced *from
that mesh* and their AABB matches it by construction: every candidate scores zero. The tool can therefore only list meshes whose
triangle count is plausible for an index, not choose between them. The per-index mapping needs the game's geometry descriptor array;
the BLAS records only the index.

Pass 1 streams every candidate .obj for its AABB and triangle count and keeps the meshes whose triangle count is plausible for at
least one index (these packs use 1-2 triangles per leaf). Pass 2 rehydrates the BLAS once per remaining mesh with that mesh on every
index, reads the leaves of each geometry index, and scores the mesh AABB against them. The chosen assignment is rehydrated once and
validated.

usage: python -I assign_bvh_geometry.py <bvh.bin> <models dir> [--stride 12] [--index-format 42] [--out report.json]
Asset names are printed for the local user; nothing is written except the optional report.
"""
import argparse, glob, json, math, os, struct, sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import bvh_rehydrate_cpu as bvh

INVALID = bvh.INVALID_NODE


def mesh_stats(path):
    mn = [math.inf] * 3
    mx = [-math.inf] * 3
    tris = 0
    with open(path, encoding="utf-8", errors="ignore") as f:
        for line in f:
            if line.startswith("v "):
                p = line.split()
                for k in range(3):
                    v = float(p[k + 1])
                    if v < mn[k]:
                        mn[k] = v
                    if v > mx[k]:
                        mx[k] = v
            elif line.startswith("f "):
                tris += max(0, len(line.split()) - 3)
    return mn, mx, tris


def read_obj(path):
    verts, tris = [], []
    with open(path, encoding="utf-8", errors="ignore") as f:
        for line in f:
            if line.startswith("v "):
                p = line.split()
                verts.append((float(p[1]), float(p[2]), float(p[3])))
            elif line.startswith("f "):
                idx = [int(t.split("/")[0]) for t in line.split()[1:]]
                idx = [i - 1 if i > 0 else len(verts) + i for i in idx]
                for k in range(1, len(idx) - 1):
                    tris.append((idx[0], idx[k], idx[k + 1]))
    vb = bytearray()
    for v in verts:
        vb.extend(struct.pack("<3f", *v))
    ib = bytearray()
    for t in tris:
        ib.extend(struct.pack("<3I", *t))
    return bytes(vb), bytes(ib)


def inverse_leaf_bounds(e, tp):
    """Invert the leaf transform (quantized -> world): leaf_r = (R_r . v - t_r) * scale_r, so v = R^T (leaf/scale + t)."""
    if tp is None or not tp.enabled:
        return e
    pts = []
    for x in (e[0], e[3]):
        for y in (e[1], e[4]):
            for z in (e[2], e[5]):
                q = [x / tp.scale[0] + tp.translation[0], y / tp.scale[1] + tp.translation[1],
                     z / tp.scale[2] + tp.translation[2]]
                pts.append([sum(tp.rotation_3x3[r][k] * q[k] for k in range(3)) for r in range(3)])
    return [min(p[k] for p in pts) for k in range(3)] + [max(p[k] for p in pts) for k in range(3)]


def leaf_bounds_by_geometry(dst, header, tp=None):
    out = {}
    for i in range(header.num_leaves):
        base = header.offset_to_leaves + (i << 6)
        if base + 64 > len(dst):
            break
        v = struct.unpack_from("<12f", dst, base)
        flags = struct.unpack_from("<I", dst, base + 48)[0]
        gi = (flags >> 8) & 0xFFFFFF
        pts = [v[0:3], v[3:6], v[6:9]] + ([v[9:12]] if (flags & 0x80) else [])
        e = out.setdefault(gi, [math.inf, math.inf, math.inf, -math.inf, -math.inf, -math.inf, 0])
        for p in pts:
            for k in range(3):
                e[k] = min(e[k], p[k])
                e[3 + k] = max(e[3 + k], p[k])
        e[6] += 1
    if tp is not None and tp.enabled:
        for gi, e in out.items():
            w = inverse_leaf_bounds(e, tp)
            out[gi] = w + [e[6]]
    return out


def score(aabb, leaf, diag, tris, leaves):
    size_a = [aabb[1][k] - aabb[0][k] for k in range(3)]
    size_b = [leaf[3 + k] - leaf[k] for k in range(3)]
    ds = max(abs(size_a[k] - size_b[k]) for k in range(3)) / diag
    off = max(abs((aabb[0][k] + aabb[1][k]) / 2 - (leaf[k] + leaf[3 + k]) / 2) for k in range(3)) / diag
    ratio = tris / max(1.0, leaves)
    return ds + off + (0.0 if 0.4 <= ratio <= 2.6 else 1.0)


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
    per_index = {}
    for l in leaves:
        per_index[l.geom_index] = per_index.get(l.geom_index, 0) + 1
    max_prim = max((max(l.prim_index_0, l.prim_index_1) for l in leaves
                    if max(l.prim_index_0, l.prim_index_1) != INVALID), default=0)
    diag = max(1e-6, math.dist([header.aabb_min[k] for k in range(3)], [header.aabb_max[k] for k in range(3)]))
    tp = bvh.decode_leaf_transform(header)
    ifmt = bvh.INDEX_FORMAT_R32_UINT if a.index_format == 42 else bvh.INDEX_FORMAT_R16_UINT
    per_tri = 6 if a.index_format == 57 else 12
    need = (max_prim + 1) * per_tri

    allm = sorted(glob.glob(os.path.join(a.models, "*.obj")))
    cand = []
    for f in allm:
        mn, mx, tris = mesh_stats(f)
        if tris and any(0.4 * n <= tris <= 2.6 * n for n in per_index.values()):
            cand.append({"name": os.path.basename(f), "path": f, "aabb": (mn, mx), "tris": tris})
    print("BVH", os.path.basename(a.bvh), "leaves", header.num_leaves, "indices", max_geom + 1,
          "per index", dict(sorted(per_index.items())), "candidates", len(cand), "of", len(allm))

    table = {}
    for j, m in enumerate(cand):
        vb, ib = read_obj(m["path"])
        if len(ib) < need:
            ib += b"\0" * (need - len(ib))
        g = [bvh.TriangleGeometryDesc(vertex_buffer=vb, vertex_stride=a.stride, index_buffer=ib, index_format=ifmt)
             for _ in range(max_geom + 1)]
        try:
            dst = bvh.rehydrate_bvh(src, geometries=g)
        except Exception as e:
            print("  skip", m["name"], e)
            continue
        for gi, e in leaf_bounds_by_geometry(dst, header, tp).items():
            table[(gi, j)] = (score(m["aabb"], e, diag, m["tris"], e[6]), e[6])

    assign = {}
    for gi in range(max_geom + 1):
        cands = sorted((table[(gi, j)][0], j) for j in range(len(cand)) if (gi, j) in table)
        if not cands:
            continue
        j = cands[0][1]
        assign[gi] = {"mesh": cand[j]["name"], "score": round(cands[0][0], 4), "leaves": table[(gi, j)][1],
                      "runner_up": cand[cands[1][1]]["name"] if len(cands) > 1 else None}
        print("  index %-2d leaves %-5d -> %-55s score %.3f" % (gi, table[(gi, j)][1], cand[j]["name"], cands[0][0]))

    g = []
    for gi in range(max_geom + 1):
        m = next((x for x in cand if x["name"] == assign.get(gi, {}).get("mesh")), cand[0])
        vb, ib = read_obj(m["path"])
        if len(ib) < need:
            ib += b"\0" * (need - len(ib))
        g.append(bvh.TriangleGeometryDesc(vertex_buffer=vb, vertex_stride=a.stride, index_buffer=ib, index_format=ifmt))
    dst = bytes(bvh.rehydrate_bvh(src, geometries=g))
    rep = bvh.validate_rehydrated_bvh(dst, check_aabb_enclosure=True, check_parent_pointers=True)
    kinds = sorted({("does not enclose" if "does not enclose" in e else "NaN" if "NaN AABB" in e else
                     "inverted" if "Inverted" in e else e.split(" ")[0]) for e in rep.errors})
    print("assigned run: errors", len(rep.errors), "kinds", kinds)
    if a.out:
        json.dump({"bvh": os.path.basename(a.bvh), "assign": assign, "errors": len(rep.errors),
                   "error_kinds": kinds}, open(a.out, "w"), indent=1)
        print("wrote", a.out)


if __name__ == "__main__":
    main()
