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
   multi-geometry BLAS verified for plumbing with a repeated mesh]

## Drawable decoder (started)

Milestone 1, block layer: `tools/xbox/rage9_vertex_blocks.py` finds the streams that start with the `42 40 00 00` vertex-block
header, reads the tagged slot table (low byte tag, offset x2 from the block start, slot 0 at +0x80) and prints a count per tag from
the adjacent offsets. In a 3 GB window of `levels.rpf` it finds 1,359 blocks and 1,367 tag-`0x42` spans (8 bytes per vertex).
The span-derived counts do not equal the exporter's vertex counts for the same models (twelve known counts tried), so they are not
the authoritative counts.

Milestone 2, next: read the counts from the descriptors instead of the spans. Per the public layout, a geometry record points at the
vertex buffer at `+8` and at the meshlet descriptor at `+64`; each buffer's `+24` pointer addresses the graphics segment, the vertex
layout carries the count and the meshlet descriptor carries meshlet/triangle/reference counts. Then walk a drawable's primary-LOD
geometry list to get the index-to-mesh order. The data is split into system and graphics segments (separate streams), so the walk
needs both.

## Open questions

- **The geometry index array lives in the drawable, not in the BLAS.** Public measurements of the same title's model resources
  describe a drawable with a primary-LOD geometry list (one entry per geometry index, tens to hundreds of entries): a geometry record
  points to the vertex buffer at `+8` and to the meshlet descriptor, which selects the index buffer, at `+64`; the buffers' own
  descriptors carry the vertex/reference/triangle counts. Recovering the index-to-mesh mapping therefore means decoding the drawable
  resource of the cell, which needs that resource's page/pointer layout in `levels.rpf` (the archive is paged and relocated). The
  documented facts are reusable; the reference implementation is GPL-3, so our parser has to be written from the facts.
- Whether the 21 box16 enclosure warnings are expected on the console as well (half saturation), or whether the leaf transform for
  this cell should be decoded differently.
- Whether upstream wants this tool set at all: their merged work (#34, #112, #119) covers the CPU model and the address lowering, not
  the library-driven rehydrate or the pairing. A claim in the coordination issue and a pull request would be the next step after the
  tests above.

## Guardrails

- No archives, carved streams, meshes, asset names or reports that quote them in the repository; every measurement stays local.
- No decryption or TOC work.
- Facts and counts in the docs, not inputs.
