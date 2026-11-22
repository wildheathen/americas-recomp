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
- Measured on the first nested pack (10 BVHs, interior cell): the geometry is in a carved window of `levels.rpf`; 13 meshes pair with the
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
   confirms the limit. **Superseded:** the drawable's geometry list gives the mapping (milestones 2-6), and the score compares the
   rehydrated leaves with what the BLAS stored (header bounds, box overrides; milestone 7) rather than with the mesh placed on the
   index, which is what made the old signal degenerate. `assign_bvh_geometry.py` is removed; `rage9_score_blas.py` replaces it.
5. Re-verify every BVH of the cell; record counts only (matches, byte differences, enclosure warnings). [one-geometry BLAS verified;
   multi-geometry BLAS verified end to end with the real drawable geometry: 0/199,744 byte difference]

## Drawable decoder (started)

Open task list, commands and traps: `docs/xbox/DRAWABLE_HANDOFF.md`.

Milestone 1, block layer: `tools/xbox/rage9_vertex_blocks.py` finds the streams that start with the `42 40 00 00` vertex-block
header, reads the tagged slot table (low byte tag, offset x2 from the block start, slot 0 at +0x80) and prints a count per tag from
the adjacent offsets. In a 1 GiB carved window of `levels.rpf` it finds 1,359 blocks and 1,367 tag-`0x42` spans (8 bytes per vertex).
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
113,600 bytes, 10 of 10 headers) and writes each geometry's whole vertex block and packed index buffer. The 32-byte origin/axis/scale
header sits at 96 and the quantized positions start at the vertex-format offset 128. **Correction (milestone 7):** the block stores
each attribute in its own stream, so the positions are one contiguous run of 8-byte records and the other attributes follow it;
`(vb_len - pos_offset) / vertex_count` (20 to 44 bytes) is the byte count per vertex over all streams, not a record stride.
Reading position `i` at `128 + i * that count` was right only for the first vertices and produced the long sliver triangles.

Milestone 4, done: `tools/xbox/rage9_export_geometry.py` converts a dumped geometry to what the rehydration library understands, because
the library only decodes standard DXGI formats (it made no sense of vertex format 16 or index format 53 and ran off the end of the
vertex buffer). Positions are decoded to float3 with the quantized-8 formula, and the meshlet index buffer is expanded primitive by
primitive (0..triangles-1) into a uint32 triangle list, so the primitive order matches the BVH leaves. `rehydrate_cpu` gained an
optional seventh `--geom` value, the vertex byte offset.

End-to-end result on `bvh_08` (6 geometries of drawable B, 1,700 leaves): the library rehydrates with `S_OK` and the
CPU model output is **byte-identical** (0 of 199,744 bytes differ). The model's box16 sentinel was the last difference: the builder
writes +-inf (`0x7C00` / `0xFC00`) when a box exceeds half range, not the 65,504 saturation the model used, so the model now follows the
library. Two boxes of the builder output were inverted in x; they came from the decoder, not the builder (milestone 7).
Leaf coverage per geometry index matches the drawable's triangle counts exactly (5/3, 131/66, 1020/518, 264/139, 1168/617, 654/357),
which closes the index-to-mesh mapping.

Milestone 5, done: `tools/xbox/rage9_match_drawables.py` matches a BLAS to its drawable without a name. The leaf geometry index plus the
leaf primitive slots give the exact triangle count per index, and the drawable's geometry list has the same counts in order, so the
signatures identify the drawable. In the 1 GB window it enumerates 1,158 drawables and matches, with a unique hit, `bvh_00` ->
drawable A (68/234/48) and `bvh_08` -> drawable B; `bvh_02` ->
drawable C is a prefix match (the BLAS uses 3 of its 7 geometries). Rehydrating those two new cases gives `S_OK`
and byte-identical model output again (`bvh_00`: 0/18,880; `bvh_02`: 0/33,280; the latter had three infinite box16 bounds, same as
the library, which also came from the decoder errors fixed in milestone 7).

Milestone 6, partial: the leaf index is the position in the geometry list **of the geometries the BVH uses**, which can be a
subsequence of the drawable. `bvh_01` matches the 13-geometry drawable in stream `0x39c05010` uniquely after skipping index 8
(2 triangles), and `bvh_03` matches the 8-geometry drawable in `0x39c8b010` uniquely after skipping index 6 (88 triangles); the skipped
geometry is shifted out, so every index above it moves down by one. `bvh_07` is a 3-of-12 subsequence candidate that still needs a
bounds check. `bvh_04` (124 candidates) and `bvh_09` (2 plus 9 in the second window) are ambiguous; `bvh_05` has no match in either
window; `bvh_06` is outside both (16 of its 29 counts exist in no geometry of the window). The selection rule itself is not yet known
and is the first open task in `docs/xbox/DRAWABLE_HANDOFF.md`.

Milestone 7, done: **geometry checks that do not depend on the library.** A byte-identical library result only shows that the model
and the library agree on the input; wrong vertices give the same wrong output in both. Three checks use what the compressed BLAS itself
recorded from the true geometry:

1. *Header bounds.* The header AABB, mapped through the leaf transform, must contain every rehydrated leaf.
2. *Stored box overrides.* Box groups carry per-component override values written by the original build. Each must lie inside the
   range the leaves give for that slot and be no looser than it.
3. *Hierarchy fit.* The stored topology was built on the true triangles, so the total surface area of the internal boxes drops sharply
   when the geometry is right.

They found two decoder errors that the library comparison could not see:

- The quantized-8 positions are a separate stream of 8-byte records (see milestone 3). Reading them interleaved left 17-31 % of the
  edges longer than half the mesh diagonal; reading the stream leaves 0 % on 9 of 12 geometries and at most 4 % on the rest.
- The BLAS is built from the geometry in the frame of its 208-byte instance descriptor: the 3x4 at `+0x50`, row major, applied as the
  geometry transform (`x' = M * x`). The ten samples carry the identity or an axis-aligned 90-degree rotation. Without it 175 of 181
  leaves of `bvh_00` and 207 of 273 of `bvh_02` lie outside the header bounds; with it, none. The transposed matrix does not fit
  (`bvh_00`: 173 outside). The quantized `axis` word is correct as decoded; the rotation is the instance's.

`rage9_export_geometry.py` reads the position stream and takes `--instance <descriptor>`. With both fixes, on the three verified
samples: 0 leaves outside the header bounds, 23 of 23 stored overrides consistent on `bvh_08` (12 of 23 before), internal box area
3.6-7.4 % of the previous decode, `validate_rehydrated_bvh` clean with zero tolerance (no inverted, infinite or unenclosed box), and the
library still returns `S_OK` with 0 differing bytes against the model (18,880 / 33,280 / 199,744 bytes).

The validator now treats `+-inf` as a valid conservative bound and fails only on NaN; an infinite outer bound no longer turns the
enclosure tolerance into NaN.

Milestone 8, done: **every sample whose drawable is in the carved windows is identified and verified.** `rage9_score_blas.py` scores
each candidate with the checks of milestone 7, and the accepted one is then rehydrated by the library:

| BLAS | leaves | used geometries of the drawable | candidates | checks | library vs model |
| --- | --- | --- | --- | --- | --- |
| `bvh_00` | 181 | 0-2 of 3 | 1 | consistent, bounds error 9e-8 | 0 / 18,880 bytes |
| `bvh_01` | 21,814 | all 13 but index 8 | 1 | consistent, bounds 0, 335/335 overrides | 0 / 2,512,512 bytes |
| `bvh_02` | 273 | 0-2 of 7 | 1 | consistent, bounds error 1.7e-7 | 0 / 33,280 bytes |
| `bvh_03` | 1,844 | all 8 but index 6 | 1 | consistent, 69/69 overrides | 0 / 208,448 bytes |
| `bvh_04` | 2 | 1 geometry | 467 | one consistent (bounds 0); every other rejected | 0 / 384 bytes |
| `bvh_07` | 566 | 9-11 of 12 | 2 | [9, 10, 11] consistent; [7, 10, 11] rejected (bounds error 0.08) | 0 / 64,704 bytes |
| `bvh_08` | 1,700 | 0-5 of 10 | 1 | consistent, bounds 0, 23/23 overrides | 0 / 199,744 bytes |
| `bvh_09` | 103 | 1 geometry | 2 (+10 in the second window) | one consistent (bounds 0); the others miss by 0.44 to 23 | 0 / 11,328 bytes |

`bvh_05` ([152, 84]) and `bvh_06` (29 geometries) have no consistent candidate in either window, even with the geometries taken in
any order. A search of the whole of `levels.rpf` (71 GB, read in 1 GB blocks with an 8 MB overlap; GB 0-1 hold no drawables) finds no
single drawable for either: no model, and no drawable with all its models concatenated, contains `bvh_06`'s signature, and none of
the 250 drawables that hold a 152- and an 84-triangle geometry keeps exactly those two under the shader rule (milestone 9). `bvh_06`'s
20 counts of 1,000 triangles or more are scattered, at most 3 in one stream. So these two BLAS are either built from several
drawables or from geometry outside `levels.rpf`; the drawable enumeration relies on the name pointer at `+168`, which is the other
limit of the search.

What it took beyond milestone 7:

- *Graphics pages are not all tagged.* A segment is the concatenation of streams in stored order, but a page can start with an index
  buffer instead of the `42 40 00 00` vertex-block tag. `bvh_01`'s segment is a 1,179,648-byte page followed by an 18,720-byte stream
  that starts with an index buffer; only that pair validates all 13 vertex headers. The scorer keeps every stream as a possible page
  and sizes the run from the buffer extent.
- *The bake rounds overrides differently.* On `bvh_03` the right geometry misses 2 of 69 stored overrides by 1-2 float32 ulp; the
  library output (0 bytes different) has the same 14 boxes that miss enclosure by about an ulp. The override check allows 4 ulp,
  `validate_rehydrated_bvh` needs `enclosure_rtol=1e-6` on that sample. A fused multiply-add in the quantized decode was tested and
  ruled out: it moves `bvh_08` from a zero bounds error to 4.7e-8.
- *The selection rule is the shader* (milestone 9).

Milestone 9, done: **which geometries the BLAS keeps.** It is not a prefix rule (`bvh_07` uses the last three of twelve geometries,
`bvh_01` and `bvh_03` skip one in the middle), and it is not in the 160-byte geometry record (`+88`, `+92`, `+96` equal for used and
skipped geometries, `+48` hash-like), the index descriptor, the vertex-format flags or the presence of the data in the pages. It is the
geometry's shader: the drawable's shader group is at `+0x10`, with a pointer array at group `+0x10` and a u16 count at group `+0x18`;
the model's u16 array at `+0x20` gives each geometry's shader index (the identity on every model seen); the shader's first word is a
name hash. **A geometry is left out of the BLAS exactly when its shader hash is hash A or hash B.** On the 8 identified
BLAS (48 geometries, 8 distinct shader hashes: 6 kept, 2 left out) the rule predicts the used set every time, including `bvh_02` (3 of 7: the 4 skipped
ones carry those two hashes) and `bvh_07` (8 of the 9 skipped carry hash A, one hash B). The shader names are not needed and
not recorded. `rage9_dump_cell.py` reads the hash per geometry and `rage9_score_blas.py` reports, per candidate, whether its used set is
the predicted one.

## Open questions

- ~~The geometry index array lives in the drawable, not in the BLAS.~~ Resolved with a nuance: the drawable's primary-LOD geometry list
  gives the order, and the leaf geometry index is the position of the geometry **among those the BVH uses**, which can be a subsequence
  of the list (see milestones 2-6). Public measurements of the same title's model resources
  were the fact source: a geometry record points to the vertex buffer at `+8` and to the meshlet descriptor, which selects the index
  buffer, at `+64`; the buffers' own descriptors carry the vertex/reference/triangle counts. The parser is our own; the reference
  implementation is GPL-3 and no code was copied.
- ~~The bound checks need a decision.~~ Resolved in milestone 7: `+-inf` is valid, only NaN fails, and the inverted and infinite boxes
  were decoder errors.
- The meaning of the quantized `axis` word beyond the three values seen (0 and 2 here), and whether the instance transform can carry a
  translation or a non-orthogonal part; every sample so far is a pure axis-aligned rotation.
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
