# BVH container (core_bvh4.rpf, 8.3 GB) - structure survey

From the author's own console dump. The dump itself is not in this repository. Everything below is from reading headers and shader code; none of it was checked on hardware.
- Outer file: "9FPR" header, encrypted root TOC (not decoded). Contains 1,260 nested RPF9 archives (4 KiB aligned, found by magic) - they match the *_bvh.rpf names in out_bvh4/*.xml (terrain etc).
- Nested archive: 9FPR header + small encrypted TOC, then at a 4 KiB boundary: 16 zero bytes + zlib streams (78 da), 16-byte aligned, then a non-zlib trailer.
- 1,244 archives parsed (16 not: no stream start found). 473,612 streams, 7.0 GB compressed -> 118 GB raw (mostly zero padding, ~93% zero in the samples).
- Streams come in pairs: 208-byte "system" header (virtual pointers 0x50000000 base, 3x4 identity transform at dwords 20-34, data size at dword 42) + one large data segment (64 KiB multiples, sparse).
- Typical archive: 128 streams = 64 header/data pairs (357 archives). Others: 124/126/122..., or 2 (56 archives).
- NOT decoded: data segment layout (sparse, offset tables near the start, header words at 0x20 point near the end of the used area). A plain RDNA2 128-byte box-node scan finds nothing, so it is not raw ray-accelerator nodes, or is stored in another layout.
- survey.py/survey2.py/load.py here; survey2.json = per-archive stats.

## Cross-reference with XboxBvhBuilder shaders (out_bvhbuilder, `dxc -dumpbin blobNN.dxbc` gives readable DXIL)
- The builder has DecompressHeader, Box32Rehydration, Box16Rehydration, LeafRehydration, CopyAndCompactBVH, GetBVHCompactedSize: so the stored BVH is a COMPRESSED form that is rehydrated into the hardware layout at load time. This fits the bit-packed, byte-planar data in the segments, and explains why a plain 128-byte box-node scan found nothing.
- DecompressHeader: copies sizeToPreserveDiv16*16 header bytes, clears bit 24 (0x01000000) of header dword 0 (compressed flag), and writes 0xFFFFFFFF at dword[14] + ((((dword[12]>>3)<<6) - dword[13]) >> 6 << 2). Compact size = ((dword*8)+311)&~255; nodes are 64-byte units.
- The 208-byte per-BVH header: dwords 16-17 = unique 64-bit id, dwords 20-30 = 3x3 matrix (rows padded to 16 B, identity in most), dword 40 = used size (256 B aligned, <= data length), dword 41 = flags/counts. NOT yet matched to the BVH header the shaders read (the 0x30/0x34/0x38 fields), because the data segment's first 0x1c00 bytes are zero and the flagged header was not located.
- Data segment end of used area holds a 15-entry ascending byte-offset table followed by 9 (probably per-level/per-type start offsets).

## Box16/Box32 rehydration read (dxil/blob01.ll, blob02.ll via dxc -dumpbin) - from the shader code, NOT yet matched to the data
- cbuffer BoxRehydrationParams {offsetToHierarchy, offsetToParentData, offsetToLeaves, IsUpdatableBVH}. Resources: compressedBVH (src), decompressedBVH (dst). One 4096-byte group of the hierarchy stream per threadgroup; each thread reads one dword: low 24 bits = a mask (popcount used for a prefix sum), high 8 bits = a count (wave prefix/sum over it) - i.e. a packed topology stream.
- Node ids use the RDNA2 ray-accelerator encoding: low 3 bits = node type, (id>>3)<<6 = byte offset in 64-byte units. Type 0/1 = triangle node (3-4 float vertices at +0..+47, flags dword at +48, bit 0x80 = second triangle), type 4 = 16-bit-float box node (half AABBs), box nodes' AABBs are rebuilt by min/max over children (shared-memory gs_boxNodeAABBs, 1536 halves).
- So the stored form keeps leaves + a compact hierarchy and the box nodes are re-created ("rehydrated"). The 15-entry table at the end of the used area is probably cumulative 64-byte-node indices per tree level (differences 86,42,38,19,10,11,4,6,2,3,0,2,0,1, roughly halving), starting at 1477.
- Checked against data: leaf vertex data is not plain float triangles (only 2 candidate 64 B blocks in the sample; values like x.004/x.508 suggest quantised/fixed-point positions). LeafRehydration (blob03, ~4000 lines of DXIL) is the shader that defines the leaf encoding and has to be read next.

## LeafRehydration (blob03): leaves are NOT stored as vertices
- cbuffer LeafParams {translation, offsetToLeaves, scale, ScaleOrTranslateEnabled, rotationMatrixRow0..2, numElements}; SRV geometryInputs = D3D12_RAYTRACING_GEOMETRY_TRIANGLES_DESC[] (Transform3x4, IndexFormat, VertexFormat, IndexCount, VertexCount, IndexBuffer, VertexBuffer{StartAddress, StrideInBytes}).
- The shader reads the game's own vertex/index buffers (rawBufferLoad.i64 x48, legacyF16ToF32 x22, 129 integer bitfield ops) and writes the hardware triangle nodes. So the compressed BVH holds topology + references into geometry (indices/primitive ids, hence the small repeated integers), not vertex positions.
- Consequence: core_bvh4.rpf alone cannot yield triangles; the matching vertex/index buffers (other archives, e.g. the terrain meshes) are needed, plus the per-BVH transform (208-byte header dwords 20-30 look like the rotation rows).

## Decoded container layout and CPU reference model (`tools/xbox/bvh_rehydrate_cpu.py`)
From `XboxBvhBuilder.dll` (`RehydrateRaytracingAccelerationStructureX` / `RehydrateRaytracingAccelerationStructureBatched2X` and DXIL blobs `DecompressHeader`, `LeafRehydration`, `Box16Rehydration`, `Box32Rehydration`):
- **Data segment prefix**: dword at `+0x20` = end byte offset (`0x100 + bvh_size`), dword at `+0x28` = `bvh_size` (`(header.dword[1] + 1) << 6`). In all inspected archives the 64-byte flagged Xbox BVH header starts at `+0x100` (`prefix[+0x20] - prefix[+0x28]`).
- **64-byte Xbox BVH header** (at `+0x100`):
  - `+0x00`: flags (`0x81020006`: bit 31 = BLAS, bit 24 = `0x01000000` compressed/dehydrated, bits 23:20 = extra preserved-header tier `k`, bit 18 = `IsUpdatableBVH`).
  - `+0x04`: `num_64B_blocks - 1` (`total_bytes = (dw[1] + 1) << 6`).
  - `+0x08`: build ID / hash; `+0x0c`: `numElements` (leaf count).
  - `+0x10..+0x27`: 6 floats = root AABB (`min_xyz`, `max_xyz`) in `Transform3x4`-rotated space.
  - `+0x28`: `offsetToHierarchy`; `+0x2c`: `header_preserve_flag`; `+0x30`: `root_node_id` (RDNA2 encoding `(off >> 6) << 3 | type`); `+0x34`: `offsetToLeaves`; `+0x38`: `offsetToParentData` (unsigned 32-bit wrapping offset so non-updatable BLAS parent slots for box nodes land between `+0x40` and `offsetToLeaves`).
  - `+0x3c`: packed leaf quantization/OBB transform word (`0x007f7f7f` = disabled; bit 31 = power-of-two per-axis scale exponents in bytes 0..2; bit 30 = AABB-midpoint translation for axes not spanning zero; bits 29:24 = index `0..63` into the `Rz * Ry * Rx` `pi/8`-step rotation table).
- **Trailing level table**: last dword at `total_bytes - 4` is `num_levels`; immediately preceding it are `num_levels` pairs `(box32_end, box16_end)` (cumulative byte offsets relative to `offsetToHierarchy`). For each bottom-up level `i`: `num_box32 = (box32_end - prev_end) >> 7` (128 B nodes) and `num_box16 = (box16_end - box32_end) >> 6` (64 B nodes).
- **Compressed hierarchy stream**: each level's Box32/Box16 slice is split into 64-node wave groups (`group_base = slice_base + (g << 13)` for Box32, `g << 12` for Box16, last group has `active = count % 64` active lanes). Within each group: `active` header dwords (`count = w >> 24`, 24-bit override mask `w & 0xffffff`), followed by `sum(count)` child node IDs, followed by `sum(popcount(mask))` float32 (Box32) or float16 (Box16) AABB overrides applied in LSB-to-MSB bit order (`ctz`).
- **Compressed leaf planes**: split into 2048-leaf chunks at `offsetToLeaves + (chunk << 17)`. Each chunk of `N` leaves stores 5 contiguous byte-planes totaling `11 * N` bytes: `geom_idx` (`3*N`), `flags` (`1*N`), `prim0` (`3*N`), `prim1` (`3*N`, `0xffffff -> 0xffffffff`), `vertex_order` (`1*N`).
- **208-byte per-BVH header**: dwords 20-30 (`+0x50..+0x7b`, pointed to by `+0x38 = 0x50000050`) are the `Transform3x4` rotation rows (zero translation) passed to `LeafRehydration`; `+0xa0` = 256-byte-aligned used data size; `+0xa4` = packed flags and geometry count.
- **RAGE9 geometry formats in `LeafRehydration`**: `XboxBvhBuilder.dll` extends `LeafRehydration` with two RAGE9-specific formats alongside standard DXGI formats: `IndexFormat == 53` decodes RAGE9 meshlet index buffers (`[num_meshlets, tot_tris, tot_vrefs]`, 16 B meshlet headers, 32-bit packed triangle descriptors, 16-bit vertex remap table), and `VertexFormat == 16` decodes RAGE9 8-byte quantized vertex streams using the 32-byte `(origin, slot_axis, scale)` header at `vb_start - 32`.
- **CPU validation**: synthetic tests live in `tools/xbox/test_bvh_rehydrate_cpu.py` (no game data). Checked locally in hierarchy-only mode (no vertex/index buffers) on every dehydrated BLAS carved from `core_bvh4.rpf` (269,096) and `shared_minigame000_bvh4.rpf` (1,233): all pass node type/offset, parent-pointer, exact leaf-count and level-table count checks. Without geometry the validator also reports `Inverted AABB in box` on about 39% of them; every inverted axis checked has a 0.0 endpoint, consistent with bounds that are only complete once leaves are rehydrated. Leaf rehydration against real geometry and per-node AABBs against console output are not verified.

## Rehydration on a PC (measured)

- **The library's CPU entry works with aliased memory.** `RehydrateRaytracingAccelerationStructureX` reads `SourceAccelerationStructureData`,
  the geometry addresses, `DestAccelerationStructureData` and the scratch address as CPU pointers (unified memory on the console). Mapping
  memory at each buffer's GPU virtual address with `VirtualAlloc` and copying the data there makes the call return `S_OK`: on a synthetic
  8-leaf BLAS the output is identical to `bvh_rehydrate_cpu.py`, and on a real 103-leaf sample from `core_bvh4.rpf` (fed synthetic
  vertices and indices) it differs only in the placeholder values a leaf gets when its triangle is unusable.
- **Empty-bound markers.** The library and the shaders use `FLT_MAX` (`0x7f7fffff`) for an empty bound and saturate half bounds to
  `+/-65504` (`0x7bff`/`0xfbff`). The CPU model now uses the same values; its unit tests pass.
- **`instance208` descriptors.** In the first nested pack of `core_bvh4.rpf` (10 BVHs, an interior cell) every BVH is preceded
  by a 208-byte descriptor `0x50` bytes earlier in the file. The record has a 64-bit key at `+0x40`, a 3x4 transform at `+0x50`, `0x101`
  at `+0x88`, a 64-byte-aligned offset at `+0xA0` and two `u16` counts at `+0xA4`. The key was not found, as a raw 64-bit value, in the
  model resources of `core_restofworld.rpf` (926) or `shared_minigame000.rpf` (926), nor inside the inflated streams of `packs.rpf`
  (143,313 streams) or `core_restofworld.rpf` (2,360 streams); it is also not a simple name hash (joaat, FNV-1a, Murmur64A, xxHash64,
  CRC64). The BLAS-to-geometry link therefore needs the drawable records in `levels.rpf` or a geometry-based pairing (scene bounds
  inverse-rotated by the descriptor transform plus leaf count), which matched model AABBs and triangle counts in `shared_minigame000`.
