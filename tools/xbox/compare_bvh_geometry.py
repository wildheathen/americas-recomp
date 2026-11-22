"""Compare the CPU rehydration model and the library on one compressed BLAS and one or more geometries, and report what a
geometry fixes: finite/non-inverted leaf bounds, degenerate leaves, index range, and the byte differences between model and library.

usage: python -I compare_bvh_geometry.py <src.bin> <vb.bin> <ib.bin> [--stride 12] [--vertex-count N] [--vertex-format 6]
       [--index-format 42] [--dll <rehydrate_cpu.exe>] [--out report.json]
       python -I compare_bvh_geometry.py <src.bin> --geom <vb> <ib|-> <stride> <count> <vertexFormat> <indexFormat> [--geom ...]
The DLL run is optional; without --dll only the model and the geometry checks run. Nothing from the input is written outside --out.
"""
import argparse, json, math, os, struct, subprocess, sys, tempfile
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import bvh_rehydrate_cpu as bvh


def leaf_metrics(dst: bytes, header) -> dict:
    off, left = header.offset_to_leaves, header.num_leaves
    nonfinite = inverted = degenerate = 0
    lo = [math.inf] * 3
    hi = [-math.inf] * 3
    for i in range(left):
        base = off + (i << 6)
        if base + 64 > len(dst):
            break
        v = struct.unpack_from("<12f", dst, base)
        flags = struct.unpack_from("<I", dst, base + 48)[0]
        pts = [v[0:3], v[3:6], v[6:9]] + ([v[9:12]] if (flags & 0x80) else [])
        if any(not math.isfinite(c) for p in pts for c in p):
            nonfinite += 1
            continue
        mn = [min(p[k] for p in pts) for k in range(3)]
        mx = [max(p[k] for p in pts) for k in range(3)]
        if any(mn[k] > mx[k] for k in range(3)):
            inverted += 1
        if all(abs(pts[0][k] - pts[1][k]) < 1e-9 and abs(pts[0][k] - pts[2][k]) < 1e-9 for k in range(3)):
            degenerate += 1
        for k in range(3):
            lo[k] = min(lo[k], mn[k])
            hi[k] = max(hi[k], mx[k])
    return {"leaves": left, "non_finite": nonfinite, "inverted": inverted, "degenerate": degenerate,
            "coord_min": lo, "coord_max": hi}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("src")
    ap.add_argument("vb", nargs="?")
    ap.add_argument("ib", nargs="?")
    ap.add_argument("--geom", nargs="+", action="append",
                    metavar=("VB IB STRIDE COUNT VFMT IFMT [VOFF]"))
    ap.add_argument("--stride", type=int, default=12)
    ap.add_argument("--vertex-count", type=int, default=0)
    ap.add_argument("--vertex-format", type=int, default=6)
    ap.add_argument("--index-format", type=int, default=42)
    ap.add_argument("--dll", default="")
    ap.add_argument("--out", default="")
    a = ap.parse_args()
    specs = []
    if a.geom:
        specs = [s for s in a.geom]
    elif a.vb and a.ib:
        specs = [[a.vb, a.ib, str(a.stride), str(a.vertex_count), str(a.vertex_format), str(a.index_format)]]
    else:
        ap.error("give vb/ib or at least one --geom")

    src = open(a.src, "rb").read()
    header = bvh.parse_bvh_header(src)
    leaves = bvh.decode_compressed_leaves(src)
    max_geom = max((l.geom_index for l in leaves), default=0)
    geometries = []
    checks = []
    for vb_path, ib_path, stride, count, vfmt, ifmt, *rest in specs:
        vb = open(vb_path, "rb").read()
        ib = b"" if ib_path == "-" else open(ib_path, "rb").read()
        stride = int(stride); vfmt = int(vfmt); ifmt = int(ifmt)
        voff = int(rest[0]) if rest else 0
        count = int(count) or (len(vb) // stride if stride else 0)
        ifmt_py = (bvh.INDEX_FORMAT_R32_UINT if ifmt == 42 else
                   bvh.INDEX_FORMAT_RAGE9_MESHLET if ifmt == 53 else bvh.INDEX_FORMAT_R16_UINT)
        geometries.append(bvh.TriangleGeometryDesc(vertex_buffer=vb, vertex_stride=stride, vertex_format=vfmt,
                                                  vertex_offset=voff,
                                                  index_buffer=(ib or None), index_format=(ifmt_py if ib else bvh.INDEX_FORMAT_NONE)))
        checks.append({"vb": os.path.basename(vb_path), "ib": os.path.basename(ib_path), "stride": stride,
                       "vertices": count, "vertex_format": vfmt, "index_format": ifmt, "vertex_offset": voff,
                       "triangles_available": len(ib) // ((6 if ifmt == 57 else 12) if ib else 1)})
    max_prim = max((max(l.prim_index_0, l.prim_index_1) for l in leaves
                    if max(l.prim_index_0, l.prim_index_1) != bvh.INVALID_NODE), default=0)
    res = {"source": os.path.basename(a.src), "geometries": checks,
           "header": {"total_bytes": header.total_bytes, "num_leaves": header.num_leaves,
                      "transform_word": header.transform_word},
           "geometry_checks": {"leaf_count": len(leaves), "geometry_indices": sorted({l.geom_index for l in leaves}),
                               "max_geometry_index": max_geom, "geometries_provided": len(geometries),
                               "max_valid_primitive": max_prim}}

    model = bytes(bvh.rehydrate_bvh(src, geometries=geometries))
    rep = bvh.validate_rehydrated_bvh(model, check_aabb_enclosure=True, check_parent_pointers=True)
    res["model"] = {"valid": rep.valid, "errors": len(rep.errors),
                    "error_kinds": sorted({("does not enclose" if "does not enclose" in e else
                                           "NaN AABB" if "NaN AABB" in e else
                                           "inverted AABB" if "Inverted" in e else e.split(" ")[0]) for e in rep.errors}),
                    "box32": rep.visited_box32, "box16": rep.visited_box16, "leaves": rep.visited_leaves,
                    "leaf_bounds": leaf_metrics(model, header)}

    if a.dll:
        out_dir = tempfile.mkdtemp(prefix="bvh_cmp_")
        dll_out = os.path.join(out_dir, "dll.bin")
        if len(specs) > 1 or (specs[0][6:] if len(specs[0]) > 6 else []):
            cmd = [a.dll, a.src, dll_out]
            for s in specs:
                cmd += ["--geom"] + [str(x) for x in s]
        else:
            s = specs[0]
            cmd = [a.dll, a.src, s[0], dll_out, s[2], s[3] or "0", s[4], s[1], s[5]]
        r = subprocess.run(cmd, capture_output=True, text=True, cwd=os.path.dirname(os.path.abspath(a.dll)))
        dll = open(dll_out, "rb").read() if os.path.exists(dll_out) else b""
        diff = sum(1 for x, y in zip(dll, model) if x != y) if dll else None
        first = next((i for i, (x, y) in enumerate(zip(dll, model)) if x != y), None) if dll else None
        res["library"] = {"returncode": r.returncode, "bytes": len(dll), "diff_bytes": diff, "first_diff": first,
                          "stdout": r.stdout.strip().splitlines()[-2:]}
        if dll:
            res["library"]["leaf_bounds"] = leaf_metrics(dll, header)

    text = json.dumps(res, indent=1)
    if a.out:
        open(a.out, "w").write(text)
        print("wrote", a.out)
    print(text)


if __name__ == "__main__":
    main()
