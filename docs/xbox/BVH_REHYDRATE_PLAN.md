# Plan: BVH rehydration and mesh pairing

Status: in progress (2026-11-22). Branch `feat/bvh-rehydrate-tools`, based on `upstream/xbox-series`.

## Goal

Run the package's own rehydration path on a PC, check it against the CPU model, and pair a dehydrated BLAS with the mesh it was
built from. Everything runs from the local package; no game data, asset names or extracted buffers enter the repository.

## What is already in place

- `RehydrateRaytracingAccelerationStructureX` reads the source, vertices, destination and scratch as CPU pointers (console unified
  memory). Mapping memory at each buffer's GPU virtual address with `VirtualAlloc` and copying the data there makes the call work:
  `tools/xbox/platform/rehydrate_cpu.cpp`.
- The CPU model uses the library's markers (empty bounds `FLT_MAX`, half bounds saturated at +/-65504); its unit tests pass.
- `find_bvh_sample.py` extracts compressed BLAS from `core_bvh4.rpf`; `make_bvh_fixture.py` writes a synthetic one.
- `compare_bvh_geometry.py` runs model and library on the same BLAS and geometry and reports byte differences, leaf bounds and
  validation error kinds.
- `obj_to_geometry.py` converts a Wavefront mesh to the raw vertex/index buffers.
- `match_bvh_models.py` pairs a BVH with meshes by rotating the scene bounds with the `instance208` transform and comparing AABBs and
  triangle counts against `leaves * 2`.
- Measured on the first nested pack (10 BVHs, interior cell): the geometry is in a 3 GB window of `levels.rpf`; 13 meshes pair with the
  10 BVHs, 7 with an exact AABB. The descriptor's 64-bit key is not stored in the model archives searched and is not a simple name hash.
- Verified case: one single-geometry BLAS (103 leaves) rehydrated with its real mesh returns `S_OK`, every leaf bound is finite and
  ordered, model and library differ in 44 of 11,328 bytes; 21 enclosure warnings come from half-float box16 saturation (transformed
  extent +/-72,536 against +/-65,504).

## Work in progress

**Multiple geometries per BLAS.** Several BVHs in the cell reference more than one geometry index, and both current tools accept one
geometry. `rehydrate_cpu` and `compare_bvh_geometry.py` now take a list of geometries (vertex buffer, optional index buffer, stride,
count, formats), so a BLAS can be rehydrated with the geometry descriptors the game would pass.

Result: **multi-geometry works; the earlier crash was our own undersized array.** `bvh_08` has leaves up to `geom_index 5` and was
first called with only two descriptors, so the library indexed past the array and faulted at `XboxBvhBuilder+0x1606e`. With six
descriptors it returns `S_OK`; a one-geometry BLAS called with two descriptors returns the same bytes as with one. The plumbing check on
`bvh_08` with the same mesh repeated for all six indices: model and library differ in 36 of 199,744 bytes, every leaf bound is finite and
ordered, and the model reports two inverted AABBs. What is still open is the **mesh per geometry index**: the triangle-count signal
(leaves of that index against the mesh's triangles) is noisy (ratios 0.5-1.9), so it cannot assign the 13 meshes to the up-to-29 indices
of a BLAS; the game's geometry records would be needed.

Steps:

1. New `--geom <vb> <ib> <stride> <count> <vertexFormat> <indexFormat>` option, repeatable, after `<src> <out>`; the old positional
   form stays as a single-geometry shortcut. [done]
2. The comparison harness accepts the same repeated option, builds the geometry list and runs the model and the library. [done]
3. Plumbing check: a multi-geometry BLAS rehydrated with a repeated mesh returns `S_OK` and matches the model (36/199,744 bytes on
   `bvh_08`). [done]
4. Mesh assignment per geometry index: pair each index with the best mesh by the per-index leaf bounds (the leaf records name the
   geometry index), then re-run the comparison. **Not solvable from the BLAS alone.** `assign_bvh_geometry.py` was extended to run
   the model once per candidate mesh with that mesh on every index and to invert the leaf transform before comparing bounds; the
   signal is degenerate, because the leaves of an index are produced from the mesh placed on it, so every candidate matches its own
   bounds. Triangle counts only narrow the candidates (1-2 triangles per leaf). The per-index mapping needs the game's geometry
   descriptor array; the BLAS records only the index. Widening the candidate set from 13 to 1,228 meshes changed nothing, which
   confirms the limit.
5. Re-verify every BVH of the cell; record counts only (matches, byte differences, enclosure warnings). [one-geometry BLAS verified;
   multi-geometry BLAS verified end to end with the real drawable geometry: 0/199,744 byte difference]

## Drawable decoder (started)

Milestone 1, block layer: `tools/xbox/rage9_vertex_blocks.py` finds the streams that start with the `42 40 00 00` vertex-block
header, reads the tagged slot table (low byte tag, offset x2 from the block start, slot 0 at +0x80) and prints a count per tag from
the adjacent offsets. In a 3 GB window of `levels.rpf` it finds 1,359 blocks and 1,367 tag-`0x42` spans (8 bytes per vertex).
The span-derived counts do not equal the exporter's vertex counts for the same models (twelve known counts tried), so they are not
the authoritative counts.

Milestone 2, done: `tools/xbox/rage9_walk_drawable.py` finds a system page by the drawable name, locates the drawable through the
pointer to the name at `+168`, and walks LOD (`+80`) -> model list -> geometry list (model `+8`, count at `+16`). Per geometry it
reads the vertex count from the vertex-format descriptor (`vb+64`, flags at `+0x138`) and the triangle/cluster counts from the index
descriptor (`geometry+64`) without touching the graphics segment. On drawable B it finds 10 geometries whose
triangle counts match the BLAS's per-index leaf counts exactly where the BLAS uses them (5/3, 131/66, 1020/518, 264/139, 1168/617,
654/357 for indices 0-5). **The geometry index in the leaf records is the drawable's geometry order**, which was the missing mapping.

Milestone 3, done: the graphics segment is a concatenation of `42 40 00 00` pages in stored order. `tools/xbox/rage9_dump_cell.py`
finds the shortest page run that validates the vertex-buffer header of every geometry (for this drawable: two pages from `0x39e84d00`,
113,600 bytes, 10 of 10 headers) and writes each geometry's whole vertex block and packed index buffer. Each vertex block keeps the
per-geometry **record stride** (20, 24 or 36 bytes here, from `(vb_len - pos_offset) / vertex_count`), with the 8-byte quantized
position at the vertex-format offset 128 and the 32-byte origin/axis/scale header at 96. Reading only the 8-byte position attribute was
the earlier mistake that produced garbage coordinates.

Milestone 4, done: `tools/xbox/rage9_export_geometry.py` converts a dumped geometry to what the rehydration library understands, because
the library only decodes standard DXGI formats (it made no sense of vertex format 16 or index format 53 and ran off the end of the
vertex buffer). Positions are decoded to float3 with the quantized-8 formula, and the meshlet index buffer is expanded primitive by
primitive (0..triangles-1) into a uint32 triangle list, so the primitive order matches the BVH leaves. `rehydrate_cpu` gained an
optional seventh `--geom` value, the vertex byte offset.

End-to-end result on `bvh_08` (6 geometries of drawable B, 1,700 leaves): the library rehydrates with `S_OK` and the
CPU model output is **byte-identical** (0 of 199,744 bytes differ). The model's box16 sentinel was the last difference: the builder
writes +-inf (`0x7C00` / `0xFC00`) when a box exceeds half range, not the 65,504 saturation the model used, so the model now follows the
library. Two boxes of the builder output are inverted in x; they come out of the library itself (same bytes) and are kept as measured.
Leaf coverage per geometry index matches the drawable's triangle counts exactly (5/3, 131/66, 1020/518, 264/139, 1168/617, 654/357),
which closes the index-to-mesh mapping.

Milestone 5, done: `tools/xbox/rage9_match_drawables.py` matches a BLAS to its drawable without a name. The leaf geometry index plus the
leaf primitive slots give the exact triangle count per index, and the drawable's geometry list has the same counts in order, so the
signatures identify the drawable. In the 3 GB window it enumerates 1,158 drawables and matches, with a unique hit, `bvh_00` ->
drawable A (68/234/48) and `bvh_08` -> drawable B; `bvh_02` ->
drawable C is a prefix match (the BLAS uses 3 of its 7 geometries). Rehydrating those two new cases gives `S_OK`
and byte-identical model output again (`bvh_00`: 0/18,880; `bvh_02`: 0/33,280; the latter has three inverted boxes, same as the library).
`bvh_01` and `bvh_03` have no single-drawable match, neither by name nor by structure (1,162 structural candidates), so their BLAS is
either an aggregate of several drawables or its drawable is outside the carved window.

## Open questions

- ~~The geometry index array lives in the drawable, not in the BLAS.~~ Resolved: the drawable's primary-LOD geometry list gives the
  order, and the leaf geometry index is that list's index (see milestones 2-4). Public measurements of the same title's model resources
  were the fact source: a geometry record points to the vertex buffer at `+8` and to the meshlet descriptor, which selects the index
  buffer, at `+64`; the buffers' own descriptors carry the vertex/reference/triangle counts. The parser is our own; the reference
  implementation is GPL-3 and no code was copied.
- Whether the two inverted boxes the builder emits for this cell matter on the console, or whether they sit on degenerate triangles
  where an empty box is harmless. The model and the library agree byte for byte, so it is not a decoder difference.
- The drawable decoder finds a drawable by name inside the inflated streams; for a resource whose name is not known, the dictionary
  entry (name + hash) still has to be located, and the graphics page run is searched in stored order, which worked here but needs a
  check on a drawable whose pages are not adjacent.
- Whether upstream wants this tool set at all: their merged work (#34, #112, #119) covers the CPU model and the address lowering, not
  the library-driven rehydrate or the pairing. A claim in the coordination issue and a pull request would be the next step after the
  tests above.

## Guardrails

- No archives, carved streams, meshes, asset names or reports that quote them in the repository; every measurement stays local.
- No decryption or TOC work.
- Facts and counts in the docs, not inputs.
