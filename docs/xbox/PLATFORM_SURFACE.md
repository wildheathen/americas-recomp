# Platform surface of the 16 DLLs in the package root (static imports only)
Tool: tools/xbox/platform/pe_imports.py (normal + delay-load imports; no delay-load imports were present). Per-function classification: the "platform" key of docs/xbox/results.json (make_platform_results.py).

| Category | DLLs | Functions |
|---|---|---|
| Standard Windows (resolves on this PC: every one of the 492 functions found with GetProcAddress) | 61 (55 api-sets + 6 others) | 492 |
| Provided by the package itself (msvcp140, vcruntime140, vcruntime140_1; every imported name is exported by the package copy) | 3 | 113 |
| Xbox-specific, no PC equivalent file | 2 (xmem.dll, d3d12_xs.dll) | 6 distinct |

Xbox-specific imports:
- xmem.dll: XMemAlloc, XMemFree (Bink, upscaler), XMemGetWorkingSetStatistics (upscaler), XMemVirtualQuery (XboxBvhBuilder)
- d3d12_xs.dll: D3D12SerializeVersionedRootSignature (XboxBvhBuilder), D3D12CreateRootSignatureDeserializer (upscaler); both exist with the same names in the PC d3d12.dll

Shims (tools/xbox/platform/xmem.cpp, d3d12_xs.cpp): all six DLLs that import something (XboxBvhBuilder, both FidelityFX, Bink, XCurl, libHttpClient.GDK) load on this PC with them; Bink decodes a real video (tools/xbox/platform/bink_test.cpp; the video was carved with carve_bink.py).
Argument shapes for the three less common xmem calls come from disassembling the callers; the meaning of the attribute bits, of the working-set struct and of the +0x30 field of XMemVirtualQuery is a guess.
Static imports only: this does not include what the DLLs call through COM/vtables (D3D12 device methods) or what the game executable imports.

## XboxBvhBuilder.dll on a PC device (tools/xbox/platform/bvh_test.cpp)
`bvh_test.exe` loads the DLL next to the stand-in `xmem.dll` / `d3d12_xs.dll`, creates a PC D3D12 device and constructs the
`XboxBvhBuilder(ID3D12Device11*, bool)` class through its exported constructor, handing it an Xbox-layout device stand-in
(`xbox_device_proxy.h`, see below). In order:
1. The first root signature carries flags `0x3000000`, bits the PC runtime does not define (its flags are all in the low 16 bits), so the PC
   serializer fails with `E_INVALIDARG`. `d3d12_xs.dll` clears the high byte on a copy of the descriptor (meaning of the bits unknown;
   `XS_KEEP_XBOX_FLAGS=1` forwards it unchanged, `XS_LOG=<file>` logs every serialize call).
2. The constructor then creates its 53 compute pipelines through device vtable slot 62, called as (description, 0, 0, interface id, output).
   On a PC device slot 62 is `CreateStateObject` (three arguments), so called directly it crashes inside the PC runtime. The stand-in
   maps slot 62 to `CreateComputePipelineState(description, iid, output)`; the description is a plain `D3D12_COMPUTE_PIPELINE_STATE_DESC`.
3. **WARP**: all 53 pipelines are created (`S_OK`, shader blobs from 4 KB to 30 KB), the constructor returns, and
   `GetRaytracingAccelerationStructurePrebuildInfo` answers for a 3,000-vertex triangle geometry: result 204,800 bytes, scratch 128,704
   (the PC driver's own answer for the same input: 115,984 / 124,560, so the layouts differ as expected). The destructor runs.
   **AMD Radeon RX 7800 XT**: the driver's shader compiler (`amdxc64.dll`) crashes on most of the builder's shaders. Compiling each of the 53
   blobs alone (`BVH_ONLY=n`): 9 compile, 39 crash the compiler (access violation inside `amdxc64.dll`), 5 fail with an error (one message:
   "Unknown pointer operation encountered during ParseAddress"); after a crash the device is removed, so they have to be run one per process.
   WARP creating the pipelines does not show that they compile: it may defer that.
   **NVIDIA GeForce RTX 5090** (driver 617.42, Windows 11): compiling each blob alone, 52 of 53 are accepted, including the 44 that call
   `@xbox.*` (acceptance may be deferred compilation, as on WARP). Blob 48 (`LeafRehydration`, the only one with `TypedLoad.i32`) ends the
   process with a fail-fast in the user-mode driver (`nvwgf2umx.dll`, `0xc0000409`), which `__try` cannot catch; in one full run it
   returned `E_OUTOFMEMORY` instead and the constructor finished. The full constructor therefore usually dies with no output after
   "constructing"; `BVH_ONLY` runs are the way to test the other blobs on NVIDIA.
   **Cause**: not a random driver bug. The shaders are Xbox DXIL: 44 of the 53 call non-standard functions `@xbox.*` (declared with opaque
   `i32` opcode first arguments, written by the Xbox DXC, `dxcxbox`), and those 44 are exactly the 44 that fail; the 9 that compile have
   none (`ResetCounterValues`, `CopyForBVHUpdate`, `ResetToIndexBuffer`, `CopyCounters`, `RadixReset`, `PT_ConstructBVH4`, `ResetKDOP`,
   `DecompressHeader`, `CopyAndCompactBVH`). The intrinsics are one-to-one wrappers of RDNA instructions: `global_load_dword/x2/x3/x4_with_cache`,
   `global_store_*`, `global_atomic_{add,sub,fmin,fmax}` and `s_load_dword_with_cache` (all with a 64-bit address operand), `Med3/Min3/Max3`
   (float and unsigned), `BFI`, `V_ALIGNBYTE_B32`, `V_CNDMASK_B32`, `V_PK_MIN/MAX_F16`, and lane operations (`ReadLane`, `LaneSwizzle`,
   `Ballot64`, `MBCNT64`, `S_BCNT1_U64`, `GetLaneID`, `WaveMin_U32`, `MakeUniform`, `TypedLoad`). 33 distinct names (the first `i32`
   argument looks like an instruction number, e.g. 356 for the dword global load). `tools/xbox/platform/bvh_shader_scan.py` classifies the
   shaders, `docs/xbox/bvh_builder_shaders.json` has the per-shader result:
   | group | shaders |
   |---|---:|
   | standard DXIL only (compile) | 9 |
   | needs raw 64-bit address access (global load/store/atomic) | 31 |
   | wave/lane intrinsics, no pointers (RadixCount, PrefixSum*, RadixAlloc) | 9 |
   | arithmetic intrinsics only (ConstructBVH4, BuildApexPointMap, Box16/32Rehydration) | 4 |
   So a PC path needs the 13 pointer-free shaders' intrinsics lowered to standard DXIL (all have direct equivalents: wave ops, min/max, bitfield
   insert, funnel shift), and for the 31 others an address-to-resource mapping for the 64-bit addresses, which the Xbox2Windows-style
   translation or our own RDNA-aware path would have to provide. The ISA semantics of these intrinsics are the ones the SPIR-V recompiler
   already implements for the same instructions.
**Lowering the intrinsics (tools/xbox/platform/xbox_dxil_lower.py + dxasm.cpp)**: `xbox_dxil_lower.py` rewrites the disassembled DXIL
(`dxc -dumpbin`) so it calls standard operations instead, and `dxasm.exe` reassembles it with DXC's assembler API and signs it with
`dxil.dll`. Needed besides the replacements: the Xbox target triple `dxil-ms-xb2` and the `xbox.compiler.options` metadata are rejected by the
standard validator; the Xbox validator allows `{ half, half }` aggregates, standard DXIL does not (every pair value is split into two scalars);
and unnamed SSA values and blocks must be renumbered after instructions are removed (number 0 is the entry block).
Done so far: `V_PK_MIN_F16` / `V_PK_MAX_F16` -> `dx.op.binary.f16` on each half, `Med3_F32` -> `max(min(a,b), min(max(a,b),c))`.
That covers the four arithmetic-only shaders, and on the RX 7800 XT (one shader per process, original / lowered):
| shader | original | lowered |
|---|---|---|
| Box16Rehydration | compiler crash | compiles |
| Box32Rehydration | compiler crash | compiles |
| ConstructBVH4 | compiler crash | compiles |
| BuildApexPointMap | error | compiles |
"Compiles" means the driver accepted the pipeline; nothing has been dispatched, so the lowered shaders are not yet checked for correct results.
**First dispatch of a lowered shader (tools/xbox/platform/dispatch_test.cpp)**: Box32Rehydration (lowered) was run with pseudo-random input
(words whose high byte is a count 0..4 and whose low 24 bits are a random mask), on the RX 7800 XT and on WARP, through bounds-checked raw UAVs.
- It runs to completion. On the GPU the output is identical across repeated runs for 1 and 4 groups, and identical whether the wave size is left to the driver or
  pinned to 64 (`dxil_sm66_wave.py`: SM 6.6 `[WaveSize(64)]`; it also converts `createHandle` to `createHandleFromBinding`/`annotateHandle`, which SM 6.6 requires).
  With 64 groups the output differs run to run: random pointers make several threads write the same node, a race in my input, not in the shader.
- WARP gives a different result (6429 vs 6552 non-zero words for 4 groups), and WARP rejects SM 6.6, so it cannot be used to check the pinned version.
- One earlier run hung the GPU (device removed). It followed runs that used root UAVs, which have no bounds checking, so I attribute it to that and not to the shader;
  it did not recur once the views were bounds-checked.
- **The lowered operations themselves are checked.** `tools/xbox/platform/oplower_kernel.ll` is a small Xbox-style kernel that applies `Med3_F32`, `V_PK_MIN_F16` and `V_PK_MAX_F16` to
  random bit patterns; it goes through `xbox_dxil_lower.py` and `dxasm`, runs with `dispatch_test` (`DT_RAW=1 DT_DUMP=<prefix>`), and `oplower_check.py` compares every result with a
  software model (min/max return the other operand when one is NaN; sign of zero and denormals are not distinguished). On 262,144 cases the RX 7800 XT and WARP both match exactly (0 mismatches, NaN and infinity included).
  That is the whole of what the lowering changed in the four arithmetic-only shaders, so what remains unchecked is the shaders' own logic, which no reference exists for.
- This shows the lowered shader is stable, not that it is correct: the original cannot run anywhere, so there is no reference output.
  A CPU model of the rehydration (from the shader text and `BVH_CONTAINER.md`) is the next way to check it.

**More intrinsics lowered; 22 of the 53 builder shaders now compile on the GPU (`lower_all.py`)**: `lower_all.py <dir with shader_NN.ll> <out_dir>` lowers, pins the wave
size to 64 where wave operations are used (`dxil_sm66_wave.py`), assembles, signs and repairs the shader-flags word. Added to the table: lane id, readfirstlane, readlane,
`ds_swizzle` (bitmask and quad modes), ballot, `v_cndmask_b32`, `mbcnt`, `s_bcnt1`, min3/max3/med3 (f32 and u32), `v_bfi`, `v_alignbyte`, `WaveMin_U32`, 64-bit `MakeUniform`.
All 22 (the 13 pointer-free shaders of the earlier count plus the nine wave/lane ones) are accepted as pipelines by the RX 7800 XT.
The lane intrinsics were run (`wavelower_kernel.ll` + `wavelower_check.py`, wave size 64, 64 groups x 64 lanes) and match a software model with 0 mismatches;
the model uses the same formulas as the lowering, so this checks the plumbing. The one argument order that is inferred and not documented is `V_CNDMASK_B32(mask pair, {src0, src1})`:
it is what the Hillis-Steele prefix sums in these shaders (masks `0xAAAAAAAA`/`0xCCCCCCCC`/`0xF0F0F0F0` with swizzle offsets 30/60/120) require.
The other 31 shaders use `global_load/store/atomic` and `s_load` on 64-bit addresses (plus one `TypedLoad`). Those addresses are not derived inside the shader:
they are 64-bit fields read from buffers (for example `rawBufferLoad.i64` at offset 24 of an input record), so they are GPU virtual addresses that the game obtained from the device.
**Address lowering (implemented)**: the proxy records each resource's (virtual address, size, heap slot) when it is created. The lowered shader finds the
entry with a branchless binary search over a sorted 16-entry table of (u64 base, u64 size, u32 slot) and accesses through `dx.op.createHandleFromHeap`
(SM 6.6 `ResourceDescriptorHeap`) at `address - base`; slot 0 holds the table, slot 1 a dummy for addresses no entry covers, 2 and up the resources.
`xbox_dxil_lower.py` lowers all of it: multi-dword loads and stores become one access per dword (a multi-component raw load through a heap handle crashes
WARP), `global_atomic_sub` is an add of the negated value, and `global_atomic_fmin/fmax`, which DXIL has no float atomic for, use `UMin`/`UMax` on the
order-preserving key `bits ^ (bits >> 31 | 0x80000000)` (correct for every non-NaN value; a NaN in memory is replaced instead of ignored). All 52 container
blobs now lower, assemble and sign; `addr_kernel.ll` + `addrmap_test.cpp` (run by `addr_test.py`) exercise every lowered access against a CPU model on an
RTX 5080 and on WARP with 0 mismatches. Still needed to dispatch the real shaders: build the table and descriptor heap from the resources the builder
creates, and create the pipeline with a root signature that carries `CBV_SRV_UAV_HEAP_DIRECTLY_INDEXED` (the builder's own signatures do not).
Raw buffers are limited to 4 GiB per view.
**PSO creation with the lowered blobs (`bvh_test.cpp`)**: the builder creates its 17 root signatures through device slot 16 directly
from the containers' `RTS0`, so the `d3d12_xs` shim never sees them; with `XS_HEAP=1` `bvh_test` rewrites each one (v1.0 to v1.1, flags
plus `CBV_SRV_UAV_HEAP_DIRECTLY_INDEXED`) on the device proxy. With `BVH_ONLY`/`BVH_REPLACE_DIR` and the blobs mapped by pipeline index
(53 dumps from `BVH_DUMP_DIR`, lowered and re-signed) **53 of 53 pipelines are created with each shader's own signature, 0 compiler
crashes, 0 other failures** on the RTX 5080. Without the mapping the replaced shaders do not match their signature (31-39 failures), so
the order matters: `BVH_DUMP_DIR` first, then lower and replace by index.
**A build call is reached but not finished**: `BVH_BUILD=1` calls `BuildRaytracingAccelerationStructure` for a three-vertex triangle with
the Xbox device proxy (slot 76 resource creation, wrapped heaps). Prebuild answers, but the builder's worker thread then takes an access
violation before any dispatch. Open: the descriptor heap and table the lowered shaders read (slot 0 table, 1 dummy), unwrapping the
proxy objects in the command-list calls, and the pass wiring. Prior art for the lookup: Xbox2Windows (`src/title_va_lookup.hlsli`,
`era_title_va.cpp`) has the same sorted-range search with alignment and whole-access checks, and makes the title's pointers valid GPU
addresses with `OpenExistingHeapFromAddress` instead of copying, so the game's 64-bit addresses need no rewriting.

**`TypedLoad.i32` and LeafRehydration (blob 48) on NVIDIA**: blob 48 is the only `TypedLoad.i32` user, with two calls
`TypedLoad.i32(i32 138, handle u0 = compressedBVH (RWByteAddressBuffer), i32 offset, i32 65)`; only component 0 of the `ResRet.i32` is used.
- Format 65 read as the GFX6-9 MTBUF encoding `dfmt | nfmt << 4` = `DFMT_8 | NFMT_UINT << 4` (LLVM `llvm/lib/Target/AMDGPU/SIDefines.h`, `MTBUFFormat`),
  an 8-bit unsigned load. Inferred, not documented: in the GFX10 unified table (`UfmtGFX10`) 65 is `16_16_16_16_UNORM`, a float format that the
  shader's use does not fit. Checked against the shader: the offsets are byte offsets (`base + 3 * count + i`, right after a packed array of
  24-bit indices that the shader itself reads with `rawBufferLoad.i64` at `offset & ~3`), and the results are used only as bytes
  (`(index24 << 8) | b`; `(b & 0xF) | ((b << 4) & 0xF00)`).
- Lowered (`xbox_dxil_lower.py`) to `rawBufferLoad.i32` at `offset & ~3`, shifted right by `8 * (offset & 3)`, `& 0xFF`. A byte in the last,
  partial dword of a buffer whose size is not a multiple of 4 reads as 0 (the dword is out of bounds); the shader's own unaligned loads do the same.
  `typedload_kernel.ll` + `typedload_check.py` (4096 threads, offsets `t` and `5t + 3` over random bytes): 0 mismatches on the RTX 5090 and on WARP.
- Blob 48 still calls `global_load_dword{,x2,x3,x4}` (64-bit addresses: index and vertex buffers read from the `D3D12_RAYTRACING_GEOMETRY_TRIANGLES_DESC`
  records), so `dxil.dll` does not validate it. `dxasm --hash-only` assembles it without validation and writes the container hash: the retail
  hash of DXC's `lib/DxilHash/DxilHash.cpp` (`ComputeHashRetail`, MD5 rounds with the bit count in the first dword and `1 | size << 1` in the last
  dword of the final block), reimplemented in `dxil_hash.h`; it reproduces the hash of all 53 original blobs and of `dxil.dll`-signed ones.
  The PC runtime rejects a container with a zeroed hash (`E_INVALIDARG`).
- RTX 5090, driver 617.42, `BVH_ONLY=48`: the original blob, and the same text reassembled unchanged, make the driver's compiler run out of memory
  (private bytes seen at 24 and 51 GB of a 65 GB commit limit; `E_OUTOFMEMORY` after about 90 s, or the fail-fast `0xC0000409`).
  With only the two `TypedLoad.i32` calls lowered (everything else, triple included, as in the original) the pipeline is created in under a second
  (about 100 MB), and also with the whole `xbox_dxil_lower.py` output (`V_ALIGNBYTE_B32` lowered too); replacing the two calls with an aligned dword
  load at offset 0 has the same effect, so the two typed byte loads are what the driver cannot handle.
- Full constructor with only blob 48 replaced: 53 of 53 pipelines, no failures, prebuild 204800/128704, destructor ok (3 runs).
  This is pipeline creation only: the `global_load_*` calls are still Xbox intrinsics, so blob 48 cannot run until the 64-bit address mapping exists.
- Not caused by this: with the 22 `lower_all.py` replacements, the 11 that pin the wave size to 64 (20 to 28, 49, 50) are rejected with
  `E_INVALIDARG` on the RTX 5090, which has no 64-lane waves; the 11 others are accepted.
Not reached: any build or rehydrate call. They take GPU virtual addresses, which the builder's DXIL shaders read by 64-bit address (no PC
D3D12 equivalent), and real inputs would also need the matching vertex/index buffers (BVH_CONTAINER.md).

## FidelityFX upscaler, unmodified, on a PC device (tools/xbox/platform/ffx_test.cpp)
The package's own `amd_fidelityfx_loader_xbox.dll` and `amd_fidelityfx_upscaler_xbox.dll` run as they are (they are ordinary x64 PE files;
nothing is recompiled), next to the stand-in `xmem.dll` / `d3d12_xs.dll`:
* `ffxQuery` (version enumeration, null context): one upscaler, id `0xf5a5ca1e01001001`, name `4.1.1`.
* `ffxCreateContext` for the upscale effect (type `0x10000`, backend extension type `0x01000002` carrying the device), 1280x720 to 1920x1080,
  returns 0 and `ffxDestroyContext` returns 0, on the RX 7800 XT and on WARP, debug layer: 0 errors, 3 warnings (buffers ignore the initial state).
* The create flags must be `0x29`: the descriptor validator (RVA `0xbb10`) fails with 6 unless bits 0x1, 0x8 and 0x20 are set and rejects
  0x2, 0x4, 0x40, 0x100 and 0x200 as unsupported.
* The provider creates 14 resources: 10 default-heap ones through device slot 76 (1920x1080 RGBA8, two RGBA16F, one RGB9E5, an 8-byte
  buffer, 1x1 RG32F, 1x1 R32_UINT, 80x45 R32F, a 21,045,760-byte buffer, a 131,072-byte buffer) and 4 upload buffers through slot 27.
Where the Xbox device differs from the PC one, as found by running it:
| Xbox call | PC | handling |
|---|---|---|
| device slot 76 (resource creation, heap properties / heap flags / description / state / clear value / iid / output) | slot 27 is CreateCommittedResource; slot 76 is something else | forwarded to CreateCommittedResource; the description is 56 bytes plus extension words and its layout field is `0x101` (tiled) or `0x1` (row-major buffer), replaced by driver-chosen layout |
| descriptor heap `GetCPU/GPUDescriptorHandleForHeapStart` (slots 9, 10) | returns through a hidden pointer in RDX | the Xbox build returns the 8-byte handle in RAX; heaps are wrapped so the call is made the PC way |
| slots 1, 15, 18, 19, 27, 38 on the device | same | forwarded unchanged (AddRef, descriptor increment size, SRV, UAV, committed resource for uploads, copyable footprints); 18 and 19 see the RGB9E5 note below |
**NVIDIA**: the fourth slot-76 resource is a 1920x1080 `R9G9B9E5_SHAREDEXP` texture with `ALLOW_UNORDERED_ACCESS`. The RTX 5090 (driver 617.42)
does not report `TYPED_UNORDERED_ACCESS_VIEW` for that format (WARP does), so `CreateCommittedResource` returns `E_INVALIDARG` and the
upscaler throws a C++ exception out of `ffxCreateContext`. `xbox_device_overrides.h` creates such a resource as `R16G16B16A16_FLOAT` when
the device lacks the support (every RGB9E5 value is exact in f16; writes are not rounded to RGB9E5 and the memory layout differs) and
rewrites RGB9E5 views of it in slots 18/19; `XS_NO_FORMAT_FALLBACK=1` turns it off. With it, flags `0x29` create and destroy return 0 on
the RTX 5090; the upscaler builds its views of that resource from the resource's own format (f16), so the view rewrite is not reached
here. Nothing was dispatched, so whether the upscaler's output is unchanged by the substitution is not checked.
The first-chance `0xe06d7363` that `ffx_test` prints during `D3D12CreateDevice` also appears on WARP and is caught inside `D3D12Core`.
This is the same finding the Xbox2Windows project reports (slot 76, layout `0x101`), reached here by running the code instead of translating it.
No dispatch was run: it needs real textures, a command list and the package's shaders.

**Shader containers**: the upscaler embeds 29 compute containers whose chunk table is `SFI0/ISG1/OSG1/PSV0/RTS0/HASH/ACCC` and has no
`DXIL` part, so the code is precompiled RDNA2 and the provider creates the pipeline states from them through a deserialization callback,
with a separate backend as fallback when a container has no usable `ACCC`. Each payload holds one program (1,184-17,760 bytes, one
`s_endpgm`-shaped word). Described, not decoded: `R0Y00/Xbox2Windows` (MIT) decodes these streams with LLVM's gfx1030/gfx1013 decoders
and runs passes 24-26 and RCAS on a PC GPU, and its pinned `XboxBvhBuilder.dll` hash is the one in this package. The title's own shaders
do not use this container (XBOX_SHADERS.md); plans for it are in FSR4_ACCC_PLAN.md. The loader also references
`amd_fidelityfx_framegeneration_xbox.dll`, which this package does not ship.

`xbox_device_proxy.h` is the reusable part: an object whose vtable forwards every slot to the PC object by generated thunks (with per-slot hit
counters, `PrintHits`) and lets selected slots be replaced. It is the starting point for the "D3D12.X runtime" row.

## Compared with the AnyXboxX stand-ins (read only; that repository has no licence, nothing was copied)
- `XMemVirtualQuery`: AnyXboxX assumes a 3-argument `VirtualQuery(address, info, size)`. The call sites in XboxBvhBuilder pass (process, address, out) with a zeroed 64-byte
  struct, which is what `xmem.cpp` here implements; the first call there would be wrong with the other signature.
- `XMemGetWorkingSetStatistics`: they return FALSE (no data). The FidelityFX upscaler reads a size at +8 of the struct and maps a zero return to "no data"; ours returns plausible values (untested whether the upscaler prefers them).
- `XMemAlloc`: they align to 64 bytes or a page and track blocks so that allocation hooks (`XMemSetAllocationHooks`) can coexist; ours uses `VirtualAlloc` (64 KiB granularity, simplest, wasteful for many small blocks). Hooks are not imported by the package's DLLs, so not needed yet.
- `d3d12_xs`: they forward four root-signature functions unchanged. Unchanged forwarding fails for Xbox root signatures (the PC serializer rejects the high flag byte; see above), so ours clears it on a copy.
  Added here: `D3D12SerializeRootSignature` (v1.0 layout, flags at +32; a descriptor with flags 0x3000001 serializes through the shim and fails with E_INVALIDARG on the PC runtime) and `D3D12CreateVersionedRootSignatureDeserializer`.
- They have no D3D12.X device layer (listed as to do), no shader recompiler, and no BVH or upscaler tests; they have a much larger `xgameruntime` (152 of 396 APIs) and a kernel redirect (`kernelx.dll`).

## D3D12XboxCreateDevice (first graphics call of a title)
`d3d12_xs_device.cpp` exports it: `(adapter, params, riid, out)` returns a PC `ID3D12Device` behind the Xbox-ABI proxy, with slot 14 (descriptor heaps, whose start handles come back in RAX) and slot 76
(resource creation with the Xbox texture layout) overridden. Checked with a client that does what the package DLLs do: create the device, create a descriptor heap through slot 14, read the heap start through slot 9/10 (non-zero 8-byte values in RAX).
The signature and the `params` struct are from memory of the public documentation, not from a header: the struct is only logged (24 words), so a wrong layout cannot break creation, but its contents (command-queue ring size, scratch memory, debug flags) are unused.
Not covered: presentation (`PresentX` on the queue, plane parameters), the command-list extensions, queue and fence creation (their slot numbers on the Xbox device are unknown beyond the ones listed above); `XS_TRACE=1` lists what a title calls.

