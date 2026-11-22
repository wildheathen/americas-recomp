"""Find one decompressed BVH data segment inside a GTA VI Xbox core_bvh4.rpf, for local testing only (no game data in the repo).

The archive layout is documented in docs/xbox/BVH_CONTAINER.md: nested archives start with "9FPR" at 4 KiB boundaries, each has 16 zero
bytes followed by zlib streams, and a data segment holds a small prefix whose dword at +0x20 is the end offset and at +0x28 the BVH size,
with the flagged 64-byte Xbox BVH header at +0x100.

usage: python -I find_bvh_sample.py <core_bvh4.rpf> <out_dir> [max_samples=1] [scan_mb=512]
Writes bvh_<n>.bin (the compressed BLAS starting at its 64-byte header) and bvh_<n>.json (header fields).
"""
import json, os, struct, sys, zlib

FLAG_BLAS = 0x80000000
FLAG_COMPRESSED = 0x01000000


def parse_header(d, off=0):
    if len(d) < off + 64:
        return None
    flags, blocks_minus1, build_id, num_leaves = struct.unpack_from("<4I", d, off)
    if (flags & 0xFF000000) != 0x81000000 or not (flags & FLAG_COMPRESSED):
        return None
    total = ((blocks_minus1 + 1) << 6)
    if total < 0x40 or off + total > len(d):
        return None
    hier, _, root, leaves, parents, xform = struct.unpack_from("<6I", d, off + 0x28)
    if not (0 < hier < total and 0 < leaves < total):
        return None
    return {"flags": flags, "total_bytes": total, "num_leaves": num_leaves, "offset_to_hierarchy": hier,
            "root_node_id": root, "offset_to_leaves": leaves, "offset_to_parent_data": parents, "transform_word": xform}


def main():
    path, out = sys.argv[1], sys.argv[2]
    want = int(sys.argv[3]) if len(sys.argv) > 3 else 1
    scan_mb = int(sys.argv[4]) if len(sys.argv) > 4 else 512
    os.makedirs(out, exist_ok=True)
    found = attempts = 0
    total_read = 0
    with open(path, "rb") as f:
        pos = 0
        chunk = 1 << 22
        carry = b""
        while total_read < scan_mb << 20 and found < want:
            buf = f.read(chunk)
            if not buf:
                break
            total_read += len(buf)
            data = carry + buf
            base = pos - len(carry)
            i = data.find(b"\x78\xda")
            while i >= 0 and found < want:
                abs_off = base + i
                if abs_off % 16 == 0:                      # streams start 16-byte aligned
                    attempts += 1
                    try:
                        dec = zlib.decompressobj()
                        raw = dec.decompress(data[i:i + (8 << 20)], 64 << 20)
                        if len(raw) > 0x160:
                            h = parse_header(raw, 0x100) or parse_header(raw, 0)
                            if h:
                                hdr_at = 0x100 if parse_header(raw, 0x100) else 0
                                seg = raw[hdr_at:hdr_at + h["total_bytes"]]
                                if len(seg) == h["total_bytes"]:
                                    n = found
                                    open(os.path.join(out, "bvh_%02d.bin" % n), "wb").write(seg)
                                    h["file_offset"] = abs_off
                                    h["header_offset"] = hdr_at
                                    json.dump(h, open(os.path.join(out, "bvh_%02d.json" % n), "w"), indent=1)
                                    print("sample %d at file 0x%x: %d bytes, %d leaves" % (n, abs_off, len(seg), h["num_leaves"]))
                                    found += 1
                    except zlib.error:
                        pass
                i = data.find(b"\x78\xda", i + 1)
            carry = data[-16:]
            pos += len(buf)
    print("found %d samples in %.0f MB, %d inflate attempts" % (found, total_read / (1 << 20), attempts))
    return 0 if found else 1


if __name__ == "__main__":
    sys.exit(main())
