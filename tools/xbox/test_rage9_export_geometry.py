"""Unit tests for rage9_export_geometry: the quantized-8 position stream and the instance transform (synthetic data only)."""
import os
import struct
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import bvh_rehydrate_cpu as bvh
import rage9_export_geometry as exp


def _pack_q8(ix, iy, iz):
    return struct.pack("<2I", (ix << 8) | (iy & 0xFF), ((iy >> 8) & 0xFFFF) | (iz << 16))


def _vertex_block(points, other_bytes_per_vertex, origin=(0.0, 0.0, 0.0), scale=(1.0, 1.0, 1.0), axis=2):
    """128-byte head (quantized header at 96), the position stream, then a second attribute stream."""
    head = bytearray(128)
    struct.pack_into("<3fI3fI", head, 96, *origin, axis, *scale, 0)
    positions = b"".join(_pack_q8(*p) for p in points)
    other = bytes([0xFF]) * (other_bytes_per_vertex * len(points))
    return bytes(head) + positions + other


class TestPositionStream(unittest.TestCase):
    def test_positions_are_one_contiguous_stream(self):
        pts = [(1, 2, 3), (40, 50, 60), (700, 800, 900)]
        vb = _vertex_block(pts, other_bytes_per_vertex=12)
        whole_record = (len(vb) - 128) // len(pts)   # 20 bytes per vertex over both streams
        self.assertEqual(whole_record, 20)
        f3 = exp.decode_positions(vb, 128, len(pts), whole_record, bvh.VERTEX_FORMAT_RAGE9_QUANTIZED8)
        got = [struct.unpack_from("<3f", f3, i * 12) for i in range(len(pts))]
        self.assertEqual(got, [tuple(float(c) for c in p) for p in pts])

    def test_origin_scale_and_axis(self):
        vb = _vertex_block([(4, 6, 8)], 4, origin=(1.0, 2.0, 3.0), scale=(0.5, 0.25, 2.0), axis=0)
        f3 = exp.decode_positions(vb, 128, 1, 12, bvh.VERTEX_FORMAT_RAGE9_QUANTIZED8)
        # components (3.0, 3.5, 19.0); axis 0 stores the 16-bit component on x
        self.assertEqual(struct.unpack("<3f", f3), (19.0, 3.5, 3.0))

    def test_instance_transform_is_applied(self):
        rot_y = ((0.0, 0.0, 1.0, 0.0), (0.0, 1.0, 0.0, 0.0), (-1.0, 0.0, 0.0, 10.0))
        vb = _vertex_block([(1, 2, 3)], 4)
        f3 = exp.decode_positions(vb, 128, 1, 12, bvh.VERTEX_FORMAT_RAGE9_QUANTIZED8, rot_y)
        self.assertEqual(struct.unpack("<3f", f3), (3.0, 2.0, 9.0))

    def test_read_instance_transform(self):
        desc = bytearray(208)
        rows = (1.0, 2.0, 3.0, 4.0, 5.0, 6.0, 7.0, 8.0, 9.0, 10.0, 11.0, 12.0)
        struct.pack_into("<12f", desc, exp.INSTANCE_TRANSFORM_OFFSET, *rows)
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "desc.bin")
            with open(p, "wb") as f:
                f.write(desc)
            self.assertEqual(exp.read_instance_transform(p), (rows[0:4], rows[4:8], rows[8:12]))


if __name__ == "__main__":
    unittest.main()
