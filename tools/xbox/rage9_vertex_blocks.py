"""First milestone of the RAGE9 drawable decoder: parse the tagged vertex blocks (42 40 00 00) of a levels window and print
the per-tag vertex counts, so a block can be matched to a known mesh by its vertex count.

Block layout (public measurement): the first words are tagged stream locations, the low byte is the tag and twice the upper 24 bits
is the byte offset from the block start, slot 0 starts at +0x80. Tag 0x42 is 8 bytes per referenced vertex.

Measured limit: the span-derived counts do not equal the exporter's vertex counts for the same models (no exact match for twelve known
counts in a 3 GB window, 1,359 such blocks), so the authoritative count has to be read from the vertex buffer descriptor
(geometry +8) and the meshlet descriptor (geometry +64), not inferred from the spans. This script is the block layer only.

usage: python -I rage9_vertex_blocks.py <window.rpf> [min_vertices]
"""
import os, struct, sys, zlib

WIDTHS = {0x42: 8}
TARGET = b"\x42\x40\x00\x00"


def streams(path):
    with open(path, "rb") as f:
        pos, carry = 0, b""
        data_all = f.read()
    i = 0
    while True:
        i = data_all.find(b"\x78\xda", i)
        if i < 0:
            break
        if i % 16 == 0:
            try:
                d = zlib.decompressobj()
                raw = d.decompress(data_all[i:i + (8 << 20)], 64 << 20)
                if d.eof and raw:
                    yield i, raw
            except zlib.error:
                pass
        i += 1


def parse_block(raw):
    if len(raw) < 0x80 or raw[:4] != TARGET:
        return None
    words = struct.unpack_from("<23I", raw, 0)
    slots = []
    for w in words:
        tag = w & 0xFF
        off = ((w >> 8) & 0xFFFFFF) * 2
        if tag:
            slots.append((off, tag))
    slots.sort()
    if not slots:
        return None
    if slots[0][0] != 0x80:
        return None
    out = []
    for k, (off, tag) in enumerate(slots):
        end = slots[k + 1][0] if k + 1 < len(slots) else None
        width = WIDTHS.get(tag)
        count = None
        if end is not None and width:
            count = (end - off) // width
        out.append((tag, off, end, count))
    return out


def main():
    path = sys.argv[1]
    minv = int(sys.argv[2]) if len(sys.argv) > 2 else 0
    blocks = 0
    hits = []
    for off, raw in streams(path):
        if raw[:4] != TARGET:
            continue
        blocks += 1
        slots = parse_block(raw)
        if not slots:
            continue
        for tag, start, end, count in slots:
            if tag == 0x42 and count and count >= minv:
                hits.append((count, off, start, end))
    print("blocks with the 42 40 00 00 start:", blocks, "tag-0x42 counts:", len(hits))
    for count, off, start, end in sorted(hits, reverse=True)[:40]:
        print("  vertices %-7d stream 0x%-8x span 0x%x..0x%x" % (count, off, start, end if end else 0))


if __name__ == "__main__":
    main()
