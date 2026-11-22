"""Find and verify the drawable geometry a dehydrated BLAS was built from, scoring every candidate by its geometry.

For one BLAS this:
  1. reads the triangle count per leaf geometry index (exact: each leaf holds one or two primitive slots);
  2. enumerates the drawables of a levels window and keeps every model whose geometry triangle counts contain that signature as an
     ordered subsequence (a prefix is the common case; the BLAS can leave geometries out);
  3. for each candidate and each run of adjacent graphics pages that holds its vertex and index buffers, decodes the used
     geometries (separate quantized-8 position stream, meshlet triangles), applies the BLAS's instance transform, rehydrates with the
     CPU model and scores the result with bvh_rehydrate_cpu.stored_geometry_checks: leaves inside the header bounds, leaf union equal
     to the header bounds, stored box overrides consistent, and the internal box area as the tie-break.
--unordered also tries the geometries in any order, not only as an ordered subsequence.
A candidate is accepted only if the checks are consistent. Prints counts and stream offsets; drawable names only with --names, and
they stay local. With --write <dir> the best candidate's float3/uint32 buffers and export.json are written for
compare_bvh_geometry.py.

usage: python -I rage9_score_blas.py <window.rpf> <blas.bin> <instance208.bin> [--runs N] [--embeddings N] [--unordered] [--names] [--write DIR]
"""
import collections
import json
import os
import struct
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import bvh_rehydrate_cpu as bvh
from rage9_dump_cell import BASE_SYS, MAGIC, W, streams
from rage9_export_geometry import decode_positions, decode_triangles, read_instance_transform

MAX_RUN_PAGES = 64
NEAR_PAGES = 48


def leaf_signature(src):
    per = collections.Counter()
    for leaf in bvh.decode_compressed_leaves(src):
        per[leaf.geom_index] += 1 if leaf.prim_index_1 == bvh.INVALID_NODE else 2
    return [per[i] for i in range(max(per) + 1)]


def embeddings(sig, counts, cap, ordered=True):
    """Every way to pick distinct geometries of `counts` matching `sig` (index lists), at most `cap` of them.

    ordered: the picked indices must increase (an ordered subsequence); otherwise any order is allowed.
    """
    out = []

    def walk(i, j, acc):
        if len(out) >= cap:
            return
        if i == len(sig):
            out.append(list(acc))
            return
        for k in (range(j, len(counts)) if ordered else range(len(counts))):
            if counts[k] == sig[i] and k not in acc:
                acc.append(k)
                walk(i + 1, k + 1, acc)
                acc.pop()

    walk(0, 0, [])
    return out


def scan(window, sig, cap, ordered=True):
    """Return (pages, candidates). pages: [(file offset, raw)] of graphics pages in stored order; candidates: dicts."""
    pages, cands, seen = [], [], set()
    for off, raw in streams(window):
        if raw[:4] == MAGIC:
            pages.append((off, raw))
            continue
        w = W(raw)
        for i in range(0, len(raw) - 8, 4):
            p = struct.unpack_from("<Q", raw, i)[0]
            if not (BASE_SYS <= p < BASE_SYS + len(raw)):
                continue
            name_at = p - BASE_SYS
            end = raw.find(b"\0", name_at, name_at + 128)
            if end - name_at < 3 or not all(32 <= c < 127 for c in raw[name_at:end]):
                continue
            d = i - 168
            if d < 0:
                continue
            try:
                geos = w.geos(d)
            except Exception:
                continue
            name = raw[name_at:end].decode()
            for mi in sorted({g["model"] for g in geos}):
                model = [g for g in geos if g["model"] == mi]
                counts = [g["triangles"] for g in model]
                key = (name, mi, tuple(counts), tuple(g["vb"] for g in model))
                if key in seen:
                    continue
                seen.add(key)
                for used in embeddings(sig, counts, cap, ordered):
                    cands.append({"stream": off, "name": name, "model": mi, "counts": counts, "used": used,
                                  "geos": [model[k] for k in used]})
    pages.sort(key=lambda x: x[0])
    return pages, cands


class PageRun:
    def __init__(self, pages, start, n):
        self.start, self.n = start, n
        self.parts = [pages[start + k][1] for k in range(n)]
        self.size = sum(len(p) for p in self.parts)

    def bytes(self, off, length):
        if off < 0 or off + length > self.size:
            return None
        out = bytearray()
        for part in self.parts:
            if off < len(part):
                take = min(len(part) - off, length - len(out))
                out += part[off:off + take]
                off = 0
                if len(out) == length:
                    break
            else:
                off -= len(part)
        return bytes(out)


def valid_runs(pages, geos, stream_off, near):
    """Page runs that hold every used buffer and validate every vertex-buffer header.

    The run must cover the highest byte any used buffer reaches, so its length follows from the page sizes; only start pages within
    `near` pages of the drawable's system stream are tried, nearest first.
    """
    need = max(max(g["vb"] + g["vb_len"], g["ib"] + g["ib_len"]) for g in geos)
    pos = min(range(len(pages)), key=lambda k: abs(pages[k][0] - stream_off)) if pages else 0
    starts = sorted(range(max(0, pos - near), min(len(pages), pos + near + 1)), key=lambda k: abs(pages[k][0] - stream_off))
    runs = []
    for start in starts:
        size, n = 0, 0
        while size < need and start + n < len(pages) and n < MAX_RUN_PAGES:
            size += len(pages[start + n][1])
            n += 1
        if size < need:
            continue
        run = PageRun(pages, start, n)
        if all((struct.unpack("<I", run.bytes(g["vb"], 4))[0] & 0x7F) == 0x42 for g in geos):
            runs.append(run)
    return runs


def run_diagnostics(pages, geos, stream_off):
    """Why no run validated: bytes needed, the run that covers them from the nearest page, headers that pass there."""
    need = max(max(g["vb"] + g["vb_len"], g["ib"] + g["ib_len"]) for g in geos)
    pos = min(range(len(pages)), key=lambda k: abs(pages[k][0] - stream_off))
    size = n = 0
    while size < need and pos + n < len(pages):
        size += len(pages[pos + n][1])
        n += 1
    run = PageRun(pages, pos, n)
    ok = sum(1 for g in geos if run.bytes(g["vb"], 4) and (struct.unpack("<I", run.bytes(g["vb"], 4))[0] & 0x7F) == 0x42)
    return "need %d bytes; from page 0x%x it takes %d pages; %d of %d vertex headers valid there" % (
        need, pages[pos][0], n, ok, len(geos))


def build(run, geos, transform):
    out = []
    for g in geos:
        vb = run.bytes(g["vb"], g["vb_len"])
        ib = run.bytes(g["ib"], g["ib_len"])
        f3 = decode_positions(vb, g["pos_off"], g["vertices"], g["stride"], g["pos_fmt"], transform)
        tri = decode_triangles(ib, g["triangles"])
        out.append((f3, tri))
    return out


def score(src, built):
    descs = [bvh.TriangleGeometryDesc(vertex_buffer=f3, vertex_stride=12, index_buffer=tri,
                                      index_format=bvh.INDEX_FORMAT_R32_UINT) for f3, tri in built]
    return bvh.stored_geometry_checks(src, bytes(bvh.rehydrate_bvh(src, geometries=descs)))


def main(argv):
    opts = {"--runs": 6, "--embeddings": 64, "--write": None}
    names = "--names" in argv
    ordered = "--unordered" not in argv
    argv = [a for a in argv if a not in ("--names", "--unordered")]
    for k in list(opts):
        if k in argv:
            i = argv.index(k)
            opts[k] = argv[i + 1] if k == "--write" else int(argv[i + 1])
            argv = argv[:i] + argv[i + 2:]
    window, blas_path, desc_path = argv[:3]
    src = open(blas_path, "rb").read()
    transform = read_instance_transform(desc_path)
    sig = leaf_signature(src)
    print("%s: %d leaves, triangles per geometry index %s" % (os.path.basename(blas_path), bvh.parse_bvh_header(src).num_leaves, sig))
    pages, cands = scan(window, sig, opts["--embeddings"], ordered)
    print("graphics pages %d, candidate (drawable, model, embedding) %d" % (len(pages), len(cands)))
    results = []
    for c in cands:
        runs = valid_runs(pages, c["geos"], c["stream"], NEAR_PAGES)
        if not runs:
            print("   stream 0x%x m%d used %s: no page run (%s)" % (c["stream"], c["model"], c["used"],
                                                                   run_diagnostics(pages, c["geos"], c["stream"])))
        for run in runs[:opts["--runs"]]:
            try:
                built = build(run, c["geos"], transform)
                chk = score(src, built)
            except Exception as e:      # a wrong run can hold indices beyond the vertex count
                print("   stream 0x%x m%d used %s run 0x%x: %s" % (c["stream"], c["model"], c["used"], pages[run.start][0], type(e).__name__))
                continue
            results.append((not chk.consistent, chk.internal_box_area, chk, c, run, built))
    results.sort(key=lambda r: (r[0], r[1]))
    best_area = next((r[1] for r in results if not r[0]), None)
    for bad, area, chk, c, run, _ in results[:12]:
        label = ("%-40s " % c["name"]) if names else ""
        ratio = "%.3f" % (area / best_area) if best_area else "-"
        print("   %s%-10s stream 0x%-8x m%d used %-24s run 0x%x+%d | outside %d bounds %.1e overrides %d/%d area x%s" % (
            label, "CONSISTENT" if not bad else "rejected", c["stream"], c["model"], c["used"], pages[run.start][0], run.n,
            chk.leaves_outside_header, chk.header_bounds_error, chk.overrides_consistent, chk.overrides_inconsistent, ratio))
    accepted = [r for r in results if not r[0]]
    print("accepted %d of %d scored" % (len(accepted), len(results)))
    if opts["--write"] and results:
        out = opts["--write"]
        os.makedirs(out, exist_ok=True)
        rejected, _, chk, c, run, built = (accepted or results)[0]   # the best rejected one too, marked, for offline analysis
        export = []
        for i, (f3, tri) in enumerate(built):
            open(os.path.join(out, "g%d_f3.bin" % i), "wb").write(f3)
            open(os.path.join(out, "g%d_tri.bin" % i), "wb").write(tri)
            export.append({"index": i, "drawable_geometry": c["used"][i], "vertices": len(f3) // 12, "triangles": len(tri) // 12,
                           "vertex_format": bvh.VERTEX_FORMAT_R32G32B32_FLOAT, "index_format": bvh.INDEX_FORMAT_R32_UINT,
                           "stride": 12, "vb": "g%d_f3.bin" % i, "ib": "g%d_tri.bin" % i, "instance_transform": True,
                           "consistent": not rejected})
        json.dump(export, open(os.path.join(out, "export.json"), "w"), indent=1)
        print("wrote", out)
    return 0 if accepted else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
