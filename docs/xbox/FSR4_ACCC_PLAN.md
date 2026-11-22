# Plan: the FSR4 upscaler's ACO containers (`ACCC`) - prior art and what is left

Status: option A done on 2026-11-22 (see the shader-container paragraph in PLATFORM_SURFACE.md). Option B is deferred until a second
title ships ACO-cooked containers; option C is rejected. No package file is copied into the repository.

## Question

`amd_fidelityfx_upscaler_xbox.dll` in the 1.0.2.1 package embeds 29 compute containers whose DXBC chunk
table has `SFI0/ISG1/OSG1/PSV0/RTS0/HASH/ACCC` and no `DXIL` part. The DLL turns them into pipeline states
through a callback table (`DeserializeComputePipelineStateX`) and falls back to "NewBE" when a container has
no usable `ACCC` part. Is describing that container, or reusing its code, worth work in this repository?

## What we observed locally (counts only)

- 16 DLLs in the package root; static imports already classified (492 standard / 113 package / 6 shim,
  0 unresolved), so the platform surface in PLATFORM_SURFACE.md already covers this exact set.
- 29 of the upscaler's containers carry `ACCC`. Each payload is 1,184-17,760 bytes, starts with four header
  dwords (the second is the code size), and contains exactly one `s_endpgm`-shaped word. That is consistent
  with one compiled program per container, not with a bitcode or metadata blob.
- The game's own `shaders.rpf` contains no `ACCC`: its shaders are DXBC with a stub `DXIL` (`NOBC`) and the
  code in `XDAT`/`XBCH` -> `FSSM` (XBOX_SHADERS.md). So this container is AMD's, not the title's.

## Prior art

Search scope: this repository (docs, tools, git log, branches; the GitHub issue tracker is disabled), then
public sources for `ACCC` + Xbox/FSR4/ACO.

- In this repository: nothing. PLATFORM_SURFACE.md stops at `ffxQuery`/`ffxCreateContext`/`ffxDestroyContext`
  and the resources the provider creates; `ACCC`, `ACO` and FSR4 appear nowhere.
- `R0Y00/Xbox2Windows` (MIT) already covers it, against the same pinned files:
  - `docs/experiments.md`, section "shader containers / deserializers": 29 structurally complete containers,
    each with a pointer-table entry, `RTS0` and `ACCC`, none with DXIL. Their container RVAs (`0x701b0`,
    `0x714f0`) are our file offsets plus the image-base delta, i.e. the same build.
  - `tools/ffx_dispatch_profile.py` and `tools/ffx_dispatch_aot.py` decode the native streams with LLVM 15
    (gfx1030 for the i8 passes, gfx1013 for RCAS) and recompile them to desktop DXIL/HLSL; pass 24, 25, 26
    and RCAS were executed on an RTX 4060 Laptop and on WARP, compared against CPU references.
  - Their declared open items: the full-wave LDS branch of pass 26, the rest of the 27-pass graph, and the
    provider's teardown with a live graphics context.
  - Their `XboxBvhBuilder.dll` SHA-256 is `99f803ac...23707e`, identical to the local file, so both projects
    use one package build.
- Nothing else surfaced: a general search for `ACCC`/ACO/Xbox finds no other write-up.

## Gap assessment

- The payload is native RDNA2 code, which our `tools/xbox` decoder could disassemble and the recompiler could
  in principle turn into SPIR-V. That is exactly what Xbox2Windows did in a different way, so doing it again
  would duplicate a finished, tested effort.
- There is no DXIL in these containers, so nothing here feeds the AnyPS5 recompiler path used for the title's
  shaders. The title's own shaders do not use `ACCC` at all.
- If the game needs FSR4 on a PC build, AMD's PC FSR4 provider is the supported route; reimplementing the
  Xbox containers is not on any path we have.

## Options

| Option | Work | Value | Risk |
|---|---|---|---|
| A. Record and stop (recommended) | 3-4 lines in PLATFORM_SURFACE.md: containers have `RTS0`+`ACCC` and no DXIL, pointer-table deserialization, no `framegeneration` module in the package, prior art cited | Keeps the platform surface complete; stops the next reader from redoing it | None |
| B. Independent container note | Parse the 29 containers, describe chunk table, `ACCC` header and pointer table in our own words; disassemble one small program with our own decoder only to confirm it is the same RDNA2 ISA the recompiler eats. Output: a short section in PLATFORM_SURFACE.md plus a 1-file script in `tools/xbox/platform/`; counts only, no listings | Removes our reliance on an external document for a format that may reappear in other Scarlett titles | Low; keep it descriptive, no payloads in the repo |
| C. Port the FSR4 graph to Vulkan through our recompiler | Days to weeks; 27 passes, model weights, resource graph, exposure | None for the title, which ships its own FSR4 and can use the PC build | High, duplicates Xbox2Windows |

## Decision (2026-11-22)

A taken; the paragraph is in PLATFORM_SURFACE.md. B stays as written below if a second title ships ACO-cooked containers. C rejected.

If a second title needs B, the steps are:

1. `tools/xbox/platform/accc_scan.py <dll>`: walk the chunk table of each container, print per-container
   `ACCC` length, the four header dwords, pointer-table entry, and the `s_endpgm` count. Counts only.
2. Run it on the local copy; record the ranges and check that the pointer table at the deserializer site
   selects them (static call-site read, same method as `bvh_shader_scan.py`).
3. Decode one 1.2 KB program with the existing RDNA2 decoder to confirm opcode coverage; report the share of
   undecoded words, not the words.
4. Write the section in `docs/xbox/PLATFORM_SURFACE.md` and, if the scanner is kept, register it in
   `tools/xbox/platform/README.md`. Cite Xbox2Windows as the source of the executed results.

## Guardrails

- CONTRIBUTING.md: no DLLs, no shader data, no DXIL/ISA/disassembly listings, no tables extracted from game
  or shader data, nothing over 500 KB. Counts, error classes and a one-line symptom are fine.
- Xbox2Windows is MIT, this repository GPL-2.0-only. Reading it is fine; if any code is ever adapted, keep the
  MIT notice and attribution, and prefer our own implementation from the observations.
- Everything runs on the local copy under `E:`; nothing from the package enters git.
