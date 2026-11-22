# Drawable decoder and BVH geometry mapping: handoff

Status after the decoder work on `feat/bvh-rehydrate-tools`. The chain BLAS -> drawable -> per-geometry buffers -> rehydration is
working and verified byte-for-byte on three samples. What is left is the selection rule for geometries the BVH leaves out, the
ambiguous samples, two loose ends, and a second window. Everything below is measured on this machine; the raw windows and samples
live outside the repository and must stay there.

## 1. State

Tools (all in `tools/xbox`):

| file | role |
| --- | --- |
| `rage9_vertex_blocks.py` | block layer: streams starting with `42 40 00 00`, tagged slot table |
| `rage9_walk_drawable.py` | system-page walk: drawable -> LOD -> models -> geometries, counts only |
| `rage9_dump_cell.py` | resolves the graphics page run, writes each geometry's vertex block and meshlet buffer |
| `rage9_export_geometry.py` | quantized-8 -> float3, meshlet -> uint32 triangle list (what the library decodes) |
| `rage9_match_drawables.py` | BLAS -> drawable by triangle-count signature, no name needed |
| `compare_bvh_geometry.py` | CPU model vs library output, leaf metrics, report JSON |
| `platform/rehydrate_cpu.cpp` | drives `XboxBvhBuilder.dll`; `--geom` takes an optional vertex byte offset |
| `bvh_rehydrate_cpu.py` | CPU model (library-compatible, incl. the box16 inf sentinel) |

Plan and measurements: `docs/xbox/BVH_REHYDRATE_PLAN.md`, `docs/xbox/BVH_CONTAINER.md`.

Verified end to end (library `S_OK`, CPU model byte-identical to the library, and since the decoder fixes of plan milestone 7 also
checked against what the BLAS itself stores: every leaf inside the header bounds, every stored box override consistent, validation
clean with zero tolerance):

| BLAS | geometries | bytes | difference | identification |
| --- | --- | --- | --- | --- |
| `bvh_00` | 3 | 18,880 | 0 | unique exact signature [68, 234, 48] |
| `bvh_02` | 3 of 7 | 33,280 | 0 | unique prefix [116, 336, 90]; instance rotation y<->z, x negated |
| `bvh_08` | 6 | 199,744 | 0 | unique exact signature [5, 131, 1020, 264, 1168, 654]; identity instance |

Signature matching over the 1 GB window (1,158 drawables, 5,346 geometries, 1,046 distinct counts):

| BLAS | signature | result |
| --- | --- | --- |
| `bvh_01` | 12 values, 1318...40 | **unique ordered subsequence** of the 13-geometry drawable in stream `0x39c05010`; it skips geometry index 8 (2 triangles) |
| `bvh_03` | 7 values, 900...118 | **unique ordered subsequence** of the 8-geometry drawable in stream `0x39c8b010`; it skips index 6 (88 triangles) |
| `bvh_07` | [4, 3, 1120] | subsequence of indices 7, 10, 11 of the 12-geometry drawable in `0x39e77010`; **needs bounds confirmation, could be a coincidence** |
| `bvh_04` | [2] | 124 candidate drawables (one geometry, 2 triangles) |
| `bvh_09` | [201] | 2 candidates in this window, 9 in the second |
| `bvh_05` | [152, 84] | no match, exact or subsequence, in either window |
| `bvh_06` | 29 values | no match; 16 of its 29 counts appear in no geometry of the window, so its drawable is outside |

Leaf-derived triangle counts are exact: each leaf holds one or two primitive slots, so summing slots per geometry index gives the
geometry's triangle count (verified against the drawable list on all matched samples).

## 2. Local data (never commit)

- Everything below lives in a local work folder outside the repository, written `<work>` here.
- Window: `<work>\tiles\win3_all.rpf` (1 GB carve of `levels.rpf`), second window
  `win40.rpf`, per-window model exports in `models_00`, `models_40`.
- Samples: `<work>\blas\bvh_00.bin` ... `bvh_09.bin`, plus the extracted `instance208` descriptors and reports.
- Library driver: `<work>\bvh\rehydrate_cpu6.exe` with `XboxBvhBuilder.dll`, `d3d12_xs.dll`, `xmem.dll` beside it.
- Carve helper (ad-hoc, not in the repo): `<work>\scan_levels_windows.py`; the stream reader used everywhere is
  `rage9_dump_cell.streams()`: zlib streams found at 16-byte-aligned `78 da`, inflated with a 64 MB cap.

Build the driver from the repository file:

```powershell
$vc='C:\Program Files (x86)\Microsoft Visual Studio\2019\BuildTools\VC\Auxiliary\Build\vcvars64.bat'
cmd /c "`"$vc`" >nul && cl /nologo /EHsc /O2 /std:c++17 rehydrate_cpu.cpp d3d12.lib dxgi.lib /Fe:rehydrate_cpu.exe"
```

## 3. Layout facts (independently measured)

System pointers are u64 based at `0x50000000`; graphic pointers at `0x60000000`. All counts below are little endian.

- drawable: bounds f32x3 at `+48` / `+64`, LOD pointer `+80`, name pointer `+168`.
- LOD: u16 model count `+8`, capacity `+10` (equal), model pointer array at the LOD address.
- model: geometry pointer array `+8`, count `+16` (`==` `+18` and `+54`), plus a pointer at `+0x20` to a u16 array that was
  `0..count-1` (identity) on every drawable checked; purpose unknown.
- geometry (160-byte record): vertex buffer `+8`, index/meshlet descriptor pointer `+64`.
- vertex buffer: u32 element count `+8`, u16 element size in bytes `+12`, data pointer `+24`, vertex-format descriptor `+64`.
- vertex format descriptor (320 bytes): position offset `+0`, position size `+0xD0`, position format `+0x104`, flags `+0x138`
  with vertex count `= flags >> 10`.
- index descriptor: index buffer pointer `+0`, u16 cluster count `+8`, u16 triangle count `+10`, u32 binding `+12`.
- index buffer: u32 element count `+8`, u16 stride `+12`, data pointer `+24`.
- `vb_len = elements * element size`. The block keeps **each attribute in its own stream**: the quantized-8 positions are one
  contiguous run of 8-byte records from `position_offset`, and the other attributes follow. `(vb_len - position_offset) /
  vertex_count` (20 to 44 bytes seen) is the byte count per vertex over all streams, **not** a position stride; reading position `i`
  at `position_offset + i * that count` is right only for the first vertices and was the cause of the sliver triangles, the two
  inverted boxes of `bvh_08` and the infinite bounds of `bvh_02`.
- quantized-8: 32-byte header at `position_offset - 32` (3 f32 origin, u32 axis, 3 f32 scale); each vertex packs x in 24 bits,
  y in 24 bits, z in 16 bits, slot order fixed by the axis word (decoded in `bvh_rehydrate_cpu._read_vertex`).
- instance transform: the BLAS is built from the geometry in the frame of its 208-byte instance descriptor, the 3x4 at `+0x50`
  (row major, `x' = M * x`). The samples carry the identity or an axis-aligned 90-degree rotation (`bvh_00`: about y; `bvh_02`:
  y<->z with x negated); without it most leaves fall outside the header bounds. The transposed matrix does not fit.
- meshlet index format: decode is in `bvh_rehydrate_cpu._read_triangle_indices`; expand primitives `0..triangles-1` in order.
- graphics segment: concatenation of `42 40 00 00` streams in stored order. The tool takes the shortest run (up to 6 pages) that
  validates the vertex header of every geometry; for the verified drawable that was 2 pages from `0x39e84d00`, 113,600 bytes.
- box16: the builder writes `+-inf` (`0x7C00` / `0xFC00`) when a box exceeds half range; saturation to 65,504 is wrong.

The library only decodes standard DXGI formats. Passing RAGE9 vertex format 16 or index format 53 makes it read the buffer as
ordinary vertices/indices and run out of bounds (access violation inside `XboxBvhBuilder+0x162ed`). Convert first
(`rage9_export_geometry.py`). It needs one descriptor per index up to `max geom_index`; fewer crashes at `+0x1606e`.

## 4. Reproduce

```powershell
python -I tools\xbox\rage9_match_drawables.py <window.rpf> <blas.bin> ...
python -I tools\xbox\rage9_dump_cell.py <window.rpf> <drawable name> <out_dir> [indices]
python -I tools\xbox\rage9_export_geometry.py <out_dir> --instance <instance208 descriptor of the BLAS>
python -I tools\xbox\compare_bvh_geometry.py <blas.bin> --geom <vb> <ib> 12 <count> 6 42 0 [--geom ...] `
    --dll <path>\rehydrate_cpu.exe --out report.json
```

`compare_bvh_geometry.py` prints model validity/leaf metrics and `diff_bytes` against the library; 0 is the target. A difference of 0
does not prove the geometry: wrong vertices give the same wrong output in the model and the library. Check the model validity (no
inverted or unenclosed box with zero tolerance) and the header bounds as well; plan milestone 7 lists the three checks.
The drawable name is needed by `rage9_dump_cell.py` only; it is a plaintext string in the system page, and the matcher prints the
names of the candidates locally. **Do not put those names, buffers or reports in the repository.**

## 5. Open work

### 5.1 The selection rule (highest value)

The leaf `geom_index` is the position in the drawable's geometry list **of the geometries the BVH actually uses**. On the verified
samples that is a prefix; on `bvh_01` and `bvh_03` the BVH skips exactly one geometry (2 and 88 triangles), so indices above the
skipped one are shifted down by one. `bvh_07` suggests a 3-of-12 selection. The rule is not visible in the fields decoded so far
(vertex format, position offset, cluster/triangle counts, descriptor binding, model words).

Ideas, in order of cost:

1. Rehydrate each candidate mapping and score it with the checks of plan milestone 7 (header bounds, stored box overrides, internal
   box area). With the decoder fixed these discriminate geometry, so they confirm the subsequence mapping for
   `bvh_01`/`bvh_03`/`bvh_07` independently of counts; it is the same tool needed for 5.2.
2. Check whether the skipped geometry is the one whose data is absent from the resolved graphics pages (streamed separately).
   `rage9_dump_cell.py` validates all headers of the run it picks; dump all geometries and see if the skipped one fails first.
3. Look at the 160-byte geometry record's remaining words (12-13 look like hashes, 14-15 unknown) and at the u32 `binding` at
   descriptor `+12`; log them for used vs skipped geometries across several drawables.
4. Check the model `+0x20` u16 array on a drawable where it is not the identity; it may be the draw order the BVH uses.
5. Check whether the same drawable appears in two BLAS with different used sets (would rule out a static resource flag).

Acceptance: a rule that predicts the used set for the three verified samples and for `bvh_01`/`bvh_03`/`bvh_07`, with the
rehydration difference still 0 when the predicted mapping is used.

### 5.2 Remaining samples

- `bvh_04` (124 candidates) and `bvh_09` (2 + 9 candidates): rehydrate each candidate (with the BLAS's instance transform) and keep
  the one whose leaves fit the header bounds and the stored overrides with the smallest internal box area.
- `bvh_05`: no exact or subsequence match in either window; try a small count tolerance (degenerate triangles dropped) and the
  other carved windows, and the AABB method.
- `bvh_06`: its drawable is outside both windows (16 of 29 counts do not exist); carve another window or record it as unavailable.
- `bvh_07`: confirm or drop the subsequence candidate with the AABB check.

### 5.3 Bound-box checks and the validator (done)

`validate_rehydrated_bvh` now accepts `+-inf` bounds (the builder's box16 overflow) and fails only on NaN, with tests for both. The
`bvh_02` infinite bounds and the `bvh_08` inverted boxes were decoder errors (separate position stream, instance transform); with the
fixes all three verified samples validate cleanly with zero tolerance. See plan milestone 7.

### 5.4 Replace `assign_bvh_geometry.py` (done)

Removed. `rage9_score_blas.py` finds the candidate drawables of a BLAS (ordered-subsequence match of the triangle counts), decodes
each candidate from its graphics page run with the instance transform, rehydrates it and accepts it only if
`stored_geometry_checks` is consistent; `--write` emits the buffers and `export.json` for `compare_bvh_geometry.py`. On `bvh_08` it
finds the single candidate, the 6 geometries in order and the same 2-page run, with every check consistent.

### 5.5 Documentation fixes

- The drawable name is plaintext in the system page (pointer at `+168`), so the hashed dictionaries are not needed to name a
  drawable. Keep the names out of the repository; state the fact, not the strings.
- Keep `BVH_REHYDRATE_PLAN.md` in sync with the subsequence result (it is updated for now), and record the position stream, the
  instance transform and the box16 inf sentinel in `BVH_CONTAINER.md` next to the rehydration section.

### 5.6 Second window and robustness

`win40.rpf` is already carved. Run the matcher/dump/export pipeline on drawables from it and check: the name pointer may live in a
different stream than the drawable (a structure-only enumeration found 4 more drawables than the name-based one); the graphics page
run search assumes at most 6 adjacent pages; a drawable with more than one model (LOD) exercises the per-model lists. Acceptance: at
least one `win40` drawable rehydrated end to end with difference 0.

## 6. Guardrails

- Read `CONTRIBUTING.md`. Nothing from the game, no asset names, no buffers, no keys, no SDK files, no binaries, no file over 500 KB.
  The environment strips asset names from commits; keep them out in the first place.
- Stage files by name; check `git diff --cached --stat` before every commit.
- This fork only. A pull request upstream needs the owner's explicit approval.
- Public GPL-3 documentation may be used for facts only; no code copied.

## 7. Traps

- PowerShell redirection (`> file`, `*> file`) writes UTF-16. Read it with `encoding='utf-16'`, or write with
  `Set-Content -Encoding utf8`. Never use `git show > file` (it has corrupted files before).
- The library CPU path aliases memory at each buffer's GPU virtual address; it makes no device calls. Do not free or move that
  memory before the call returns.
- `rehydrate_cpu` passes `IndexCount = index bytes / 4` for 32-bit indices; give it a real uint32 triangle list, not the meshlet
  buffer.
- `--geom` takes seven values, plus an optional eighth (vertex byte offset) that must be numeric and directly after the others.
- The window files are byte carves, not RPF containers: streams are found by scanning zlib headers, never by an archive index.
