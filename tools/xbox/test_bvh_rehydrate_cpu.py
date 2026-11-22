"""Synthetic unit tests for the Xbox Series BVH CPU rehydration reference model.

All test inputs are synthetically constructed in memory; zero game data is used.
"""
import math
import os
import struct
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import bvh_rehydrate_cpu as bvh


def _pack_compressed_leaf_chunk(entries):
    n = len(entries)
    p0 = bytearray()
    p1 = bytearray()
    p2 = bytearray()
    p3 = bytearray()
    p4 = bytearray()
    for geom_idx, flags, prim0, prim1, vorder in entries:
        p0 += bytes((geom_idx & 0xFF, (geom_idx >> 8) & 0xFF, (geom_idx >> 16) & 0xFF))
        p1.append(flags & 0xFF)
        p2 += bytes((prim0 & 0xFF, (prim0 >> 8) & 0xFF, (prim0 >> 16) & 0xFF))
        pr1 = 0xFFFFFF if prim1 == bvh.INVALID_NODE else (prim1 & 0xFFFFFF)
        p3 += bytes((pr1 & 0xFF, (pr1 >> 8) & 0xFF, (pr1 >> 16) & 0xFF))
        p4.append(vorder & 0xFF)
    return bytes(p0 + p1 + p2 + p3 + p4)


def _pack_box_group(nodes, is_box32):
    """Pack up to 64 compressed Box32 or Box16 nodes into one wave group."""
    assert 1 <= len(nodes) <= 64
    hwords = []
    cids = []
    ovs = []
    for children, overrides in nodes:
        mask = 0
        ordered_vals = []
        for bit_idx in sorted(overrides.keys()):
            mask |= 1 << bit_idx
            ordered_vals.append(overrides[bit_idx])
        hwords.append((len(children) << 24) | mask)
        cids.extend(children)
        ovs.extend(ordered_vals)
    out = bytearray()
    out += struct.pack(f"<{len(hwords)}I", *hwords)
    if cids:
        out += struct.pack(f"<{len(cids)}I", *cids)
    if ovs:
        fmt = f"<{len(ovs)}f" if is_box32 else f"<{len(ovs)}H"
        out += struct.pack(fmt, *ovs)
    return bytes(out)


def _make_bvh_header(
    flags=bvh.BVH_FLAG_BLAS | bvh.BVH_FLAG_COMPRESSED | 0x20006,
    total_bytes=0x400,
    build_id=0x12345678,
    num_leaves=1,
    aabb_min=(-1.0, -2.0, -3.0),
    aabb_max=(4.0, 5.0, 6.0),
    offset_to_hierarchy=0x100,
    header_preserve_flag=0,
    root_node_id=0,
    offset_to_leaves=0x80,
    offset_to_parent_data=0x40,
    transform_word=bvh.DEFAULT_TRANSFORM_WORD,
):
    assert (total_bytes & 63) == 0 and total_bytes >= 64
    return struct.pack(
        "<4I6f6I",
        flags,
        (total_bytes >> 6) - 1,
        build_id,
        num_leaves,
        aabb_min[0], aabb_min[1], aabb_min[2],
        aabb_max[0], aabb_max[1], aabb_max[2],
        offset_to_hierarchy,
        header_preserve_flag,
        root_node_id,
        offset_to_leaves,
        offset_to_parent_data & 0xFFFFFFFF,
        transform_word,
    )


def _pack_rage9_quantized_vb(verts, origin, scale, slot_axis):
    """Build a synthetic RAGE9 8-byte quantized vertex buffer with 32-byte pre-header."""
    hdr = struct.pack("<3fI3fI", origin[0], origin[1], origin[2], slot_axis, scale[0], scale[1], scale[2], 0)
    body = bytearray()
    for pos in verts:
        if slot_axis == 0:
            slots = (pos[2], pos[1], pos[0])
        elif slot_axis == 1:
            slots = (pos[2], pos[0], pos[1])
        else:
            slots = (pos[0], pos[1], pos[2])
        ix = max(0, min(0xFFFFFF, int(round((slots[0] - origin[0]) / scale[0]))))
        iy = max(0, min(0xFFFFFF, int(round((slots[1] - origin[1]) / scale[1]))))
        iz = max(0, min(0xFFFF, int(round((slots[2] - origin[2]) / scale[2]))))
        w0 = (iy & 0xFF) | ((ix & 0xFFFFFF) << 8)
        w1 = ((iy >> 8) & 0xFFFF) | ((iz & 0xFFFF) << 16)
        body += struct.pack("<2I", w0, w1)
    return hdr + bytes(body), 32


def _pack_rage9_meshlet_ib(meshlets):
    """Build a synthetic RAGE9 meshlet index buffer: list of (local_tris, remap_verts)."""
    num_m = len(meshlets)
    m_hdrs = bytearray()
    tri_words = bytearray()
    remaps = bytearray()
    v_off = 0
    t_off = 0
    for m_idx, (local_tris, remap_verts) in enumerate(meshlets):
        mh0 = ((v_off >> 16) & 1) << 14
        mh1 = (v_off & 0xFFFF) | ((t_off & 0xFFFF) << 16)
        m_hdrs += struct.pack("<4I", mh0, mh1, 0, 0)
        for r0, r1, r2 in local_tris:
            w = (r0 & 0x7F) | ((r1 & 0x7F) << 7) | ((r2 & 0x7F) << 14) | ((m_idx & 0x7FF) << 21)
            tri_words += struct.pack("<I", w)
        for rv in remap_verts:
            remaps += struct.pack("<H", rv)
        v_off += len(remap_verts)
        t_off += len(local_tris)
    top = struct.pack("<3I", num_m, t_off, v_off)
    return bytes(top + m_hdrs + tri_words + remaps)


class TestFp16ConservativeRounding(unittest.TestCase):
    def test_enclosure_across_scales(self):
        samples = [
            0.0, -0.0, 1e-8, -1e-8, 2.0 ** -15, -(2.0 ** -15),
            2.0 ** -14, -(2.0 ** -14), 0.1, -0.1, 1.00048828125, -1.00048828125,
            123.456, -123.456, 32769.0, -32769.0, 65504.0, -65504.0,
        ]
        for x in samples:
            lo = struct.unpack("<e", struct.pack("<H", bvh.f32_to_f16_round_down(x)))[0]
            hi = struct.unpack("<e", struct.pack("<H", bvh.f32_to_f16_round_up(x)))[0]
            self.assertLessEqual(lo, x, f"round_down({x})={lo} > {x}")
            self.assertGreaterEqual(hi, x, f"round_up({x})={hi} < {x}")

    def test_infinities_preserved(self):
        self.assertEqual(bvh.f32_to_f16_round_down(float("inf")), 0x7C00)
        self.assertEqual(bvh.f32_to_f16_round_up(float("-inf")), 0xFC00)
        self.assertEqual(bvh.f32_to_f16_round_down(float("-inf")), 0xFC00)
        self.assertEqual(bvh.f32_to_f16_round_up(float("inf")), 0x7C00)

    def test_subnormal_clamping(self):
        self.assertEqual(bvh.f32_to_f16_round_down(1e-7), 0x0000)
        self.assertEqual(bvh.f32_to_f16_round_up(1e-7), 0x0400)
        self.assertEqual(bvh.f32_to_f16_round_down(-1e-7), 0x8400)
        self.assertEqual(bvh.f32_to_f16_round_up(-1e-7), 0x0000)


class TestTransformsAndHeaders(unittest.TestCase):
    def test_rotation_table_orthonormal(self):
        for idx in range(64):
            r = bvh.build_rotation_matrix(idx)
            for a in range(3):
                for c in range(3):
                    dot = sum(r[a][k] * r[c][k] for k in range(3))
                    self.assertAlmostEqual(dot, 1.0 if a == c else 0.0, places=5)
            det = (
                r[0][0] * (r[1][1] * r[2][2] - r[1][2] * r[2][1])
                - r[0][1] * (r[1][0] * r[2][2] - r[1][2] * r[2][0])
                + r[0][2] * (r[1][0] * r[2][1] - r[1][1] * r[2][0])
            )
            self.assertAlmostEqual(det, 1.0, places=5)

    def test_decode_leaf_transform(self):
        h_id = bvh.parse_bvh_header(_make_bvh_header(transform_word=bvh.DEFAULT_TRANSFORM_WORD))
        tp_id = bvh.decode_leaf_transform(h_id)
        self.assertFalse(tp_id.enabled)

        word = 0xC0000000 | (128 << 0) | (129 << 8) | (126 << 16)
        h = bvh.parse_bvh_header(
            _make_bvh_header(aabb_min=(2.0, -4.0, -10.0), aabb_max=(6.0, 4.0, -2.0), transform_word=word)
        )
        tp = bvh.decode_leaf_transform(h)
        self.assertTrue(tp.enabled)
        self.assertEqual(tp.scale, (2.0, 4.0, 0.5))
        self.assertEqual(tp.translation, (4.0, 0.0, -6.0))

    def test_rage9_system_header_and_prefix(self):
        sys_buf = bytearray(208)
        struct.pack_into("<Q", sys_buf, 0x08, 0x50000080)
        struct.pack_into("<I", sys_buf, 0x10, 1)
        struct.pack_into("<Q", sys_buf, 0x18, 0x500000A0)
        struct.pack_into("<Q", sys_buf, 0x20, 0x60000000)
        struct.pack_into("<Q", sys_buf, 0x38, 0x50000050)
        struct.pack_into("<Q", sys_buf, 0x40, 0x0123456789ABCDEF)
        struct.pack_into("<3f", sys_buf, 0x50, 1.0, 0.0, 0.0)
        struct.pack_into("<3f", sys_buf, 0x60, 0.0, 0.0, 1.0)
        struct.pack_into("<3f", sys_buf, 0x70, 0.0, -1.0, 0.0)
        struct.pack_into("<I", sys_buf, 0x88, 0x0101)
        struct.pack_into("<2I", sys_buf, 0xA0, 0x1200, 0x00000340)
        sh = bvh.parse_rage9_system_header(bytes(sys_buf))
        self.assertEqual(sh.bvh_id, 0x0123456789ABCDEF)
        self.assertEqual(sh.aligned_used_size, 0x1200)
        self.assertEqual(sh.transform_3x3[1], (0.0, 0.0, 1.0))

        seg = bytearray(0x500)
        struct.pack_into("<4I", seg, 0x20, 0x4C0, 0, 0x3C0, 0)
        boff, bsz = bvh.locate_bvh_in_data_segment(bytes(seg))
        self.assertEqual((boff, bsz), (0x100, 0x3C0))

    def test_decompress_header_wrapping_parent_offset(self):
        root_off = 0x200
        root_id = ((root_off >> 6) << 3) | bvh.NODE_TYPE_BOX16
        leaf_off = 0x100
        parent_off = (0x50 - (((root_off - leaf_off) >> 6) << 2)) & 0xFFFFFFFF
        src = bytearray(0x280)
        src[:64] = _make_bvh_header(
            total_bytes=0x280,
            root_node_id=root_id,
            offset_to_leaves=leaf_off,
            offset_to_parent_data=parent_off,
        )
        dst = bytearray(0x280)
        bvh.decompress_header(bytes(src), dst)
        out_hdr = bvh.parse_bvh_header(bytes(dst))
        self.assertFalse(out_hdr.is_compressed)
        self.assertEqual(struct.unpack_from("<I", dst, 0x50)[0], bvh.INVALID_NODE)


class TestLeafRehydration(unittest.TestCase):
    def _run_single_leaf(self, geom, vorder=0x09, prim0=0, prim1=bvh.INVALID_NODE, flags=0x01, transform_word=bvh.DEFAULT_TRANSFORM_WORD):
        leaf_off = 0x80
        total = 0x100
        src = bytearray(total)
        src[:64] = _make_bvh_header(
            total_bytes=total,
            num_leaves=1,
            offset_to_leaves=leaf_off,
            offset_to_hierarchy=0xC0,
            root_node_id=((leaf_off >> 6) << 3) | bvh.NODE_TYPE_TRI0,
            transform_word=transform_word,
        )
        chunk = _pack_compressed_leaf_chunk([(0, flags, prim0, prim1, vorder)])
        src[leaf_off : leaf_off + len(chunk)] = chunk
        dst = bytearray(total)
        bvh.rehydrate_leaves(bytes(src), dst, [geom])
        verts = struct.unpack_from("<12f", dst, leaf_off)
        meta = struct.unpack_from("<4I", dst, leaf_off + 48)
        return (verts[0:3], verts[3:6], verts[6:9], verts[9:12]), meta

    def test_all_13_vertex_formats_and_index_modes(self):
        v_f32 = [(1.0, 2.0, 3.0), (4.0, 5.0, 6.0), (7.0, 8.0, 9.0)]
        vb_f32 = b"".join(struct.pack("<3f", *v) for v in v_f32)
        ib_u32 = struct.pack("<3I", 2, 0, 1)
        g_u32 = bvh.TriangleGeometryDesc(
            vertex_buffer=vb_f32, vertex_stride=12, vertex_format=bvh.VERTEX_FORMAT_R32G32B32_FLOAT,
            index_buffer=ib_u32, index_format=bvh.INDEX_FORMAT_R32_UINT,
        )
        (v0, v1, v2, v3), meta = self._run_single_leaf(g_u32)
        self.assertEqual((v0, v1, v2), (v_f32[2], v_f32[0], v_f32[1]))
        self.assertTrue(all(math.isnan(c) for c in v3))
        self.assertEqual(meta, (0x01, 0, bvh.INVALID_NODE, 0x009))

        ib_u16 = struct.pack("<3H", 1, 2, 0)
        g_u16 = bvh.TriangleGeometryDesc(
            vertex_buffer=vb_f32, vertex_stride=12, vertex_format=bvh.VERTEX_FORMAT_R32G32B32_FLOAT,
            index_buffer=ib_u16, index_format=bvh.INDEX_FORMAT_R16_UINT,
        )
        (v0, v1, v2, _), _ = self._run_single_leaf(g_u16)
        self.assertEqual((v0, v1, v2), (v_f32[1], v_f32[2], v_f32[0]))

        for axis in (0, 1, 2):
            vb_q8, voff = _pack_rage9_quantized_vb(v_f32, (-2.0, -2.0, -2.0), (0.001, 0.001, 0.001), axis)
            ib_m = _pack_rage9_meshlet_ib([([(0, 1, 2)], [0, 1, 2])])
            g_q8 = bvh.TriangleGeometryDesc(
                vertex_buffer=vb_q8, vertex_stride=8, vertex_format=bvh.VERTEX_FORMAT_RAGE9_QUANTIZED8,
                vertex_offset=voff, index_buffer=ib_m, index_format=bvh.INDEX_FORMAT_RAGE9_MESHLET,
            )
            (v0, v1, v2, _), _ = self._run_single_leaf(g_q8)
            for got, exp in zip((v0, v1, v2), v_f32):
                for a, b in zip(got, exp):
                    self.assertAlmostEqual(a, b, places=2)

            g_flip = bvh.TriangleGeometryDesc(
                vertex_buffer=vb_q8, vertex_stride=8, vertex_format=bvh.VERTEX_FORMAT_RAGE9_QUANTIZED8,
                vertex_offset=voff, index_buffer=ib_m, index_format=bvh.INDEX_FORMAT_RAGE9_MESHLET,
                winding_flip=True,
            )
            (fv0, fv1, fv2, _), _ = self._run_single_leaf(g_flip)
            for a, b in zip(fv0, v_f32[2]):
                self.assertAlmostEqual(a, b, places=2)

        def pack_h(vals):
            return b"".join(struct.pack("<e", x) for x in vals)

        fmt_cases = [
            (bvh.VERTEX_FORMAT_R16G16B16A16_FLOAT, 8, pack_h([0.5, -1.0, 2.0, 0.0]) * 3, (0.5, -1.0, 2.0)),
            (bvh.VERTEX_FORMAT_R16G16_FLOAT, 4, pack_h([0.25, -0.5]) * 3, (0.25, -0.5, 0.0)),
            (bvh.VERTEX_FORMAT_R16G16B16A16_UNORM, 8, struct.pack("<4H", 65535, 0, 32768, 0) * 3, (1.0, 0.0, 32768.0 / 65535.0)),
            (bvh.VERTEX_FORMAT_R16G16_UNORM, 4, struct.pack("<2H", 65535, 32768) * 3, (1.0, 32768.0 / 65535.0, 0.0)),
            (bvh.VERTEX_FORMAT_R16G16B16A16_SNORM, 8, struct.pack("<4h", 32767, -32768, 0, 0) * 3, (1.0, -1.0, 0.0)),
            (bvh.VERTEX_FORMAT_R16G16_SNORM, 4, struct.pack("<2h", -32768, 32767) * 3, (-1.0, 1.0, 0.0)),
            (bvh.VERTEX_FORMAT_R10G10B10A2_UNORM, 4, struct.pack("<I", 1023 | (511 << 10) | (0 << 20)) * 3, (1.0, 511.0 / 1023.0, 0.0)),
            (bvh.VERTEX_FORMAT_R8G8B8A8_UNORM, 4, bytes([255, 0, 128, 0]) * 3, (1.0, 0.0, 128.0 / 255.0)),
            (bvh.VERTEX_FORMAT_R8G8B8A8_SNORM, 4, struct.pack("<4b", 127, -128, 0, 0) * 3, (1.0, -1.0, 0.0)),
            (bvh.VERTEX_FORMAT_R8G8_UNORM, 2, bytes([255, 128]) * 3, (1.0, 128.0 / 255.0, 0.0)),
            (bvh.VERTEX_FORMAT_R8G8_SNORM, 2, struct.pack("<2b", -128, 127) * 3, (-1.0, 1.0, 0.0)),
        ]
        for fmt, stride, vb_bytes, expected in fmt_cases:
            g = bvh.TriangleGeometryDesc(vertex_buffer=vb_bytes, vertex_stride=stride, vertex_format=fmt)
            (v0, _, _, _), _ = self._run_single_leaf(g)
            for got, exp in zip(v0, expected):
                self.assertAlmostEqual(got, exp, places=4, msg=f"fmt={fmt}")

    def test_paired_triangle_permutation_and_transform(self):
        quad_verts = [(0.0, 0.0, 0.0), (1.0, 0.0, 0.0), (0.0, 1.0, 0.0), (1.0, 1.0, 0.0)]
        vb = b"".join(struct.pack("<3f", *v) for v in quad_verts)
        ib = struct.pack("<6H", 0, 1, 2, 1, 3, 2)
        m3x4 = ((2.0, 0.0, 0.0, 10.0), (0.0, 3.0, 0.0, 20.0), (0.0, 0.0, 1.0, 30.0))
        g = bvh.TriangleGeometryDesc(
            vertex_buffer=vb, vertex_stride=12, vertex_format=bvh.VERTEX_FORMAT_R32G32B32_FLOAT,
            index_buffer=ib, index_format=bvh.INDEX_FORMAT_R16_UINT, transform_3x4=m3x4,
        )
        (v0, v1, v2, v3), meta = self._run_single_leaf(g, vorder=0x94, prim0=0, prim1=1, flags=0x81)
        self.assertEqual(v2, (10.0, 20.0, 30.0))
        self.assertEqual(v0, (12.0, 20.0, 30.0))
        self.assertEqual(v1, (10.0, 23.0, 30.0))
        self.assertEqual(v3, (12.0, 23.0, 30.0))
        self.assertEqual(meta, (0x81, 0, 1, 0x904))


class TestEndToEndBoxRehydration(unittest.TestCase):
    def test_full_multilevel_synthetic_bvh(self):
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
        geom = bvh.TriangleGeometryDesc(vertex_buffer=vb, vertex_stride=12)

        leaf_off = 0x100
        hier_off = 0x300
        lv0_b32_off = hier_off
        lv0_b16_off = hier_off + 128
        lv1_b16_off = hier_off + 192
        total_bytes = 0x440

        leaf_ids = [((leaf_off + i * 64) >> 6) << 3 | bvh.NODE_TYPE_TRI0 for i in range(8)]
        b32_id = ((lv0_b32_off >> 6) << 3) | bvh.NODE_TYPE_BOX32
        b16_id = ((lv0_b16_off >> 6) << 3) | bvh.NODE_TYPE_BOX16
        root_id = ((lv1_b16_off >> 6) << 3) | bvh.NODE_TYPE_BOX16

        src = bytearray(total_bytes)
        src[:64] = _make_bvh_header(
            flags=bvh.BVH_FLAG_BLAS | bvh.BVH_FLAG_COMPRESSED | bvh.BVH_FLAG_UPDATABLE | 0x20006,
            total_bytes=total_bytes,
            num_leaves=8,
            aabb_min=(0.0, 0.0, 0.0),
            aabb_max=(13.0, 5.0, 2.0),
            offset_to_hierarchy=hier_off,
            root_node_id=root_id,
            offset_to_leaves=leaf_off,
            offset_to_parent_data=0x40,
            transform_word=bvh.DEFAULT_TRANSFORM_WORD,
        )
        leaf_chunk = _pack_compressed_leaf_chunk([(0, 0x01, i, bvh.INVALID_NODE, 0x09) for i in range(8)])
        src[leaf_off : leaf_off + len(leaf_chunk)] = leaf_chunk

        g0_b32 = _pack_box_group([(leaf_ids[0:4], {})], is_box32=True)
        src[lv0_b32_off : lv0_b32_off + len(g0_b32)] = g0_b32
        g0_b16 = _pack_box_group([(leaf_ids[4:8], {})], is_box32=False)
        src[lv0_b16_off : lv0_b16_off + len(g0_b16)] = g0_b16
        g1_b16 = _pack_box_group([([b32_id, b16_id], {})], is_box32=False)
        src[lv1_b16_off : lv1_b16_off + len(g1_b16)] = g1_b16

        table = struct.pack("<5I", 128, 192, 192, 256, 2)
        src[total_bytes - len(table) : total_bytes] = table

        dst = bvh.rehydrate_bvh(bytes(src), geometries=[geom])
        rep = bvh.validate_rehydrated_bvh(bytes(dst), check_aabb_enclosure=True, check_parent_pointers=True)
        self.assertTrue(rep.valid, rep.errors)
        self.assertEqual(rep.visited_box32, 1)
        self.assertEqual(rep.visited_box16, 2)
        self.assertEqual(rep.visited_leaves, 8)
        self.assertEqual(rep.max_depth, 2)
        self.assertEqual(rep.root_aabb, (0.0, 0.0, 0.0, 13.0, 5.0, 2.0))

    def test_multigroup_box32_and_box16_overrides(self):
        num_nodes = 70
        leaf_off = 0x400
        hier_off = leaf_off + num_nodes * 64
        total_bytes = hier_off + num_nodes * 128 + 0x100
        src = bytearray(total_bytes)
        dst = bytearray(total_bytes)

        leaf_ids = []
        for i in range(num_nodes):
            lo = leaf_off + i * 64
            x = float(i)
            struct.pack_into(
                "<12f4I", dst, lo,
                x, 0.0, 0.0,
                x + 1.0, 0.0, 0.0,
                x, 1.0, 0.0,
                0.0, 0.0, 0.0,
                0x01, i, bvh.INVALID_NODE, 0x009,
            )
            leaf_ids.append(((lo >> 6) << 3) | bvh.NODE_TYPE_TRI0)

        g0_nodes = [([leaf_ids[i]], {0: -5.5} if i == 3 else {}) for i in range(64)]
        g1_nodes = [([leaf_ids[64 + i]], {3: 99.25} if i == 5 else {}) for i in range(6)]
        p0 = _pack_box_group(g0_nodes, is_box32=True)
        p1 = _pack_box_group(g1_nodes, is_box32=True)
        src[hier_off : hier_off + len(p0)] = p0
        src[hier_off + 8192 : hier_off + 8192 + len(p1)] = p1

        written = bvh.rehydrate_box32(bytes(src), dst, hier_off, 0x40, leaf_off, True, num_nodes)
        self.assertEqual(written, num_nodes)

        n3_box0 = struct.unpack_from("<6f", dst, hier_off + 3 * 128 + 16)
        self.assertEqual(n3_box0[0], -5.5)
        self.assertEqual(n3_box0[3], 4.0)

        n69_off = hier_off + 8192 + 5 * 128
        n69_box0 = struct.unpack_from("<6f", dst, n69_off + 16)
        self.assertEqual(n69_box0[0], 69.0)
        self.assertEqual(n69_box0[3], 99.25)

        n69_cids = struct.unpack_from("<4I", dst, n69_off)
        self.assertEqual(n69_cids[0], leaf_ids[69])
        self.assertEqual(n69_cids[1:], (bvh.INVALID_NODE, bvh.INVALID_NODE, bvh.INVALID_NODE))
        self.assertEqual(struct.unpack_from("<I", dst, 0x40 + 69 * 4)[0], ((n69_off >> 6) << 3) | bvh.NODE_TYPE_BOX32)

        hier16 = hier_off
        src16 = bytearray(total_bytes)
        dst16 = bytearray(dst)
        b16_g0 = _pack_box_group([([leaf_ids[i]], {1: bvh._f32_to_f16_bits(-2.0)} if i == 2 else {}) for i in range(64)], is_box32=False)
        b16_g1 = _pack_box_group([([leaf_ids[64 + i]], {4: bvh._f32_to_f16_bits(12.0)} if i == 5 else {}) for i in range(6)], is_box32=False)
        src16[hier16 : hier16 + len(b16_g0)] = b16_g0
        src16[hier16 + 4096 : hier16 + 4096 + len(b16_g1)] = b16_g1
        written16 = bvh.rehydrate_box16(bytes(src16), dst16, hier16, 0x40, leaf_off, False, num_nodes)
        self.assertEqual(written16, num_nodes)
        n2_h = struct.unpack_from("<6H", dst16, hier16 + 2 * 64 + 16)
        self.assertEqual(struct.unpack("<e", struct.pack("<H", n2_h[1]))[0], -2.0)
        n69_h = struct.unpack_from("<6H", dst16, hier16 + 4096 + 5 * 64 + 16)
        self.assertEqual(struct.unpack("<e", struct.pack("<H", n69_h[4]))[0], 12.0)

    def test_tri1_child_and_decompress_header_modes(self):
        total = 0x300
        leaf_off = 0x100
        hier_off = 0x180
        src = bytearray(total)
        dst = bytearray(total)
        struct.pack_into(
            "<12f4I", dst, leaf_off,
            -999.0, -999.0, -999.0,
            1.0, 2.0, 3.0,
            4.0, 5.0, 6.0,
            2.0, 8.0, 4.0,
            0x81, 0, 1, 0x904,
        )
        tri1_id = ((leaf_off >> 6) << 3) | bvh.NODE_TYPE_TRI1
        g = _pack_box_group([([tri1_id], {})], is_box32=True)
        src[hier_off : hier_off + len(g)] = g
        bvh.rehydrate_box32(bytes(src), dst, hier_off, 0x40, leaf_off, False, 1)
        box0 = struct.unpack_from("<6f", dst, hier_off + 16)
        self.assertEqual(box0, (1.0, 2.0, 3.0, 4.0, 8.0, 6.0))
        root_id = ((hier_off >> 6) << 3) | bvh.NODE_TYPE_BOX32
        dst[:64] = _make_bvh_header(
            flags=bvh.BVH_FLAG_BLAS, total_bytes=total, num_leaves=1,
            offset_to_hierarchy=hier_off, root_node_id=root_id, offset_to_leaves=leaf_off,
        )
        self.assertEqual(bvh.trace_blas(dst, (2.5, 3.5, 0), (0, 0, 1)),
                         bvh.TriangleHit(4.5, tri1_id, 0, 1))

        src_k1 = bytearray(0x200)
        flags_k1 = bvh.BVH_FLAG_BLAS | bvh.BVH_FLAG_COMPRESSED | (1 << 20)
        src_k1[:64] = _make_bvh_header(flags=flags_k1, total_bytes=0x200, offset_to_leaves=0x100, offset_to_parent_data=0xE0)
        src_k1[64:208] = bytes([0xAB] * 144)
        dst_k1 = bytearray(0x200)
        bvh.decompress_header(bytes(src_k1), dst_k1)
        self.assertEqual(dst_k1[64:208], bytes([0xAB] * 144))

        src_pf = bytearray(0x200)
        src_pf[:64] = _make_bvh_header(total_bytes=0x200, header_preserve_flag=1, offset_to_leaves=0x100, offset_to_parent_data=0x80)
        src_pf[64:0x80] = bytes([0xCD] * 0x40)
        dst_pf = bytearray(0x200)
        bvh.decompress_header(bytes(src_pf), dst_pf)
        self.assertEqual(dst_pf[64:0x80], bytes([0xCD] * 0x40))

        inplace = bytearray(src_pf)
        bvh.decompress_header(bytes(inplace), inplace, in_place=True)
        self.assertFalse(bvh.parse_bvh_header(bytes(inplace)).is_compressed)

    def test_multichunk_leaves_and_validation_errors(self):
        num_leaves = 2050
        leaf_off = 0x100
        chunk1_off = leaf_off + (1 << 17)
        total_bytes = chunk1_off + 64 * 4
        src = bytearray(total_bytes)
        src[:64] = _make_bvh_header(
            total_bytes=total_bytes,
            num_leaves=num_leaves,
            offset_to_leaves=leaf_off,
            offset_to_hierarchy=chunk1_off + 128,
            root_node_id=((leaf_off >> 6) << 3) | bvh.NODE_TYPE_TRI0,
        )
        c0 = _pack_compressed_leaf_chunk([(0, 0x01, 0, bvh.INVALID_NODE, 0x09)] * 2048)
        c1 = _pack_compressed_leaf_chunk([(0, 0x01, 1, bvh.INVALID_NODE, 0x09)] * 2)
        src[leaf_off : leaf_off + len(c0)] = c0
        src[chunk1_off : chunk1_off + len(c1)] = c1

        vb = struct.pack("<18f", 0.0, 0.0, 0.0, 1.0, 0.0, 0.0, 0.0, 1.0, 0.0, 5.0, 6.0, 7.0, 8.0, 6.0, 7.0, 5.0, 9.0, 7.0)
        geom = bvh.TriangleGeometryDesc(vertex_buffer=vb, vertex_stride=12)
        dst = bytearray(total_bytes)
        written = bvh.rehydrate_leaves(bytes(src), dst, [geom])
        self.assertEqual(written, num_leaves)
        last_v0 = struct.unpack_from("<3f", dst, chunk1_off + 64)
        self.assertEqual(last_v0, (5.0, 6.0, 7.0))

        bad_tbl = bytearray(0x100)
        bad_tbl[:64] = _make_bvh_header(total_bytes=0x100)
        struct.pack_into("<3I", bad_tbl, 0x100 - 12, 0x80, 0x40, 1)
        with self.assertRaises(ValueError):
            bvh.parse_level_table(bytes(bad_tbl))

        still_comp = bytearray(0x100)
        still_comp[:64] = _make_bvh_header(total_bytes=0x100, offset_to_leaves=0x80, root_node_id=((0x80 >> 6) << 3))
        rep = bvh.validate_rehydrated_bvh(bytes(still_comp), check_parent_pointers=False)
        self.assertFalse(rep.valid)

    def _box16_over_one_leaf(self, halves):
        total, leaf_off, hier_off = 0x200, 0x100, 0x140
        buf = bytearray(total)
        struct.pack_into(
            "<12f4I", buf, leaf_off,
            1.0, 2.0, 3.0, 4.0, 2.0, 3.0, 1.0, 5.0, 6.0, 0.0, 0.0, 0.0,
            0x01, 0, bvh.INVALID_NODE, 0x009,
        )
        leaf_id = ((leaf_off >> 6) << 3) | bvh.NODE_TYPE_TRI0
        struct.pack_into("<4I", buf, hier_off, leaf_id, bvh.INVALID_NODE, bvh.INVALID_NODE, bvh.INVALID_NODE)
        struct.pack_into("<6H", buf, hier_off + 16, *halves)
        root_id = ((hier_off >> 6) << 3) | bvh.NODE_TYPE_BOX16
        buf[:64] = _make_bvh_header(
            flags=bvh.BVH_FLAG_BLAS, total_bytes=total, num_leaves=1,
            offset_to_hierarchy=hier_off, root_node_id=root_id, offset_to_leaves=leaf_off,
        )
        return bytes(buf)

    def test_infinite_box16_bound_is_valid_and_encloses(self):
        # The builder writes +-inf (0x7C00 / 0xFC00) when a bound exceeds half range; that is a conservative bound.
        h = bvh._f32_to_f16_bits
        buf = self._box16_over_one_leaf((0xFC00, h(2.0), h(3.0), 0x7C00, h(5.0), h(6.0)))
        rep = bvh.validate_rehydrated_bvh(buf, check_aabb_enclosure=True, check_parent_pointers=False)
        self.assertTrue(rep.valid, rep.errors)
        self.assertEqual(rep.visited_leaves, 1)

        # An infinite bound that is too small on the finite axes still fails enclosure.
        tight = self._box16_over_one_leaf((0xFC00, h(2.0), h(3.0), 0x7C00, h(4.0), h(6.0)))
        rep = bvh.validate_rehydrated_bvh(tight, check_aabb_enclosure=True, check_parent_pointers=False)
        self.assertFalse(rep.valid)
        self.assertTrue(any("does not enclose" in e for e in rep.errors), rep.errors)

    def test_nan_box16_bound_is_an_error(self):
        h = bvh._f32_to_f16_bits
        buf = self._box16_over_one_leaf((0x7E00, h(2.0), h(3.0), h(4.0), h(5.0), h(6.0)))
        rep = bvh.validate_rehydrated_bvh(buf, check_aabb_enclosure=True, check_parent_pointers=False)
        self.assertFalse(rep.valid)
        self.assertTrue(any("NaN AABB" in e for e in rep.errors), rep.errors)


class TestBvhTraversal(unittest.TestCase):
    def test_nearest_hit_miss_and_interval(self):
        for box_type, fmt in ((bvh.NODE_TYPE_BOX16, "<6e"), (bvh.NODE_TYPE_BOX32, "<6f")):
            with self.subTest(box_type=box_type):
                leaf_off, root_off, total = 0x40, 0xC0, 0x140
                far_id, near_id = 8, 16
                root_id = ((root_off >> 6) << 3) | box_type
                buf = bytearray(total)
                buf[:64] = _make_bvh_header(
                    flags=bvh.BVH_FLAG_BLAS, total_bytes=total, num_leaves=2,
                    aabb_min=(0, 0, 2), aabb_max=(1, 1, 4),
                    offset_to_hierarchy=root_off, root_node_id=root_id,
                    offset_to_leaves=leaf_off, offset_to_parent_data=0,
                    transform_word=bvh.DEFAULT_TRANSFORM_WORD,
                )
                struct.pack_into("<4I", buf, root_off, near_id, far_id, bvh.INVALID_NODE, bvh.INVALID_NODE)
                for slot, z in enumerate((2, 4)):
                    struct.pack_into(fmt, buf, root_off + 16 + slot * struct.calcsize(fmt), 0, 0, z, 1, 1, z)
                for offset, z, prim in ((leaf_off, 4, 7), (leaf_off + 64, 2, 9)):
                    struct.pack_into("<9f", buf, offset, 0, 0, z, 1, 0, z, 0, 1, z)
                    struct.pack_into("<3I", buf, offset + 48, 3 << 8, prim, bvh.INVALID_NODE)

                origin, direction = (0.25, 0.25, 0), (0, 0, 2)
                self.assertEqual(bvh.trace_blas(buf, origin, direction), bvh.TriangleHit(1, near_id, 3, 9))
                self.assertEqual(bvh.trace_blas(buf, origin, direction, 1.5, 2), bvh.TriangleHit(2, far_id, 3, 7))
                self.assertIsNone(bvh.trace_blas(buf, origin, direction, t_max=0.5))
                self.assertIsNone(bvh.trace_blas(buf, (2, 0, 0), direction))
                self.assertIsNone(bvh.trace_blas(buf, (0.75, 0.75, 0), direction))
                self.assertEqual(bvh.trace_blas(buf, (0, 0, 6), (0, 0, -1)),
                                 bvh.TriangleHit(2, far_id, 3, 7))


if __name__ == "__main__":
    unittest.main()
