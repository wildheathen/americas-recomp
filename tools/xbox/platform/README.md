# tools/xbox/platform

What the package's DLLs need from the platform, and stand-ins for the Xbox-only parts. Result summary: `docs/xbox/PLATFORM_SURFACE.md`.

* `pe_imports.py <dir> <out.json>` lists the static imports (normal and delay-load) and exports of every PE file under a directory.
* `make_platform_results.py <imports.json>` classifies each imported function (standard Windows / shipped in the package / needs a shim) and
  stores it under `"platform"` in `docs/xbox/results.json` for the progress map. Windows only.
* `d3d12_xs_device.cpp` (build with `d3d12_xs.cpp`) adds `D3D12XboxCreateDevice`: a PC device behind the Xbox-ABI proxy with the deviations learned so far (`xbox_device_overrides.h`: descriptor heap handles in RAX, resource creation at slot 76). The parameter struct is only logged. On a device without typed-UAV support for `R9G9B9E5_SHAREDEXP` (NVIDIA), slot 76 creates a UAV resource of that format as `R16G16B16A16_FLOAT` (every RGB9E5 value is exact in f16; writes are not rounded to RGB9E5 and the memory layout differs) and slots 18/19 rewrite RGB9E5 views of it; `XS_NO_FORMAT_FALLBACK=1` keeps the original format. `XS_LOG` and `XS_TRACE=1` show what a title does with it.
* `xmem.cpp` is a stand-in `xmem.dll` (XMemAlloc, XMemFree, XMemGetWorkingSetStatistics, XMemVirtualQuery); `d3d12_xs.cpp` forwards the two
  `d3d12_xs.dll` functions the DLLs import to the PC `d3d12.dll`. Build each with `cl /LD /O2 file.cpp /Fe:name.dll`. Argument shapes were read from the callers' machine code;
  the meaning of the attribute bits and of two output fields is a guess (see the comments in `xmem.cpp`). `XMEM_LOG=<file>` logs every call.
* `make_bvh_fixture.py <dir>` writes a synthetic compressed BLAS, its vertex buffer and the CPU model's rehydrated output (no game data).
  `bvh_test` picks them up with `BVH_REHYDRATE=<dir>`: that path calls `RehydrateRaytracingAccelerationStructureX`, the compute side of the
  builder (`PLATFORM_SURFACE.md` explains the CPU-pointer aliasing the console's unified memory needs).
* `find_bvh_sample.py <core_bvh4.rpf> <out_dir> [count] [scan_mb]` pulls compressed BLAS out of the archive (the zlib data segments, header
  at +0x100) for local testing; nothing from the archive is committed.
* `rehydrate_cpu.cpp <src.bin> <vb.bin> <out.bin> [stride] [count] [format] [index.bin] [indexFormat]` drives that same CPU rehydration
  entry as a standalone tool (same aliasing) and writes the rehydrated BVH. On the fixture from `make_bvh_fixture.py` its output is identical
  to the CPU model; on a real 103-leaf sample from `core_bvh4.rpf` (synthetic vertices and indices) the two differ only in 474 of 11,328 bytes,
  the placeholder extremes a leaf gets when its triangle is unusable.
* `obj_to_geometry.py in.obj out_vb.bin out_ib.bin` converts a Wavefront mesh into those two buffers, e.g. one exported by a model viewer
  (`--extract-models` of PercyStar writes .obj). Fed to a real BLAS the rehydration runs (S_OK) but the result is not the game's mesh: the
  compressed BLAS does not say which vertex/index buffers it references. The CPU model now uses the library's own markers (empty bounds are
  ±FLT_MAX, half bounds saturate to ±65504): on a real 103-leaf sample that removes every non-finite AABB and leaves 144 of 11,328 bytes
  different, 36 coordinates off by one ULP where half bounds saturate (the sample is fed out-of-range synthetic geometry). The corpus figures
  in `docs/xbox/BVH_CONTAINER.md` were measured before this constant change.
* `compare_bvh_geometry.py <src.bin> <vb.bin> <ib.bin> [--dll <rehydrate_cpu.exe>] [--out report.json]` runs the model and, optionally, the
  library on one BLAS and one geometry and reports the byte difference, the leaf bounds (non-finite, inverted, degenerate, coordinate range),
  the validation error kinds and whether the leaf primitives fit the index buffer. Baselines: with synthetic geometry the leaf coordinates are
  0..786,432 and every Box16 bound saturates (72 enclosure errors); an exported character mesh gives z around -247,768, still outside the
  ±65,504 half range. The leaf transform multiplies by up to 2^20, so the geometry the shader reads is the small pre-transform (quantised)
  position, not an .obj in world space.
* `xbox_device_proxy.h` now answers `QueryInterface` with the proxy itself when the real object returns its own pointer, so a caller
  that queries for a newer interface keeps going through the overrides. `bvh_test` records the Xbox command-list slot map learned so
  far (Dispatch 14, pipeline state 25, Xbox-only 26-29, PC slot + 4 from 30) in its probe overrides.
* `bvh_test.cpp` and `ffx_test.cpp` run the package's BVH builder and FidelityFX DLLs on a PC D3D12 device behind `xbox_device_proxy.h` (what works and where it stops: PLATFORM_SURFACE.md). `XS_HEAP=1` adds `CBV_SRV_UAV_HEAP_DIRECTLY_INDEXED` to the builder's signatures (including the ones it creates from `RTS0` through device slot 16); `BVH_REPLACE_DIR` with the dumps from `BVH_DUMP_DIR` lowered by index creates all 53 pipelines. `BVH_BUILD=1` tries a real build call (reached, then a crash in a builder thread: see PLATFORM_SURFACE.md).
* `bink_test.cpp` loads `Bink2Scarlett.dll` next to a stand-in `xmem.dll`, decodes every frame of a video and saves two frames as raw BGRA.
  `carve_bink.py <movies.rpf> <out_dir> [count]` extracts Bink videos from the archive (they are stored raw).
* `addr_kernel.ll`, `addr_test.py` and `addrmap_test.cpp` cover the 64-bit-address lowering. The kernel calls the Xbox
  `global_load/store/atomic` and `s_load` intrinsics; the lowering replaces every access with a lookup in a 16-entry (base, size,
  heap slot) table read through `ResourceDescriptorHeap` (slot 0 = table, 1 = dummy, 2 and up = the resources), then the access at
  `address - base`. `addr_test.py [groups] [dxasm] [addrmap_test]` lowers, raises to SM 6.6, assembles, signs and runs it;
  `addrmap_test` builds the heap and compares every output word and every store/atomic target word with a CPU model, on the GPU
  and on WARP.

* `xgr/idl_table.py <dir of IDLs> <table.json>` reads the interface definitions of another xgameruntime reimplementation (Xodus, LGPL-2.1; not part of this repo) and
  `xgr/gen_stub.py <table.json> <out_dir>` generates a logging stand-in `xgameruntime.dll` from it (60 interfaces, 1505 vtable slots). With `XGR_LOG=<file>` it records which
  interfaces a game queries and which methods it calls first, and logs interface ids missing from the table (where the console build differs from the PC one).
  Neither the IDLs nor the generated table are committed.

## First run with the game executable
1. `python exe_report.py <exe or directory> --out report --stubs` prints header facts (entry point, subsystem, sections) and classifies every
   static import: standard Windows, shipped in the package, covered by the stand-ins here, or *needs* an implementation. `report/needs.json`
   lists the last group; `report/stubs/` holds generated logging stand-in DLLs for it (`build.bat` from an x64 VS prompt).
2. Run the game with the stubs beside it and `STUB_LOG=<file>`: every call is logged as `dll!name(rcx, rdx, r8, r9) tid=..`
   (`STUB_ABORT=<text>` stops at the first call whose name contains the text). That trace says which functions are called and in which order.
   Check on the known case: a generated `xmem.dll` stub in front of `Bink2Scarlett.dll` logged `xmem!XMemAlloc(0x3c20, 0x5e50000, ..)`,
   the same size and attribute word found by disassembly.
3. Replace stubs with real implementations as behaviour becomes known (`xmem.cpp`, `d3d12_xs.cpp` are the pattern).
Static imports only: calls made through GetProcAddress, COM or vtables do not show up in step 1.

## Shader and BVH side, once the game runs
* Capture: put `xbox_device_proxy.h` in front of the device the game creates; `Slot62` (pipeline creation) in `bvh_test.cpp` shows how to dump every compute shader
  (`BVH_DUMP_DIR`). Slots seen so far are 1, 14, 15, 16, 18, 19, 27, 38, 62, 76; per-slot hit counters (`PrintHits`) show which others the game uses.
* Lower and sign: `dxc -dumpbin` -> `xbox_dxil_lower.py in.ll out.ll` -> `dxasm in.ll out.dxil` (replaces `@xbox.*` intrinsics; handles min/max f16 and med3 so far;
  it reports the intrinsics it could not replace). `dxil_sm66_wave.py` pins the wave size when a shader depends on 64 lanes.
* Try: `bvh_test.exe` with `BVH_REPLACE_DIR=<dir of repl_NN.dxil>` for pipeline creation; `dispatch_test.exe shader.dxil [groups] [seed]` for a dispatch on GPU and WARP.
* `lower_all.py <dir> <out>` runs the whole chain on a directory of disassembled shaders (52 of the 53 builder pipelines, i.e. all 52 containers, assemble and sign). `oplower_check.py`, `wavelower_check.py` and `typedload_check.py` check lowered operations against software models.
* `dxasm --hash-only in.ll out.dxil` assembles without validating and writes only the container hash (`dxil_hash.h`), so a partly lowered shader that still calls `@xbox.*` intrinsics can be given to a PC driver.
* `XS_TRACE=1` makes every proxy log the first call of each forwarded slot (argument registers, caller): one run shows which Xbox-only slots a program uses.
* Not done: the 31 shaders using raw 64-bit addresses are lowered and assemble, but a dispatch needs a descriptor heap built from the
  resources the builder creates and a heap-indexed root signature (see PLATFORM_SURFACE.md and `addr_kernel.ll`).

No game files are part of this repository: the DLLs, the videos and the decoded frames stay local.
