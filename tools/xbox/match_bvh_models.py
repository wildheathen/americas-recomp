"""Match a dehydrated BVH against exported meshes: rotate the BVH scene bounds by the instance descriptor's transform and
compare them with each mesh's world AABB and triangle count (the leaf records of these packs hold two triangles per leaf).

usage: python -I match_bvh_models.py <bvh.bin> <models dir or .obj>... [--descriptor <instance208.bin>] [--tol 0.08]
       [--tris-per-leaf 2]
The descriptor is optional; without it the bounds are compared unrotated. Nothing from the inputs is written.
"""
import argparse, math, os, struct, sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import bvh_rehydrate_cpu as bvh


def read_descriptor(path):
    d = open(path, "rb").read()
    if len(d) < 0x88:
        raise SystemExit("descriptor shorter than 0x88")
    rows = [struct.unpack_from("<4f", d, 0x50 + 16 * i) for i in range(3)]
    return rows


def invert_rot3(m):
    # rotation transpose (rows x, y, z; fourth column is translation, ignored for bounds)
    return [[m[j][i] for j in range(3)] for i in range(3)]


def transform_bounds(bmin, bmax, rot):
    pts = []
    for x in (bmin[0], bmax[0]):
        for y in (bmin[1], bmax[1]):
            for z in (bmin[2], bmax[2]):
                pts.append((sum(rot[0][k] * v for k, v in enumerate((x, y, z))),
                            sum(rot[1][k] * v for k, v in enumerate((x, y, z))),
                            sum(rot[2][k] * v for k, v in enumerate((x, y, z)))))
    return (tuple(min(p[k] for p in pts) for k in range(3)),
            tuple(max(p[k] for p in pts) for k in range(3)))


def obj_stats(path):
    mn = [math.inf] * 3
    mx = [-math.inf] * 3
    verts = 0
    tris = 0
    for line in open(path, encoding="utf-8", errors="ignore"):
        p = line.split()
        if not p:
            continue
        if p[0] == "v":
            verts += 1
            for k in range(3):
                v = float(p[k + 1])
                mn[k] = min(mn[k], v)
                mx[k] = max(mx[k], v)
        elif p[0] == "f":
            tris += max(0, len(p) - 3)
    return tuple(mn), tuple(mx), verts, tris


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("bvh")
    ap.add_argument("models", nargs="+")
    ap.add_argument("--descriptor", default="")
    ap.add_argument("--tol", type=float, default=0.08)
    ap.add_argument("--tris-per-leaf", type=float, default=2.0)
    a = ap.parse_args()
    src = open(a.bvh, "rb").read()
    h = bvh.parse_bvh_header(src)
    rot = None
    if a.descriptor:
        rot = invert_rot3(read_descriptor(a.descriptor))
    bmin, bmax = transform_bounds(h.aabb_min, h.aabb_max, rot) if rot else (h.aabb_min, h.aabb_max)
    size = [bmax[k] - bmin[k] for k in range(3)]
    print("BVH bounds", tuple(round(v, 3) for v in bmin), tuple(round(v, 3) for v in bmax),
          "size", tuple(round(v, 3) for v in size), "leaves", h.num_leaves,
          "expected triangles ~", int(h.num_leaves * a.tris_per_leaf))
    files = []
    for m in a.models:
        if os.path.isdir(m):
            for root, _, names in os.walk(m):
                files += [os.path.join(root, n) for n in names if n.lower().endswith(".obj")]
        else:
            files.append(m)
    best = []
    for f in files:
        try:
            mn, mx, verts, tris = obj_stats(f)
        except Exception:
            continue
        if not all(math.isfinite(v) for v in mn + mx):
            continue
        msize = [mx[k] - mn[k] for k in range(3)]
        # relative size difference and centre offset, both normalised by the BVH size
        dsize = max(abs(msize[k] - size[k]) / size[k] for k in range(3) if size[k] > 1e-6) if any(s > 1e-6 for s in size) else 1e9
        off = max(abs((mn[k] + mx[k]) / 2 - (bmin[k] + bmax[k]) / 2) / max(size[k], 1e-6) for k in range(3))
        tri_ratio = tris / max(1.0, h.num_leaves * a.tris_per_leaf)
        if dsize <= a.tol and off <= a.tol:
            best.append((dsize + off, f, tuple(round(v, 3) for v in msize), tris, tri_ratio))
    best.sort()
    print("candidates", len(best), "of", len(files), "models")
    for s, f, ms, tris, tr in best[:10]:
        print("  score %.3f %-60s size %s tris %d (x%.2f expected)" % (s, os.path.basename(f), ms, tris, tr))


if __name__ == "__main__":
    main()
