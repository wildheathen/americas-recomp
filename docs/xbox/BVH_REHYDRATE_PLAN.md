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

Result: **the library's CPU entry rejects more than one geometry**. With two identical descriptors it faults inside itself
(`XboxBvhBuilder+0x1606e`) regardless of the descriptor stride used when packing them (48, 56, 64, 72, 80, 96 bytes), and returns
through the SEH wrapper. A single geometry works. So the multi-geometry BLAS of the cell cannot be rehydrated through this export on a
PC as it is; the one-geometry BLAS is verified end to end. The repeated-option support stays in both tools for the day the array layout
is known, and `REHYDRATE_GEOM_STRIDE` is the probe for it.

Steps:

1. New `--geom <vb> <ib> <stride> <count> <vertexFormat> <indexFormat>` option, repeatable, after `<src> <out>`; the old positional
   form stays as a single-geometry shortcut. [done]
2. The comparison harness accepts the same repeated option, builds the geometry list and runs the model and the library. [done]
3. Plumbing check: a multi-geometry BLAS rehydrated with a repeated mesh must return `S_OK` and match the model; this checks the
   descriptor array, not the mesh assignment. [blocked: the library faults with two descriptors]
4. Mesh assignment per geometry index: pair each index with the best mesh by the per-index leaf bounds (the leaf records name the
   geometry index), then re-run the comparison. [blocked by 3]
5. Re-verify every BVH of the cell; record counts only (matches, byte differences, enclosure warnings). [only the single-geometry
   BLAS can be verified]

## Open questions

- Which record links a geometry index to a mesh when one BLAS uses several indices; per-index leaf bounds are the current candidate.
- Whether the 21 box16 enclosure warnings are expected on the console as well (half saturation), or whether the leaf transform for
  this cell should be decoded differently.
- Whether upstream wants this tool set at all: their merged work (#34, #112, #119) covers the CPU model and the address lowering, not
  the library-driven rehydrate or the pairing. A claim in the coordination issue and a pull request would be the next step after the
  tests above.

## Guardrails

- No archives, carved streams, meshes, asset names or reports that quote them in the repository; every measurement stays local.
- No decryption or TOC work.
- Facts and counts in the docs, not inputs.
