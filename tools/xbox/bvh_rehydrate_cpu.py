"""CPU reference model for Xbox Series dehydrated BVH container decoding and rehydration.

Models the four XboxBvhBuilder rehydration compute shaders (DecompressHeader,
LeafRehydration, Box16Rehydration, Box32Rehydration) and the RAGE9 208-byte
system header / 256-byte data-segment prefix that wrap dehydrated BLAS blobs.
"""
from dataclasses import dataclass
import math
import struct
from typing import Dict, List, Optional, Sequence, Tuple

BVH_FLAG_COMPRESSED = 0x01000000
BVH_FLAG_UPDATABLE = 0x00040000
BVH_FLAG_BLAS = 0x80000000
DEFAULT_TRANSFORM_WORD = 0x007F7F7F

NODE_TYPE_TRI0 = 0
NODE_TYPE_TRI1 = 1
NODE_TYPE_BOX16 = 4
NODE_TYPE_BOX32 = 5
INVALID_NODE = 0xFFFFFFFF

INDEX_FORMAT_NONE = 0
INDEX_FORMAT_R32_UINT = 42
INDEX_FORMAT_R16_UINT = 57
INDEX_FORMAT_RAGE9_MESHLET = 53

VERTEX_FORMAT_R32G32B32_FLOAT = 6
VERTEX_FORMAT_R16G16B16A16_FLOAT = 10
VERTEX_FORMAT_R16G16B16A16_UNORM = 11
VERTEX_FORMAT_R16G16B16A16_SNORM = 13
VERTEX_FORMAT_RAGE9_QUANTIZED8 = 16
VERTEX_FORMAT_R10G10B10A2_UNORM = 24
VERTEX_FORMAT_R8G8B8A8_UNORM = 28
VERTEX_FORMAT_R8G8B8A8_SNORM = 31
VERTEX_FORMAT_R16G16_FLOAT = 34
VERTEX_FORMAT_R16G16_UNORM = 35
VERTEX_FORMAT_R16G16_SNORM = 37
VERTEX_FORMAT_R8G8_UNORM = 49
VERTEX_FORMAT_R8G8_SNORM = 51

_POS_INF = 3.4028234663852886e38      # the library and the shaders mark empty bounds with FLT_MAX, not infinity
_NEG_INF = -3.4028234663852886e38
_QNAN_BITS = 0x7FC00000
_QNAN = struct.unpack("<f", struct.pack("<I", _QNAN_BITS))[0]
_HALF_POS_INF_BITS = 0x7C00           # the builder writes infinity when a box exceeds half range; 0x7BFF (65504) would clamp
_HALF_NEG_INF_BITS = 0xFC00
_MIN_NORMAL_F16 = 2.0 ** -14
_U32_MASK = 0xFFFFFFFF
_VERTEX_SLOT_TABLE = (2, 0, 1)
_ROT_COS_TABLE = (
    1.0,
    math.cos(math.pi / 8.0),
    math.cos(math.pi / 4.0),
    math.cos(3.0 * math.pi / 8.0),
    0.0,
)


def _f32(val: float) -> float:
    return struct.unpack("<f", struct.pack("<f", val))[0]


def _fma32(a: float, b: float, c: float) -> float:
    return _f32(float(a) * float(b) + float(c))


def _f32_to_u32(val: float) -> int:
    return struct.unpack("<I", struct.pack("<f", val))[0]


def _u32_to_f32(bits: int) -> float:
    return struct.unpack("<f", struct.pack("<I", bits & _U32_MASK))[0]


def _f16_bits_to_f32(bits: int) -> float:
    return struct.unpack("<e", struct.pack("<H", bits & 0xFFFF))[0]


def _f32_to_f16_bits(val: float) -> int:
    try:
        return struct.unpack("<H", struct.pack("<e", val))[0]
    except OverflowError:
        return _HALF_NEG_INF_BITS if val < 0.0 else _HALF_POS_INF_BITS


def _fmin_num(a: float, b: float) -> float:
    if math.isnan(a):
        return b
    if math.isnan(b):
        return a
    return a if a <= b else b


def _fmax_num(a: float, b: float) -> float:
    if math.isnan(a):
        return b
    if math.isnan(b):
        return a
    return a if a >= b else b


def _f16_min_bits(a_bits: int, b_bits: int) -> int:
    a = _f16_bits_to_f32(a_bits)
    b = _f16_bits_to_f32(b_bits)
    if math.isnan(a):
        return b_bits & 0xFFFF
    if math.isnan(b):
        return a_bits & 0xFFFF
    return (a_bits if a <= b else b_bits) & 0xFFFF


def _f16_max_bits(a_bits: int, b_bits: int) -> int:
    a = _f16_bits_to_f32(a_bits)
    b = _f16_bits_to_f32(b_bits)
    if math.isnan(a):
        return b_bits & 0xFFFF
    if math.isnan(b):
        return a_bits & 0xFFFF
    return (a_bits if a >= b else b_bits) & 0xFFFF


def f32_to_f16_round_down(val: float) -> int:
    """Conservatively round an IEEE-754 float32 downward to FP16 bits (Box16Rehydration min bound)."""
    if math.isnan(val):
        return _f32_to_f16_bits(val)
    bits = _f32_to_u32(val)
    if val < 0.0:
        rounded = _u32_to_f32((bits + 8191) & ~8191)
        clamped = _fmin_num(rounded, -_MIN_NORMAL_F16)
    else:
        rounded = _u32_to_f32(bits & ~8191)
        clamped = 0.0 if rounded < _MIN_NORMAL_F16 else rounded
    return _f32_to_f16_bits(clamped)


def f32_to_f16_round_up(val: float) -> int:
    """Conservatively round an IEEE-754 float32 upward to FP16 bits (Box16Rehydration max bound)."""
    if math.isnan(val):
        return _f32_to_f16_bits(val)
    bits = _f32_to_u32(val)
    if val > 0.0:
        rounded = _u32_to_f32((bits + 8191) & ~8191)
        clamped = _fmax_num(rounded, _MIN_NORMAL_F16)
    else:
        rounded = _u32_to_f32(bits & ~8191)
        clamped = 0.0 if rounded > -_MIN_NORMAL_F16 else rounded
    return _f32_to_f16_bits(clamped)


def _sext(val: int, bits: int) -> int:
    sign = 1 << (bits - 1)
    mask = (1 << bits) - 1
    v = val & mask
    return (v ^ sign) - sign


def _matmul3x3(
    a: Tuple[Tuple[float, float, float], ...],
    b: Tuple[Tuple[float, float, float], ...],
) -> Tuple[Tuple[float, float, float], Tuple[float, float, float], Tuple[float, float, float]]:
    rows = []
    for r in range(3):
        row = []
        for c in range(3):
            s = _f32(0.0)
            for m in range(3):
                s = _f32(s + _f32(a[r][m] * b[m][c]))
            row.append(s)
        rows.append((row[0], row[1], row[2]))
    return (rows[0], rows[1], rows[2])


def build_rotation_matrix(index: int) -> Tuple[Tuple[float, float, float], Tuple[float, float, float], Tuple[float, float, float]]:
    """Return one of the 64 discrete 3x3 OBB rotation matrices Rz * Ry * Rx (steps of pi/8)."""
    idx = index & 63
    i = idx & 3
    j = (idx >> 2) & 3
    k = (idx >> 4) & 3
    cx, sx = _f32(_ROT_COS_TABLE[i]), _f32(_ROT_COS_TABLE[4 - i])
    cy, sy = _f32(_ROT_COS_TABLE[j]), _f32(_ROT_COS_TABLE[4 - j])
    cz, sz = _f32(_ROT_COS_TABLE[k]), _f32(_ROT_COS_TABLE[4 - k])
    rx = ((1.0, 0.0, 0.0), (0.0, cx, sx), (0.0, -sx, cx))
    ry = ((cy, 0.0, -sy), (0.0, 1.0, 0.0), (sy, 0.0, cy))
    rz = ((cz, sz, 0.0), (-sz, cz, 0.0), (0.0, 0.0, 1.0))
    return _matmul3x3(rz, _matmul3x3(ry, rx))


@dataclass(frozen=True)
class BvhHeader:
    """64-byte Xbox Series BLAS/TLAS acceleration-structure header."""

    flags: int
    num_blocks_minus_one: int
    build_id: int
    num_leaves: int
    aabb_min: Tuple[float, float, float]
    aabb_max: Tuple[float, float, float]
    offset_to_hierarchy: int
    header_preserve_flag: int
    root_node_id: int
    offset_to_leaves: int
    offset_to_parent_data: int
    transform_word: int

    @property
    def is_compressed(self) -> bool:
        return bool(self.flags & BVH_FLAG_COMPRESSED)

    @property
    def is_updatable(self) -> bool:
        return bool(self.flags & BVH_FLAG_UPDATABLE)

    @property
    def is_blas(self) -> bool:
        return bool(self.flags & BVH_FLAG_BLAS)

    @property
    def total_bytes(self) -> int:
        return (self.num_blocks_minus_one + 1) << 6

    @property
    def root_node_type(self) -> int:
        return self.root_node_id & 7

    @property
    def root_node_offset(self) -> int:
        return (self.root_node_id >> 3) << 6

    def preserve_byte_count(self) -> int:
        if self.header_preserve_flag != 0:
            return self.offset_to_parent_data
        k = (self.flags >> 20) & 0xF
        return ((k * k * 9) << 4) + 64


@dataclass(frozen=True)
class LevelEntry:
    """One bottom-up level in the trailing dehydrated BVH level offset table."""

    level: int
    box32_start: int
    box32_end: int
    box16_start: int
    box16_end: int

    @property
    def num_box32(self) -> int:
        return (self.box32_end - self.box32_start) >> 7

    @property
    def num_box16(self) -> int:
        return (self.box16_end - self.box16_start) >> 6


@dataclass(frozen=True)
class Rage9BvhSystemHeader:
    """208-byte per-BVH RAGE9 system-segment header paired with each BVH data stream."""

    ptr_meta: int
    count: int
    ptr_size_block: int
    ptr_gpu_data: int
    ptr_aux0: int
    ptr_aux1: int
    ptr_transform: int
    bvh_id: int
    transform_3x3: Tuple[Tuple[float, float, float], Tuple[float, float, float], Tuple[float, float, float]]
    flags_word: int
    aligned_used_size: int
    packed_meta: int

    @property
    def transform_3x4(self) -> Tuple[Tuple[float, float, float, float], ...]:
        return tuple((r[0], r[1], r[2], 0.0) for r in self.transform_3x3)


@dataclass(frozen=True)
class LeafTransformParams:
    """Pre-box quantization transform derived from BvhHeader.transform_word and AABB."""

    enabled: bool
    rotation_3x3: Tuple[Tuple[float, float, float], Tuple[float, float, float], Tuple[float, float, float]]
    translation: Tuple[float, float, float]
    scale: Tuple[float, float, float]


@dataclass(frozen=True)
class LeafDescriptor:
    """Decoded compressed leaf metadata from the 5 byte-planes of a 2048-leaf chunk."""

    geom_index: int
    flags: int
    prim_index_0: int
    prim_index_1: int
    vertex_order: int


@dataclass(frozen=True)
class TriangleGeometryDesc:
    """CPU equivalent of D3D12_RAYTRACING_GEOMETRY_TRIANGLES_DESC for LeafRehydration."""

    vertex_buffer: bytes
    vertex_stride: int
    vertex_format: int = VERTEX_FORMAT_R32G32B32_FLOAT
    vertex_offset: int = 0
    index_buffer: Optional[bytes] = None
    index_format: int = INDEX_FORMAT_NONE
    index_offset: int = 0
    transform_3x4: Optional[Tuple[Tuple[float, float, float, float], ...]] = None
    winding_flip: bool = False


@dataclass(frozen=True)
class ValidationReport:
    """Structural and numeric validation results for a rehydrated BVH buffer."""

    valid: bool
    errors: Tuple[str, ...]
    root_node_id: int
    visited_box32: int
    visited_box16: int
    visited_leaves: int
    max_depth: int
    root_aabb: Optional[Tuple[float, float, float, float, float, float]]


def parse_bvh_header(data: bytes, offset: int = 0) -> BvhHeader:
    """Parse a 64-byte Xbox BVH header at `offset`."""
    if offset < 0 or offset + 64 > len(data):
        raise ValueError(f"BVH header out of range: offset={offset}, len={len(data)}")
    w0, w1, w2, w3 = struct.unpack_from("<4I", data, offset)
    f0, f1, f2, f3, f4, f5 = struct.unpack_from("<6f", data, offset + 16)
    w10, w11, w12, w13, w14, w15 = struct.unpack_from("<6I", data, offset + 40)
    return BvhHeader(
        flags=w0,
        num_blocks_minus_one=w1,
        build_id=w2,
        num_leaves=w3,
        aabb_min=(f0, f1, f2),
        aabb_max=(f3, f4, f5),
        offset_to_hierarchy=w10,
        header_preserve_flag=w11,
        root_node_id=w12,
        offset_to_leaves=w13,
        offset_to_parent_data=w14,
        transform_word=w15,
    )


def parse_rage9_system_header(data: bytes) -> Rage9BvhSystemHeader:
    """Parse a 208-byte RAGE9 per-BVH system-segment header."""
    if len(data) != 208:
        raise ValueError(f"Expected 208-byte RAGE9 BVH system header, got {len(data)}")
    q = struct.unpack_from("<9Q", data, 0)
    r0 = struct.unpack_from("<3f", data, 0x50)
    r1 = struct.unpack_from("<3f", data, 0x60)
    r2 = struct.unpack_from("<3f", data, 0x70)
    flags_word = struct.unpack_from("<I", data, 0x88)[0]
    aligned_used_size, packed_meta = struct.unpack_from("<2I", data, 0xA0)
    return Rage9BvhSystemHeader(
        ptr_meta=q[1],
        count=q[2] & _U32_MASK,
        ptr_size_block=q[3],
        ptr_gpu_data=q[4],
        ptr_aux0=q[5],
        ptr_aux1=q[6],
        ptr_transform=q[7],
        bvh_id=q[8],
        transform_3x3=(r0, r1, r2),
        flags_word=flags_word,
        aligned_used_size=aligned_used_size,
        packed_meta=packed_meta,
    )


def locate_bvh_in_data_segment(data: bytes) -> Tuple[int, int]:
    """Return `(bvh_offset, bvh_byte_size)` inside a RAGE9 BVH graphics/data segment."""
    if len(data) < 0x40:
        raise ValueError(f"Data segment too small: {len(data)}")
    end_off, _, bvh_size, _ = struct.unpack_from("<4I", data, 0x20)
    if bvh_size == 0 or end_off < bvh_size or end_off > len(data):
        raise ValueError(
            f"Invalid RAGE9 BVH prefix at +0x20: end=0x{end_off:x}, size=0x{bvh_size:x}, len=0x{len(data):x}"
        )
    bvh_offset = end_off - bvh_size
    if bvh_offset + 64 > len(data):
        raise ValueError(f"BVH header does not fit at offset 0x{bvh_offset:x}")
    return bvh_offset, bvh_size


def parse_level_table(bvh_bytes: bytes, header: Optional[BvhHeader] = None) -> List[LevelEntry]:
    """Decode the trailing bottom-up `(box32_end, box16_end)` level table at the end of a dehydrated BVH."""
    hdr = header or parse_bvh_header(bvh_bytes)
    total = hdr.total_bytes
    if total < 68 or total > len(bvh_bytes):
        raise ValueError(f"Invalid BVH total_bytes=0x{total:x} for buffer len=0x{len(bvh_bytes):x}")
    num_levels = struct.unpack_from("<I", bvh_bytes, total - 4)[0]
    table_off = total - 4 - (num_levels * 8)
    if table_off < 64:
        raise ValueError(f"Invalid level count {num_levels} at end of 0x{total:x}-byte BVH")
    raw = struct.unpack_from(f"<{num_levels * 2}I", bvh_bytes, table_off)
    levels: List[LevelEntry] = []
    prev = 0
    for lv in range(num_levels):
        b32_end = raw[2 * lv]
        b16_end = raw[2 * lv + 1]
        if b32_end < prev or b16_end < b32_end:
            raise ValueError(
                f"Non-monotonic level table at level {lv}: prev=0x{prev:x}, b32_end=0x{b32_end:x}, b16_end=0x{b16_end:x}"
            )
        if ((b32_end - prev) & 127) != 0 or ((b16_end - b32_end) & 63) != 0:
            raise ValueError(
                f"Misaligned level slice at level {lv}: b32_bytes=0x{b32_end - prev:x}, b16_bytes=0x{b16_end - b32_end:x}"
            )
        levels.append(LevelEntry(lv, prev, b32_end, b32_end, b16_end))
        prev = b16_end
    return levels


def decode_leaf_transform(header: BvhHeader) -> LeafTransformParams:
    """Decode LeafRehydrationParams transform fields from BvhHeader.transform_word and root AABB."""
    word = header.transform_word
    if word == DEFAULT_TRANSFORM_WORD:
        ident = ((1.0, 0.0, 0.0), (0.0, 1.0, 0.0), (0.0, 0.0, 1.0))
        return LeafTransformParams(False, ident, (0.0, 0.0, 0.0), (1.0, 1.0, 1.0))
    trans = [0.0, 0.0, 0.0]
    if (word >> 30) & 1:
        for ax in range(3):
            lo = header.aabb_min[ax]
            hi = header.aabb_max[ax]
            if lo > 0.0 or hi < 0.0:
                trans[ax] = _f32(_f32(lo + hi) * 0.5)
    if (word >> 31) & 1:
        sx = _u32_to_f32((word & 0xFF) << 23)
        sy = _u32_to_f32(((word >> 8) & 0xFF) << 23)
        sz = _u32_to_f32(((word >> 16) & 0xFF) << 23)
        scale = (sx, sy, sz)
    else:
        scale = (1.0, 1.0, 1.0)
    rot_idx = (word >> 24) & 0x3F
    rot = build_rotation_matrix(rot_idx)
    return LeafTransformParams(True, rot, (trans[0], trans[1], trans[2]), scale)


def decode_compressed_leaves(
    bvh_bytes: bytes,
    offset_to_leaves: Optional[int] = None,
    num_leaves: Optional[int] = None,
) -> List[LeafDescriptor]:
    """Decode all compressed leaf metadata entries from the 5 byte-planes per 2048-leaf chunk."""
    hdr = parse_bvh_header(bvh_bytes) if (offset_to_leaves is None or num_leaves is None) else None
    leaf_off = hdr.offset_to_leaves if offset_to_leaves is None else offset_to_leaves
    total_leaves = hdr.num_leaves if num_leaves is None else num_leaves
    out: List[LeafDescriptor] = []
    num_chunks = (total_leaves + 2047) // 2048
    for g in range(num_chunks):
        count = min(2048, total_leaves - g * 2048)
        base = leaf_off + (g << 17)
        end_needed = base + count * 11
        if end_needed > len(bvh_bytes):
            raise ValueError(
                f"Compressed leaf chunk {g} out of range: need 0x{end_needed:x}, len=0x{len(bvh_bytes):x}"
            )
        for i in range(count):
            p0 = base + i * 3
            geom_idx = bvh_bytes[p0] | (bvh_bytes[p0 + 1] << 8) | (bvh_bytes[p0 + 2] << 16)
            flags = bvh_bytes[base + count * 3 + i]
            p2 = base + count * 4 + i * 3
            prim0 = bvh_bytes[p2] | (bvh_bytes[p2 + 1] << 8) | (bvh_bytes[p2 + 2] << 16)
            p3 = base + count * 7 + i * 3
            prim1 = bvh_bytes[p3] | (bvh_bytes[p3 + 1] << 8) | (bvh_bytes[p3 + 2] << 16)
            if prim1 == 0xFFFFFF:
                prim1 = INVALID_NODE
            vorder = bvh_bytes[base + count * 10 + i]
            out.append(LeafDescriptor(geom_idx, flags, prim0, prim1, vorder))
    return out


def decompress_header(
    src: bytes,
    dst: bytearray,
    size_to_preserve_div16: Optional[int] = None,
    in_place: bool = False,
) -> None:
    """CPU reference of DecompressHeader compute shader (blob_04)."""
    hdr = parse_bvh_header(src)
    if size_to_preserve_div16 is None:
        if in_place:
            size_to_preserve_div16 = 1
        else:
            size_to_preserve_div16 = (hdr.preserve_byte_count() + 15) >> 4
    copy_bytes = size_to_preserve_div16 * 16
    if copy_bytes > len(src) or copy_bytes > len(dst):
        raise ValueError(
            f"DecompressHeader copy exceeds buffer: copy=0x{copy_bytes:x}, src=0x{len(src):x}, dst=0x{len(dst):x}"
        )
    if copy_bytes > 0:
        dst[:copy_bytes] = src[:copy_bytes]
        w0 = struct.unpack_from("<I", src, 0)[0] & (~BVH_FLAG_COMPRESSED & _U32_MASK)
        struct.pack_into("<I", dst, 0, w0)
        root_off = ((hdr.root_node_id >> 3) << 6) & _U32_MASK
        slot_delta = ((root_off - hdr.offset_to_leaves) & _U32_MASK) >> 6
        parent_addr = (hdr.offset_to_parent_data + (slot_delta << 2)) & _U32_MASK
        if parent_addr + 4 <= len(dst):
            struct.pack_into("<I", dst, parent_addr, INVALID_NODE)


def _read_triangle_indices(geom: TriangleGeometryDesc, prim_idx: int) -> Tuple[int, int, int]:
    fmt = geom.index_format
    if fmt == INDEX_FORMAT_R32_UINT:
        if geom.index_buffer is None:
            raise ValueError("R32_UINT geometry requires index_buffer")
        off = geom.index_offset + prim_idx * 12
        return struct.unpack_from("<3I", geom.index_buffer, off)
    if fmt == INDEX_FORMAT_R16_UINT:
        if geom.index_buffer is None:
            raise ValueError("R16_UINT geometry requires index_buffer")
        off = geom.index_offset + prim_idx * 6
        return struct.unpack_from("<3H", geom.index_buffer, off)
    if fmt == INDEX_FORMAT_RAGE9_MESHLET:
        if geom.index_buffer is None:
            raise ValueError("RAGE9 meshlet geometry requires index_buffer")
        ib = geom.index_buffer
        base = geom.index_offset
        dw0, dw1, _ = struct.unpack_from("<3I", ib, base)
        tri_rel = ((dw0 << 4) & 0x1FFFF0) | 12
        tri_word = struct.unpack_from("<I", ib, base + tri_rel + prim_idx * 4)[0]
        meshlet_idx = tri_word >> 21
        mh_rel = (meshlet_idx << 4) | 12
        mh0, mh1 = struct.unpack_from("<2I", ib, base + mh_rel)
        v_off = (mh1 & 0xFFFF) | ((mh0 << 2) & 0x10000)
        remap_base = base + tri_rel + dw1 * 4 + v_off * 2
        i0 = struct.unpack_from("<H", ib, remap_base + (tri_word & 0x7F) * 2)[0]
        i1 = struct.unpack_from("<H", ib, remap_base + ((tri_word >> 7) & 0x7F) * 2)[0]
        i2 = struct.unpack_from("<H", ib, remap_base + ((tri_word >> 14) & 0x7F) * 2)[0]
        if geom.winding_flip:
            return (i2, i1, i0)
        return (i0, i1, i2)
    b = prim_idx * 3
    return (b, b + 1, b + 2)


def _read_vertex(geom: TriangleGeometryDesc, vertex_idx: int) -> Tuple[float, float, float]:
    vb = geom.vertex_buffer
    stride = geom.vertex_stride & 0x7FFF
    off = geom.vertex_offset + vertex_idx * stride
    fmt = geom.vertex_format
    if fmt == VERTEX_FORMAT_R32G32B32_FLOAT:
        return struct.unpack_from("<3f", vb, off)
    if fmt == VERTEX_FORMAT_RAGE9_QUANTIZED8:
        hdr_off = geom.vertex_offset - 32
        ox, oy, oz, axis = struct.unpack_from("<3fI", vb, hdr_off)
        sx, sy, sz = struct.unpack_from("<3f", vb, geom.vertex_offset - 16)
        w0, w1 = struct.unpack_from("<2I", vb, off)
        ix = (w0 >> 8) & 0xFFFFFF
        iy = (w0 & 0xFF) | ((w1 & 0xFFFF) << 8)
        iz = (w1 >> 16) & 0xFFFF
        s0 = _f32(_f32(float(ix) * sx) + ox)
        s1 = _f32(_f32(float(iy) * sy) + oy)
        s2 = _f32(_f32(float(iz) * sz) + oz)
        if axis == 0:
            return (s2, s1, s0)
        if axis == 1:
            return (s1, s2, s0)
        return (s0, s1, s2)
    if fmt == VERTEX_FORMAT_R16G16B16A16_FLOAT:
        h0, h1, h2 = struct.unpack_from("<3H", vb, off)
        return (_f16_bits_to_f32(h0), _f16_bits_to_f32(h1), _f16_bits_to_f32(h2))
    if fmt == VERTEX_FORMAT_R16G16_FLOAT:
        h0, h1 = struct.unpack_from("<2H", vb, off)
        return (_f16_bits_to_f32(h0), _f16_bits_to_f32(h1), 0.0)
    if fmt == VERTEX_FORMAT_R16G16B16A16_UNORM:
        u0, u1, u2 = struct.unpack_from("<3H", vb, off)
        s = _f32(1.0 / 65535.0)
        return (_f32(float(u0) * s), _f32(float(u1) * s), _f32(float(u2) * s))
    if fmt == VERTEX_FORMAT_R16G16_UNORM:
        u0, u1 = struct.unpack_from("<2H", vb, off)
        s = _f32(1.0 / 65535.0)
        return (_f32(float(u0) * s), _f32(float(u1) * s), 0.0)
    if fmt == VERTEX_FORMAT_R16G16B16A16_SNORM:
        s0, s1, s2 = struct.unpack_from("<3h", vb, off)
        s = _f32(1.0 / 32767.0)
        return (
            _fmax_num(_f32(float(s0) * s), -1.0),
            _fmax_num(_f32(float(s1) * s), -1.0),
            _fmax_num(_f32(float(s2) * s), -1.0),
        )
    if fmt == VERTEX_FORMAT_R16G16_SNORM:
        s0, s1 = struct.unpack_from("<2h", vb, off)
        s = _f32(1.0 / 32767.0)
        return (
            _fmax_num(_f32(float(s0) * s), -1.0),
            _fmax_num(_f32(float(s1) * s), -1.0),
            0.0,
        )
    if fmt == VERTEX_FORMAT_R10G10B10A2_UNORM:
        w = struct.unpack_from("<I", vb, off)[0]
        s = _f32(1.0 / 1023.0)
        return (
            _f32(float(w & 0x3FF) * s),
            _f32(float((w >> 10) & 0x3FF) * s),
            _f32(float((w >> 20) & 0x3FF) * s),
        )
    if fmt == VERTEX_FORMAT_R8G8B8A8_UNORM:
        b0, b1, b2 = vb[off], vb[off + 1], vb[off + 2]
        s = _f32(1.0 / 255.0)
        return (_f32(float(b0) * s), _f32(float(b1) * s), _f32(float(b2) * s))
    if fmt == VERTEX_FORMAT_R8G8B8A8_SNORM:
        s0, s1, s2 = _sext(vb[off], 8), _sext(vb[off + 1], 8), _sext(vb[off + 2], 8)
        s = _f32(1.0 / 127.0)
        return (
            _fmax_num(_f32(float(s0) * s), -1.0),
            _fmax_num(_f32(float(s1) * s), -1.0),
            _fmax_num(_f32(float(s2) * s), -1.0),
        )
    if fmt == VERTEX_FORMAT_R8G8_UNORM:
        b0, b1 = vb[off], vb[off + 1]
        s = _f32(1.0 / 255.0)
        return (_f32(float(b0) * s), _f32(float(b1) * s), 0.0)
    if fmt == VERTEX_FORMAT_R8G8_SNORM:
        s0, s1 = _sext(vb[off], 8), _sext(vb[off + 1], 8)
        s = _f32(1.0 / 127.0)
        return (
            _fmax_num(_f32(float(s0) * s), -1.0),
            _fmax_num(_f32(float(s1) * s), -1.0),
            0.0,
        )
    raise ValueError(f"Unsupported DXGI vertex format: {fmt}")


def _apply_transform_3x4(
    v: Tuple[float, float, float],
    m: Tuple[Tuple[float, float, float, float], ...],
) -> Tuple[float, float, float]:
    out = []
    for r in range(3):
        val = _f32(m[r][0] * v[0])
        val = _fma32(m[r][1], v[1], val)
        val = _fma32(m[r][2], v[2], val)
        val = _f32(val + m[r][3])
        out.append(val)
    return (out[0], out[1], out[2])


def _apply_leaf_transform(
    v: Tuple[float, float, float],
    tp: LeafTransformParams,
) -> Tuple[float, float, float]:
    out = []
    for r in range(3):
        rot_v = _f32(tp.rotation_3x3[r][0] * v[0])
        rot_v = _fma32(tp.rotation_3x3[r][1], v[1], rot_v)
        rot_v = _fma32(tp.rotation_3x3[r][2], v[2], rot_v)
        shifted = _f32(rot_v - tp.translation[r])
        out.append(_f32(shifted * tp.scale[r]))
    return (out[0], out[1], out[2])


def rehydrate_leaves(
    src: bytes,
    dst: bytearray,
    geometries: Sequence[TriangleGeometryDesc],
    offset_to_leaves: Optional[int] = None,
    num_elements: Optional[int] = None,
    transform_params: Optional[LeafTransformParams] = None,
) -> int:
    """CPU reference of LeafRehydration compute shader (blob_03). Returns number of leaves written."""
    hdr = parse_bvh_header(src) if (offset_to_leaves is None or num_elements is None or transform_params is None) else None
    leaf_off = hdr.offset_to_leaves if offset_to_leaves is None else offset_to_leaves
    total = hdr.num_leaves if num_elements is None else num_elements
    tp = decode_leaf_transform(hdr) if transform_params is None else transform_params

    descriptors = decode_compressed_leaves(src, leaf_off, total)
    num_chunks = (total + 2047) // 2048
    idx = 0
    for g in range(num_chunks):
        count = min(2048, total - g * 2048)
        chunk_base = leaf_off + (g << 17)
        chunk_nodes: List[bytes] = []
        for i in range(count):
            desc = descriptors[idx]
            idx += 1
            if desc.geom_index >= len(geometries):
                raise ValueError(
                    f"Leaf {idx - 1} references geometry {desc.geom_index}, only {len(geometries)} provided"
                )
            geom = geometries[desc.geom_index]
            i0, i1, i2 = _read_triangle_indices(geom, desc.prim_index_0)
            v_raw = [_read_vertex(geom, i0), _read_vertex(geom, i1), _read_vertex(geom, i2)]
            has_tri1 = desc.prim_index_1 != INVALID_NODE
            if has_tri1:
                j0, j1, j2 = _read_triangle_indices(geom, desc.prim_index_1)
                if (desc.vertex_order & 0x20) != 0:
                    j_extra = j0
                elif (desc.vertex_order & 0x80) != 0:
                    j_extra = j1
                else:
                    j_extra = j2
                v3 = _read_vertex(geom, j_extra)
            else:
                v3 = (_QNAN, _QNAN, _QNAN)

            if geom.transform_3x4 is not None:
                v_raw = [_apply_transform_3x4(v, geom.transform_3x4) for v in v_raw]
                if has_tri1:
                    v3 = _apply_transform_3x4(v3, geom.transform_3x4)

            if tp.enabled:
                v_raw = [_apply_leaf_transform(v, tp) for v in v_raw]
                if has_tri1:
                    v3 = _apply_leaf_transform(v3, tp)

            p0 = desc.vertex_order & 3
            p1 = (desc.vertex_order >> 2) & 3
            p2 = (p0 ^ 3) - p1
            verts: List[Tuple[float, float, float]] = [(0.0, 0.0, 0.0)] * 4
            verts[_VERTEX_SLOT_TABLE[p0]] = v_raw[0]
            verts[_VERTEX_SLOT_TABLE[p1]] = v_raw[1]
            verts[_VERTEX_SLOT_TABLE[p2]] = v_raw[2]
            verts[3] = v3

            dw48 = ((desc.geom_index << 8) | desc.flags) & _U32_MASK
            dw52 = desc.prim_index_0 & _U32_MASK
            dw56 = desc.prim_index_1 & _U32_MASK
            dw60 = (desc.vertex_order & 0xF) | ((desc.vertex_order << 4) & 0xF00)
            chunk_nodes.append(
                struct.pack(
                    "<12f4I",
                    verts[0][0], verts[0][1], verts[0][2],
                    verts[1][0], verts[1][1], verts[1][2],
                    verts[2][0], verts[2][1], verts[2][2],
                    verts[3][0], verts[3][1], verts[3][2],
                    dw48, dw52, dw56, dw60,
                )
            )
        for i, node_bytes in enumerate(chunk_nodes):
            out_off = chunk_base + (i << 6)
            dst[out_off : out_off + 64] = node_bytes
    return total


def _child_aabb_from_dst(dst: bytes, child_id: int) -> Tuple[float, float, float, float, float, float]:
    node_type = child_id & 7
    node_off = (child_id >> 3) << 6
    if node_type == NODE_TYPE_TRI0:
        v = struct.unpack_from("<12f", dst, node_off)
        flags = struct.unpack_from("<I", dst, node_off + 48)[0]
        mnx = _fmin_num(_fmin_num(v[0], v[3]), v[6])
        mny = _fmin_num(_fmin_num(v[1], v[4]), v[7])
        mnz = _fmin_num(_fmin_num(v[2], v[5]), v[8])
        mxx = _fmax_num(_fmax_num(v[0], v[3]), v[6])
        mxy = _fmax_num(_fmax_num(v[1], v[4]), v[7])
        mxz = _fmax_num(_fmax_num(v[2], v[5]), v[8])
        if flags & 0x80:
            mnx = _fmin_num(mnx, v[9])
            mny = _fmin_num(mny, v[10])
            mnz = _fmin_num(mnz, v[11])
            mxx = _fmax_num(mxx, v[9])
            mxy = _fmax_num(mxy, v[10])
            mxz = _fmax_num(mxz, v[11])
        return (mnx, mny, mnz, mxx, mxy, mxz)
    if node_type == NODE_TYPE_TRI1:
        v = struct.unpack_from("<9f", dst, node_off + 12)
        mnx = _fmin_num(_fmin_num(v[0], v[3]), v[6])
        mny = _fmin_num(_fmin_num(v[1], v[4]), v[7])
        mnz = _fmin_num(_fmin_num(v[2], v[5]), v[8])
        mxx = _fmax_num(_fmax_num(v[0], v[3]), v[6])
        mxy = _fmax_num(_fmax_num(v[1], v[4]), v[7])
        mxz = _fmax_num(_fmax_num(v[2], v[5]), v[8])
        return (mnx, mny, mnz, mxx, mxy, mxz)
    if node_type == NODE_TYPE_BOX16:
        h = struct.unpack_from("<24H", dst, node_off + 16)
        min_x = _f16_min_bits(_f16_min_bits(h[0], h[6]), _f16_min_bits(h[12], h[18]))
        min_y = _f16_min_bits(_f16_min_bits(h[1], h[7]), _f16_min_bits(h[13], h[19]))
        min_z = _f16_min_bits(_f16_min_bits(h[2], h[8]), _f16_min_bits(h[14], h[20]))
        max_x = _f16_max_bits(_f16_max_bits(h[3], h[9]), _f16_max_bits(h[15], h[21]))
        max_y = _f16_max_bits(_f16_max_bits(h[4], h[10]), _f16_max_bits(h[16], h[22]))
        max_z = _f16_max_bits(_f16_max_bits(h[5], h[11]), _f16_max_bits(h[17], h[23]))
        return (
            _f16_bits_to_f32(min_x),
            _f16_bits_to_f32(min_y),
            _f16_bits_to_f32(min_z),
            _f16_bits_to_f32(max_x),
            _f16_bits_to_f32(max_y),
            _f16_bits_to_f32(max_z),
        )
    if node_type == NODE_TYPE_BOX32:
        f = struct.unpack_from("<24f", dst, node_off + 16)
        mnx = _fmin_num(_fmin_num(_fmin_num(f[0], f[6]), f[12]), f[18])
        mny = _fmin_num(_fmin_num(_fmin_num(f[1], f[7]), f[13]), f[19])
        mnz = _fmin_num(_fmin_num(_fmin_num(f[2], f[8]), f[14]), f[20])
        mxx = _fmax_num(_fmax_num(_fmax_num(f[3], f[9]), f[15]), f[21])
        mxy = _fmax_num(_fmax_num(_fmax_num(f[4], f[10]), f[16]), f[22])
        mxz = _fmax_num(_fmax_num(_fmax_num(f[5], f[11]), f[17]), f[23])
        return (mnx, mny, mnz, mxx, mxy, mxz)
    return (_POS_INF, _POS_INF, _POS_INF, _NEG_INF, _NEG_INF, _NEG_INF)


def _write_parent_pointers(
    dst: bytearray,
    self_node_id: int,
    children: Sequence[int],
    offset_to_parent_data: int,
    offset_to_leaves: int,
    is_updatable_bvh: bool,
) -> None:
    for cid in children:
        is_box = (cid & 6) == 4
        is_tri0_upd = ((cid & 7) == 0) and is_updatable_bvh
        if is_box or is_tri0_upd:
            child_off = ((cid >> 3) << 6) & _U32_MASK
            slot_delta = ((child_off - offset_to_leaves) & _U32_MASK) >> 6
            parent_addr = (offset_to_parent_data + (slot_delta << 2)) & _U32_MASK
            if parent_addr + 4 <= len(dst):
                struct.pack_into("<I", dst, parent_addr, self_node_id)


def rehydrate_box32(
    src: bytes,
    dst: bytearray,
    offset_to_hierarchy: int,
    offset_to_parent_data: int,
    offset_to_leaves: int,
    is_updatable_bvh: bool,
    num_nodes: int,
) -> int:
    """CPU reference of Box32Rehydration compute shader (blob_01). Returns nodes written."""
    if num_nodes <= 0:
        return 0
    num_groups = (num_nodes + 63) // 64
    for g in range(num_groups):
        active = min(64, num_nodes - g * 64)
        group_base = offset_to_hierarchy + (g << 13)
        hwords = struct.unpack_from(f"<{active}I", src, group_base)
        counts = [w >> 24 for w in hwords]
        masks = [w & 0x00FFFFFF for w in hwords]
        popcounts = [bin(m).count("1") for m in masks]
        total_children = sum(counts)

        child_prefix = 0
        ov_prefix = 0
        parsed_group: List[Tuple[List[int], int, List[float]]] = []
        for tid in range(active):
            c_cnt = counts[tid]
            c_off = group_base + ((active + child_prefix) << 2)
            cids = list(struct.unpack_from(f"<{c_cnt}I", src, c_off)) if c_cnt > 0 else []
            child_prefix += c_cnt
            while len(cids) < 4:
                cids.append(INVALID_NODE)
            ov_cnt = popcounts[tid]
            ov_off = group_base + ((active + total_children + ov_prefix) << 2)
            ovs = list(struct.unpack_from(f"<{ov_cnt}f", src, ov_off)) if ov_cnt > 0 else []
            ov_prefix += ov_cnt
            parsed_group.append((cids, masks[tid], ovs))

        for tid in range(active):
            cids, mask, ovs = parsed_group[tid]
            c_cnt = counts[tid]
            aabbs = [0.0] * 24
            for slot in range(c_cnt):
                box = _child_aabb_from_dst(dst, cids[slot])
                base6 = slot * 6
                for k in range(6):
                    aabbs[base6 + k] = box[k]
            for slot in range(c_cnt, 4):
                base6 = slot * 6
                aabbs[base6 + 0] = _POS_INF
                aabbs[base6 + 1] = _POS_INF
                aabbs[base6 + 2] = _POS_INF
                aabbs[base6 + 3] = _NEG_INF
                aabbs[base6 + 4] = _NEG_INF
                aabbs[base6 + 5] = _NEG_INF
            rem = mask
            ov_i = 0
            while rem:
                lsb = rem & -rem
                bit_idx = lsb.bit_length() - 1
                aabbs[bit_idx] = ovs[ov_i]
                ov_i += 1
                rem ^= lsb
            node_off = group_base + (tid << 7)
            struct.pack_into("<4I24f4I", dst, node_off, *cids, *aabbs, 0, 0, 0, 0)
            self_id = ((node_off >> 6) << 3) | NODE_TYPE_BOX32
            _write_parent_pointers(
                dst, self_id, cids, offset_to_parent_data, offset_to_leaves, is_updatable_bvh
            )
    return num_nodes


def rehydrate_box16(
    src: bytes,
    dst: bytearray,
    offset_to_hierarchy: int,
    offset_to_parent_data: int,
    offset_to_leaves: int,
    is_updatable_bvh: bool,
    num_nodes: int,
) -> int:
    """CPU reference of Box16Rehydration compute shader (blob_02). Returns nodes written."""
    if num_nodes <= 0:
        return 0
    num_groups = (num_nodes + 63) // 64
    for g in range(num_groups):
        active = min(64, num_nodes - g * 64)
        group_base = offset_to_hierarchy + (g << 12)
        hwords = struct.unpack_from(f"<{active}I", src, group_base)
        counts = [w >> 24 for w in hwords]
        masks = [w & 0x00FFFFFF for w in hwords]
        popcounts = [bin(m).count("1") for m in masks]
        total_children = sum(counts)

        child_prefix = 0
        ov_prefix = 0
        parsed_group: List[Tuple[List[int], int, List[int]]] = []
        for tid in range(active):
            c_cnt = counts[tid]
            c_off = group_base + ((active + child_prefix) << 2)
            cids = list(struct.unpack_from(f"<{c_cnt}I", src, c_off)) if c_cnt > 0 else []
            child_prefix += c_cnt
            while len(cids) < 4:
                cids.append(INVALID_NODE)
            ov_cnt = popcounts[tid]
            ov_off = group_base + ((active + total_children) << 2) + (ov_prefix << 1)
            ovs = list(struct.unpack_from(f"<{ov_cnt}H", src, ov_off)) if ov_cnt > 0 else []
            ov_prefix += ov_cnt
            parsed_group.append((cids, masks[tid], ovs))

        for tid in range(active):
            cids, mask, ovs = parsed_group[tid]
            c_cnt = counts[tid]
            aabbs = [0] * 24
            for slot in range(c_cnt):
                box = _child_aabb_from_dst(dst, cids[slot])
                base6 = slot * 6
                aabbs[base6 + 0] = f32_to_f16_round_down(box[0])
                aabbs[base6 + 1] = f32_to_f16_round_down(box[1])
                aabbs[base6 + 2] = f32_to_f16_round_down(box[2])
                aabbs[base6 + 3] = f32_to_f16_round_up(box[3])
                aabbs[base6 + 4] = f32_to_f16_round_up(box[4])
                aabbs[base6 + 5] = f32_to_f16_round_up(box[5])
            for slot in range(c_cnt, 4):
                base6 = slot * 6
                aabbs[base6 + 0] = _HALF_POS_INF_BITS
                aabbs[base6 + 1] = _HALF_POS_INF_BITS
                aabbs[base6 + 2] = _HALF_POS_INF_BITS
                aabbs[base6 + 3] = _HALF_NEG_INF_BITS
                aabbs[base6 + 4] = _HALF_NEG_INF_BITS
                aabbs[base6 + 5] = _HALF_NEG_INF_BITS
            rem = mask
            ov_i = 0
            while rem:
                lsb = rem & -rem
                bit_idx = lsb.bit_length() - 1
                aabbs[bit_idx] = ovs[ov_i]
                ov_i += 1
                rem ^= lsb
            node_off = group_base + (tid << 6)
            struct.pack_into("<4I24H", dst, node_off, *cids, *aabbs)
            self_id = ((node_off >> 6) << 3) | NODE_TYPE_BOX16
            _write_parent_pointers(
                dst, self_id, cids, offset_to_parent_data, offset_to_leaves, is_updatable_bvh
            )
    return num_nodes


def rehydrate_bvh(
    src: bytes,
    geometries: Optional[Sequence[TriangleGeometryDesc]] = None,
    prebuilt_leaves: Optional[bytes] = None,
) -> bytearray:
    """Run the full Xbox BVH rehydration pipeline: DecompressHeader, LeafRehydration, bottom-up Box32/Box16."""
    hdr = parse_bvh_header(src)
    if not hdr.is_compressed:
        raise ValueError("Input BVH header does not have the compressed flag (0x01000000) set")
    total = hdr.total_bytes
    if total > len(src):
        raise ValueError(f"Truncated BVH buffer: header declares 0x{total:x} bytes, got 0x{len(src):x}")

    dst = bytearray(total)
    decompress_header(src, dst, in_place=False)

    if geometries is not None:
        rehydrate_leaves(src, dst, geometries)
    elif prebuilt_leaves is not None:
        expected = hdr.num_leaves * 64
        if len(prebuilt_leaves) != expected:
            raise ValueError(f"Expected {expected} bytes of prebuilt leaves, got {len(prebuilt_leaves)}")
        num_chunks = (hdr.num_leaves + 2047) // 2048
        src_pos = 0
        for g in range(num_chunks):
            count = min(2048, hdr.num_leaves - g * 2048)
            byte_len = count * 64
            chunk_base = hdr.offset_to_leaves + (g << 17)
            dst[chunk_base : chunk_base + byte_len] = prebuilt_leaves[src_pos : src_pos + byte_len]
            src_pos += byte_len

    if hdr.root_node_type in (NODE_TYPE_BOX16, NODE_TYPE_BOX32):
        levels = parse_level_table(src, hdr)
        for lv in levels:
            if lv.num_box32 > 0:
                rehydrate_box32(
                    src,
                    dst,
                    hdr.offset_to_hierarchy + lv.box32_start,
                    hdr.offset_to_parent_data,
                    hdr.offset_to_leaves,
                    hdr.is_updatable,
                    lv.num_box32,
                )
            if lv.num_box16 > 0:
                rehydrate_box16(
                    src,
                    dst,
                    hdr.offset_to_hierarchy + lv.box16_start,
                    hdr.offset_to_parent_data,
                    hdr.offset_to_leaves,
                    hdr.is_updatable,
                    lv.num_box16,
                )
    return dst


def _box_encloses(
    outer: Tuple[float, float, float, float, float, float],
    inner: Tuple[float, float, float, float, float, float],
    rtol: float = 0.0,
) -> bool:
    for ax in range(3):
        # Infinite outer bounds (the builder's box16 +-inf) are conservative; keep the tolerance finite so inf * 0 is not NaN.
        scale = max([abs(v) for v in (outer[ax], outer[ax + 3], inner[ax], inner[ax + 3]) if math.isfinite(v)] + [1.0])
        eps = scale * rtol
        if outer[ax] > inner[ax] + eps or outer[ax + 3] < inner[ax + 3] - eps:
            return False
    return True


def validate_rehydrated_bvh(
    buf: bytes,
    check_aabb_enclosure: bool = True,
    check_parent_pointers: bool = True,
    enclosure_rtol: float = 0.0,
) -> ValidationReport:
    """Validate structural integrity, node types/offsets, parent links, and AABB finiteness/enclosure."""
    errors: List[str] = []
    hdr = parse_bvh_header(buf)
    if hdr.is_compressed:
        errors.append("Compressed flag (0x01000000) is still set in rehydrated header")
    total = hdr.total_bytes
    if total > len(buf):
        errors.append(f"Header total_bytes 0x{total:x} exceeds buffer len 0x{len(buf):x}")
        return ValidationReport(False, tuple(errors), hdr.root_node_id, 0, 0, 0, 0, None)

    if check_parent_pointers:
        root_slot = ((hdr.root_node_offset - hdr.offset_to_leaves) & _U32_MASK) >> 6
        root_par_addr = (hdr.offset_to_parent_data + (root_slot << 2)) & _U32_MASK
        if root_par_addr + 4 <= len(buf):
            root_par = struct.unpack_from("<I", buf, root_par_addr)[0]
            if root_par != INVALID_NODE:
                errors.append(f"Root parent pointer is 0x{root_par:08x}, expected 0xFFFFFFFF")

    visited: Dict[int, int] = {}
    box32_cnt = 0
    box16_cnt = 0
    leaf_cnt = 0
    max_depth = 0
    root_aabb: Optional[Tuple[float, float, float, float, float, float]] = None
    post_order: List[int] = []
    node_slots: Dict[int, Tuple[Tuple[int, ...], List[Tuple[float, float, float, float, float, float]]]] = {}

    stack: List[Tuple[int, int, Optional[int]]] = [(hdr.root_node_id, 0, None)]
    while stack:
        nid, depth, expected_parent = stack.pop()
        if nid == INVALID_NODE:
            continue
        ntype = nid & 7
        noff = (nid >> 3) << 6
        if ntype not in (NODE_TYPE_TRI0, NODE_TYPE_TRI1, NODE_TYPE_BOX16, NODE_TYPE_BOX32):
            errors.append(f"Invalid node type {ntype} for node_id 0x{nid:08x}")
            continue
        nsize = 128 if ntype == NODE_TYPE_BOX32 else 64
        if noff < hdr.offset_to_leaves or noff + nsize > total:
            errors.append(f"Node 0x{nid:08x} (off=0x{noff:x}, sz={nsize}) out of bounds [0x{hdr.offset_to_leaves:x}, 0x{total:x})")
            continue
        if nid in visited:
            errors.append(f"Cycle or shared node detected at node_id 0x{nid:08x}")
            continue
        visited[nid] = depth
        post_order.append(nid)
        if depth > max_depth:
            max_depth = depth

        if check_parent_pointers and expected_parent is not None:
            if (nid & 6) == 4 or ((nid & 7) == 0 and hdr.is_updatable):
                slot_delta = ((noff - hdr.offset_to_leaves) & _U32_MASK) >> 6
                par_addr = (hdr.offset_to_parent_data + (slot_delta << 2)) & _U32_MASK
                if par_addr + 4 <= len(buf):
                    got_par = struct.unpack_from("<I", buf, par_addr)[0]
                    if got_par != expected_parent:
                        errors.append(
                            f"Parent mismatch for 0x{nid:08x}: got 0x{got_par:08x}, expected 0x{expected_parent:08x}"
                        )

        if ntype in (NODE_TYPE_TRI0, NODE_TYPE_TRI1):
            leaf_cnt += 1
            continue

        cids = struct.unpack_from("<4I", buf, noff)
        if ntype == NODE_TYPE_BOX32:
            box32_cnt += 1
            raw_aabbs = struct.unpack_from("<24f", buf, noff + 16)
            boxes = [raw_aabbs[s * 6 : s * 6 + 6] for s in range(4)]
        else:
            box16_cnt += 1
            raw_halves = struct.unpack_from("<24H", buf, noff + 16)
            boxes = [
                tuple(_f16_bits_to_f32(raw_halves[s * 6 + k]) for k in range(6))
                for s in range(4)
            ]

        if nid == hdr.root_node_id:
            root_aabb = _child_aabb_from_dst(buf, nid)

        node_slots[nid] = (cids, boxes)
        for slot in range(4):
            cid = cids[slot]
            box = boxes[slot]
            if cid == INVALID_NODE:
                continue
            # +-inf is a valid conservative bound (box16 overflow); only NaN is malformed.
            if any(math.isnan(x) for x in box):
                errors.append(f"NaN AABB in box 0x{nid:08x} slot {slot}: {box}")
            elif box[0] > box[3] or box[1] > box[4] or box[2] > box[5]:
                errors.append(f"Inverted AABB in box 0x{nid:08x} slot {slot}: {box}")
            stack.append((cid, depth + 1, nid))

    if check_aabb_enclosure and not errors:
        subtree_leaf_bounds: Dict[int, Tuple[float, float, float, float, float, float]] = {}
        for nid in reversed(post_order):
            ntype = nid & 7
            if ntype in (NODE_TYPE_TRI0, NODE_TYPE_TRI1):
                subtree_leaf_bounds[nid] = _child_aabb_from_dst(buf, nid)
                continue
            cids, boxes = node_slots[nid]
            acc = [_POS_INF, _POS_INF, _POS_INF, _NEG_INF, _NEG_INF, _NEG_INF]
            for slot in range(4):
                cid = cids[slot]
                if cid == INVALID_NODE or cid not in subtree_leaf_bounds:
                    continue
                sub = subtree_leaf_bounds[cid]
                if all(math.isfinite(x) for x in sub):
                    if not _box_encloses(boxes[slot], sub, rtol=enclosure_rtol):
                        errors.append(
                            f"Box 0x{nid:08x} slot {slot} AABB {boxes[slot]} does not enclose subtree leaves of 0x{cid:08x} {sub}"
                        )
                    for ax in range(3):
                        acc[ax] = _fmin_num(acc[ax], sub[ax])
                        acc[ax + 3] = _fmax_num(acc[ax + 3], sub[ax + 3])
            subtree_leaf_bounds[nid] = (acc[0], acc[1], acc[2], acc[3], acc[4], acc[5])

    return ValidationReport(
        valid=len(errors) == 0,
        errors=tuple(errors),
        root_node_id=hdr.root_node_id,
        visited_box32=box32_cnt,
        visited_box16=box16_cnt,
        visited_leaves=leaf_cnt,
        max_depth=max_depth,
        root_aabb=root_aabb,
    )


@dataclass(frozen=True)
class TriangleHit:
    t: float
    node_id: int
    geometry_index: int
    primitive_index: int


def _ray_box_entry(origin, direction, bounds, t_min, t_max):
    for axis, (o, d) in enumerate(zip(origin, direction)):
        low, high = bounds[axis], bounds[axis + 3]
        if d == 0:
            if not low <= o <= high:
                return None
            continue
        a, b = (low - o) / d, (high - o) / d
        t_min = max(t_min, min(a, b))
        t_max = min(t_max, max(a, b))
        if t_min > t_max:
            return None
    return t_min


def _cross(a, b):
    return (a[1] * b[2] - a[2] * b[1],
            a[2] * b[0] - a[0] * b[2],
            a[0] * b[1] - a[1] * b[0])


def _dot(a, b):
    return sum(x * y for x, y in zip(a, b))


def _ray_triangle_t(origin, direction, vertices):
    a, b, c = (vertices[i:i + 3] for i in (0, 3, 6))
    ab = tuple(y - x for x, y in zip(a, b))
    ac = tuple(y - x for x, y in zip(a, c))
    p = _cross(direction, ac)
    det = _dot(ab, p)
    if det == 0:
        return None
    oa = tuple(o - x for o, x in zip(origin, a))
    u = _dot(oa, p) / det
    if not 0 <= u <= 1:
        return None
    q = _cross(oa, ab)
    v = _dot(direction, q) / det
    if not 0 <= v <= 1 - u:
        return None
    return _dot(ac, q) / det


def trace_blas(
    buf: bytes,
    origin: Tuple[float, float, float],
    direction: Tuple[float, float, float],
    t_min: float = 0.0,
    t_max: float = math.inf,
) -> Optional[TriangleHit]:
    """Trace a two-sided ray through a rehydrated triangle BLAS in stored coordinates.

    The ray is origin + t * direction over the inclusive interval [t_min, t_max].
    This is a geometric reference for structurally valid buffers. Equal hits
    select the lower node ID.
    """
    if (len(origin) != 3 or len(direction) != 3 or not all(map(math.isfinite, (*origin, *direction)))
            or not any(direction) or not math.isfinite(t_min) or not 0 <= t_min <= t_max):
        raise ValueError("Invalid ray or ray interval")
    header = parse_bvh_header(buf)
    if header.is_compressed or not header.is_blas or header.total_bytes > len(buf):
        raise ValueError("Expected a complete rehydrated BLAS")

    hit = None
    visited = set()
    stack = [(header.root_node_id, t_min)]
    while stack:
        node_id, entry = stack.pop()
        if node_id == INVALID_NODE or entry > t_max:
            continue
        node_type = node_id & 7
        offset = (node_id >> 3) << 6
        size = 128 if node_type == NODE_TYPE_BOX32 else 64
        if offset < header.offset_to_leaves or offset + size > header.total_bytes:
            raise ValueError(f"BVH node 0x{node_id:x} is out of bounds")
        if node_id in visited:
            raise ValueError(f"Repeated BVH node 0x{node_id:x}")
        visited.add(node_id)

        if node_type in (NODE_TYPE_BOX16, NODE_TYPE_BOX32):
            children = struct.unpack_from("<4I", buf, offset)
            fmt = "<6e" if node_type == NODE_TYPE_BOX16 else "<6f"
            stride = struct.calcsize(fmt)
            for slot, child in enumerate(children):
                if child == INVALID_NODE:
                    continue
                bounds = struct.unpack_from(fmt, buf, offset + 16 + slot * stride)
                child_entry = _ray_box_entry(origin, direction, bounds, t_min, t_max)
                if child_entry is not None:
                    stack.append((child, child_entry))
        elif node_type in (NODE_TYPE_TRI0, NODE_TYPE_TRI1):
            triangle = node_type == NODE_TYPE_TRI1
            geometry_flags, primitive0, primitive1 = struct.unpack_from("<3I", buf, offset + 48)
            primitive = primitive1 if triangle else primitive0
            if primitive == INVALID_NODE:
                raise ValueError(f"BVH node 0x{node_id:x} has no triangle")
            vertices = struct.unpack_from("<9f", buf, offset + 12 * triangle)
            t = _ray_triangle_t(origin, direction, vertices)
            if t is not None and t_min <= t <= t_max and (
                hit is None or (t, node_id) < (hit.t, hit.node_id)
            ):
                hit = TriangleHit(t, node_id, geometry_flags >> 8, primitive)
                t_max = t
        else:
            raise ValueError(f"Unsupported BVH node type {node_type}")
    return hit
