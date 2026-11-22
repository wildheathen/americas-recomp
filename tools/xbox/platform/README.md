# tools/xbox/platform

What the package's DLLs need from the platform, and stand-ins for the Xbox-only parts. Result summary: `docs/xbox/PLATFORM_SURFACE.md`.

* `pe_imports.py <dir> <out.json>` lists the static imports (normal and delay-load) and exports of every PE file under a directory.
  Exports include names and `#<ordinal>` entries for nonzero export-address-table slots, including named aliases and forwarders.
  Empty slots are excluded. The ordinal is the export directory's ordinal base plus the slot index, as specified by the
  [PE export address table](https://learn.microsoft.com/en-us/windows/win32/debug/pe-format#export-address-table).
* `make_platform_results.py <imports.json>` classifies each imported function (standard Windows / shipped in the package / needs a shim) and
  stores it under `"platform"` in `docs/xbox/results.json` for the progress map. Windows only.
* `d3d12_xs_device.cpp` (build with `d3d12_xs.cpp`) adds `D3D12XboxCreateDevice`: a PC device behind the Xbox-ABI proxy with the deviations learned so far (`xbox_device_overrides.h`: descriptor heap handles in RAX, resource creation at slot 76). The parameter struct is only logged. On a device without typed-UAV support for `R9G9B9E5_SHAREDEXP` (NVIDIA), slot 76 creates a UAV resource of that format as `R16G16B16A16_FLOAT` (every RGB9E5 value is exact in f16; writes are not rounded to RGB9E5 and the memory layout differs) and slots 18/19 rewrite RGB9E5 views of it; `XS_NO_FORMAT_FALLBACK=1` keeps the original format. `XS_LOG` and `XS_TRACE=1` show what a title does with it.
* `xmem.cpp` is a stand-in `xmem.dll` (XMemAlloc, XMemFree, XMemGetWorkingSetStatistics, XMemVirtualQuery); `d3d12_xs.cpp` forwards the two
  `d3d12_xs.dll` functions the DLLs import to the PC `d3d12.dll`. Build each with `cl /LD /O2 file.cpp /Fe:name.dll`. Argument shapes were read from the callers' machine code;
  the allocation semantics and two output fields remain unverified. `XMEM_LOG=<file>` logs every call.
  Allocation and free records retain the raw `a` and `b` values and append `era_object_type`, `era_page_size`, `era_allocator_id`,
  `era_alignment_log2`, `era_memory_type` and `era_reserved`. These fields interpret `b` using the public ERA bit layout;
  page size and memory type are numeric codes, and alignment is an exponent. Unknown values are printed without rejection.
  This is diagnostic decoding; the allocator still uses `VirtualAlloc` for all attribute words. Sources and limitations are recorded
  in the `XMemAlloc and XMemFree` technical-debt entries.
* `bvh_test.cpp` and `ffx_test.cpp` run the package's BVH builder and FidelityFX DLLs on a PC D3D12 device behind `xbox_device_proxy.h` (what works and where it stops: PLATFORM_SURFACE.md).
* `bink_test.cpp` loads `Bink2Scarlett.dll` next to a stand-in `xmem.dll`, decodes every frame of a video and saves two frames as raw BGRA.
  `carve_bink.py <movies.rpf> <out_dir> [count]` extracts Bink videos from the archive (they are stored raw).
  `wine_smoke.sh <package dir> [work dir]` runs the same test on Linux under Wine: it builds `xmem.dll` and
  `bink_test.exe` with mingw-w64, copies the package's DLLs, carves one video and fails if any frame errors. Needs
  Wine, mingw-w64 and the user's package, so it is not part of the CI tests. The `WINEDLLOVERRIDES` force native
  `xmem`/`d3d12_xs`, so a missing DLL fails loudly instead of falling back; the Bink test loads `xmem` only (a
  one-off `WINEDEBUG=+loaddll` run shows no `d3d12_xs`), and a synthetic D3D12.X caller is the next step for the
  layer itself.

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
* `lower_all.py <dir> <out>` runs the whole chain on a directory of disassembled shaders (all 53 builder shaders assemble; the wave size is pinned only where the result can depend on it, `xbox_dxil_lower.needs_wave64`). `oplower_check.py`, `wavelower_check.py` and `typedload_check.py` check lowered operations against software models.
* `dxasm --hash-only in.ll out.dxil` assembles without validating and writes only the container hash (`dxil_hash.h`), so a partly lowered shader that still calls `@xbox.*` intrinsics can be given to a PC driver.
* `XS_TRACE=1` makes every proxy log the first call of each forwarded slot (argument registers, caller): one run shows which Xbox-only slots a program uses.
* 64-bit addresses: `xbox_address_table.h` is the device side of the lowering's address table (recorded buffers, reserved heap descriptors, the heap
  indexing root-signature flag); `xbox_overrides::WrapDevice` installs it. `proxy_address_test.exe [kernel.dxil [groups] [warp]]` checks it:
  build `proxy_address_kernel.ll` with `xbox_dxil_lower.py`, `dxil_sm66_wave.py ... 0` and `dxasm`, run with `PT_DUMP=<prefix>`, then
  `address_check.py check <prefix>_gpu_u0.bin <prefix>_gpu_u1.bin <threads>`. Build: `cl /nologo /EHsc /O2 /std:c++17 proxy_address_test.cpp d3d12.lib dxgi.lib`.

No game files are part of this repository: the DLLs, the videos and the decoded frames stay local.

## CPU rehydration tools

- `rehydrate_cpu.cpp <src.bin> <vb.bin> <out.bin> [stride] [count] [format] [index.bin] [indexFormat]` calls
  `XboxBvhBuilder.dll`'s `RehydrateRaytracingAccelerationStructureX` for one compressed BLAS. The library reads the source, the
  vertices, the destination and the scratch buffer as **CPU pointers**, as on the console where an allocation's CPU and GPU addresses
  are the same; the tool maps memory at each buffer's GPU virtual address and copies the data there, and its constructor gets an empty
  shader for every pipeline through slot 62. On the synthetic fixture from `make_bvh_fixture.py` the output is identical to the CPU
  model; on a real 103-leaf sample from `core_bvh4.rpf` the call returns `S_OK` and the output matches the model except for placeholder
  values of leaves whose triangle is unusable with the synthetic geometry.
- `make_bvh_fixture.py <dir>` writes that synthetic compressed BLAS (8 leaves, one Box32 level, two Box16 levels), its vertex buffer and
  the CPU model's rehydrated output, for `BVH_REHYDRATE=<dir>` in `bvh_test` or for `rehydrate_cpu`.
- `find_bvh_sample.py <core_bvh4.rpf> <out_dir> [count] [scan_mb]` extracts decompressed BVH data segments (zlib streams) and writes the
  compressed BLAS starting at its 64-byte header.
- `obj_to_geometry.py in.obj out_vb.bin out_ib.bin` converts a Wavefront mesh into the two raw buffers (float3 vertices, u32 triangles).
- `compare_bvh_geometry.py <src.bin> <vb.bin> <ib.bin> [--dll rehydrate_cpu.exe]` runs model and library on the same input and reports the
  byte difference, leaf bounds (non-finite, inverted, degenerate, coordinate range), validation error kinds and whether the leaf
  primitives fit the index buffer.
- `match_bvh_models.py <bvh.bin> <models dir>... [--descriptor <instance208.bin>]` pairs a BVH with exported meshes: it rotates the BVH
  scene bounds by the descriptor's 3x4 transform and compares them with each mesh's world AABB (size and centre, tolerance) and its
  triangle count against `leaves * 2`, the ratio measured in the small pack where the geometry is known.

## XMem trace test

From an x64 Visual Studio prompt, build the stand-in with `cl /LD /O2 xmem.cpp /Fe:xmem.dll`.
Run `python xmem_test.py --dll <path-to-xmem.dll>` with 64-bit Windows Python. The test uses synthetic calls and temporary log files;
it checks both reported attribute words, field boundaries including bit 32, reserved bits, unknown encodings, allocation/free records,
non-allocation records and disabled logging. No game files or GPU are needed.
