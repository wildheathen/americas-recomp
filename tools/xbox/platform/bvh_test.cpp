// Loads XboxBvhBuilder.dll next to the stand-in xmem.dll / d3d12_xs.dll and drives its C++ class XboxBvhBuilder on a PC D3D12 device.
// Stage 1 (this file): construct it (the constructor creates the builder's root signatures and compute pipelines on the device), then
// ask for prebuild sizes of a small triangle geometry and compare with the PC driver's own answer. Prints what succeeds, what fails and
// every D3D12 debug-layer message, so the first failing step is visible. Nothing here is from the game except the DLL.
// build: cl /nologo /EHsc /O2 /std:c++17 bvh_test.cpp d3d12.lib dxgi.lib /Fe:bvh_test.exe     usage: bvh_test [warp] [nodebug] [64bit-morton]
#include <windows.h>
#include <d3d12.h>
#include <dxgi1_6.h>
#include <wrl/client.h>
#include <cstdio>
#include <cstdint>
#include <cstring>
#include <exception>
#include <cstdlib>
#include <vector>
#include <csignal>
#include <fstream>
#include <iterator>
using Microsoft::WRL::ComPtr;
#include "xbox_device_proxy.h"
#include "xbox_device_overrides.h"

// Xbox device vtable slot 62: the builder's pipeline creation. Called as (desc, 0, 0, riid, ppv) with a D3D12_COMPUTE_PIPELINE_STATE_DESC
// (root signature, shader bytecode, node mask, cached pso, flags). Treated here as the PC CreateComputePipelineState; the two zero
// arguments are not understood (logged if nonzero).
#include "bvh_dummy_cs.h"      // g_dummy: an empty compute shader, used in place of a blob the PC compiler cannot take
static int g_pipe = 0, g_crashed = 0, g_failed = 0;
static const char* g_dump = nullptr;     // BVH_DUMP_DIR: write every shader blob handed to the device
static bool Trace() { static const bool on = getenv("BVH_TRACE") != nullptr; return on; }

// BVH_TRACE=1: log the overridden device slots and the command-list calls, so a fault in a builder thread can be located.
static std::vector<void*> g_heapProxies;
static std::vector<ID3D12RootSignature*> g_signatures;   // created from RTS0, in SlotRootSignature order
static ID3D12DescriptorHeap* UnwrapHeap(ID3D12DescriptorHeap* h) {
    for (void* p : g_heapProxies) if (p == (void*)h) return (ID3D12DescriptorHeap*)((XboxObjectProxy*)p)->real;
    return h;
}
static HRESULT __fastcall Trace14(XboxObjectProxy* self, const D3D12_DESCRIPTOR_HEAP_DESC* d, REFIID iid, void** out) {
    HRESULT hr = ((ID3D12Device*)self->real)->CreateDescriptorHeap(d, iid, out);
    if (SUCCEEDED(hr) && out && *out && iid == __uuidof(ID3D12DescriptorHeap)) {
        auto* h = XboxObjectProxy::Create((IUnknown*)*out, "descriptor heap");
        h->Override(9, (void*)xbox_overrides::HeapCpuStart);
        h->Override(10, (void*)xbox_overrides::HeapGpuStart);
        g_heapProxies.push_back(h);
        *out = h;
    }
    if (Trace()) fprintf(stderr, "T device14: heap type %d count %u -> 0x%08lx\n", d ? (int)d->Type : -1, d ? d->NumDescriptors : 0, (unsigned long)hr);
    return hr;
}
static HRESULT __fastcall Trace76(XboxObjectProxy* self, const D3D12_HEAP_PROPERTIES* hp, UINT heapFlags, const unsigned char* desc, UINT state, const D3D12_CLEAR_VALUE* cv, REFIID iid, void** out) {
    HRESULT hr = xbox_overrides::Slot76(self, hp, heapFlags, desc, state, cv, iid, out);
    if (Trace()) {
        const D3D12_RESOURCE_DESC* d = (const D3D12_RESOURCE_DESC*)desc;
        fprintf(stderr, "T device76: dimension %u %llux%u format %u state %u -> 0x%08lx\n", d ? (unsigned)d->Dimension : 0,
                d ? (unsigned long long)d->Width : 0, d ? (unsigned)d->Height : 0, d ? (unsigned)d->Format : 0, state, (unsigned long)hr);
    }
    return hr;
}
static void __fastcall Trace18(XboxObjectProxy* self, ID3D12Resource* r, const D3D12_SHADER_RESOURCE_VIEW_DESC* d, SIZE_T h) {
    if (Trace()) fprintf(stderr, "T device18: srv resource %p -> handle %llx\n", (void*)r, (unsigned long long)h);
    xbox_overrides::Slot18(self, r, d, h);
}
static void __fastcall Trace19(XboxObjectProxy* self, ID3D12Resource* r, ID3D12Resource* c, const D3D12_UNORDERED_ACCESS_VIEW_DESC* d, SIZE_T h) {
    if (Trace()) fprintf(stderr, "T device19: uav resource %p -> handle %llx\n", (void*)r, (unsigned long long)h);
    xbox_overrides::Slot19(self, r, c, d, h);
}
static void __fastcall ClDispatch(XboxObjectProxy* self, UINT x, UINT y, UINT z) {
    if (Trace()) fprintf(stderr, "T cl Dispatch %u %u %u\n", x, y, z);
    ((ID3D12GraphicsCommandList*)self->real)->Dispatch(x, y, z);
}
static void __fastcall ClSetPso(XboxObjectProxy* self, ID3D12PipelineState* pso) {
    if (Trace()) fprintf(stderr, "T cl SetPipelineState %p\n", (void*)pso);
    ((ID3D12GraphicsCommandList*)self->real)->SetPipelineState(pso);
}
static void __fastcall ClBarrier(XboxObjectProxy* self, UINT n, const D3D12_RESOURCE_BARRIER* b) {
    if (Trace()) fprintf(stderr, "T cl ResourceBarrier %u\n", n);
    ((ID3D12GraphicsCommandList*)self->real)->ResourceBarrier(n, b);
}
static void __fastcall ClSetHeaps(XboxObjectProxy* self, UINT n, ID3D12DescriptorHeap* const* heaps) {
    std::vector<ID3D12DescriptorHeap*> real(n);
    for (UINT i = 0; i < n && heaps; i++) real[i] = UnwrapHeap(heaps[i]);
    if (Trace()) fprintf(stderr, "T cl SetDescriptorHeaps %u (%p -> %p)\n", n, n && heaps ? (void*)heaps[0] : nullptr, n ? (void*)real[0] : nullptr);
    ((ID3D12GraphicsCommandList*)self->real)->SetDescriptorHeaps(n, real.data());
}
static void __fastcall ClSetRootSig(XboxObjectProxy* self, ID3D12RootSignature* sig) {
    if (Trace()) fprintf(stderr, "T cl SetComputeRootSignature %p\n", (void*)sig);
    ((ID3D12GraphicsCommandList*)self->real)->SetComputeRootSignature(sig);
}
static void __fastcall ClSetTable(XboxObjectProxy* self, UINT i, D3D12_GPU_DESCRIPTOR_HANDLE h) {
    if (Trace()) fprintf(stderr, "T cl SetComputeRootDescriptorTable %u %llx\n", i, (unsigned long long)h.ptr);
    ((ID3D12GraphicsCommandList*)self->real)->SetComputeRootDescriptorTable(i, h);
}
static void __fastcall ClCopyBuffer(XboxObjectProxy* self, ID3D12Resource* d, UINT64 doff, ID3D12Resource* s, UINT64 soff, UINT64 n) {
    if (Trace()) fprintf(stderr, "T cl CopyBufferRegion %llu+%llu <- %llu\n", (unsigned long long)doff, (unsigned long long)n, (unsigned long long)soff);
    ((ID3D12GraphicsCommandList*)self->real)->CopyBufferRegion(d, doff, s, soff, n);
}
// Xbox command-list extension used by the BVH builder (slot 148): reserve <bytes> of command stream, <align> aligned, and return the
// pointer to fill. The PC command list has no equivalent; the builder writes its own 40-byte records into it. A plain allocation lets it
// continue so the next Xbox-only call becomes visible.
static void* __fastcall ClReserve(XboxObjectProxy* self, unsigned bytes, unsigned align) {
    (void)self; (void)align;
    void* p = VirtualAlloc(nullptr, bytes ? bytes : 1, MEM_COMMIT | MEM_RESERVE, PAGE_READWRITE);
    if (Trace()) fprintf(stderr, "T cl slot148 reserve %u align %u -> %p\n", bytes, align, p);
    return p;
}
static void __fastcall ClSet32(XboxObjectProxy* self, UINT rootIndex, UINT value, UINT offset) {
    if (Trace()) fprintf(stderr, "T cl SetComputeRoot32BitConstant root %u value %u offset %u\n", rootIndex, value, offset);
    ((ID3D12GraphicsCommandList*)self->real)->SetComputeRoot32BitConstant(rootIndex, value, offset);
}
static ComPtr<ID3D12RootSignature> g_heapSig;
static ID3D12RootSignature* HeapSignature(ID3D12Device* dev);   // generic signature for replacement pipelines
// The builder creates 17 root signatures directly from the containers' RTS0 blobs through device slot 16, so the d3d12_xs shim never sees
// them. With XS_HEAP=1, rebuild each through a v1.0 -> v1.1 conversion and add CBV_SRV_UAV_HEAP_DIRECTLY_INDEXED, keeping the layout.
static HRESULT __fastcall SlotRootSignature(XboxObjectProxy* self, UINT nodeMask, const void* blob, SIZE_T size, REFIID riid, void** out) {
    auto real = (ID3D12Device*)self->real;
    if (!getenv("XS_HEAP")) return real->CreateRootSignature(nodeMask, blob, size, riid, out);
    ComPtr<ID3D12RootSignatureDeserializer> des;
    if (FAILED(D3D12CreateRootSignatureDeserializer(blob, size, IID_PPV_ARGS(&des))))
        return real->CreateRootSignature(nodeMask, blob, size, riid, out);
    const D3D12_ROOT_SIGNATURE_DESC* d = des->GetRootSignatureDesc();
    std::vector<D3D12_ROOT_PARAMETER1> params(d->NumParameters);
    std::vector<std::vector<D3D12_DESCRIPTOR_RANGE1>> ranges(d->NumParameters);
    for (UINT i = 0; i < d->NumParameters; i++) {
        const D3D12_ROOT_PARAMETER& s = d->pParameters[i];
        D3D12_ROOT_PARAMETER1& t = params[i];
        t.ParameterType = s.ParameterType; t.ShaderVisibility = s.ShaderVisibility;
        if (s.ParameterType == D3D12_ROOT_PARAMETER_TYPE_DESCRIPTOR_TABLE) {
            ranges[i].resize(s.DescriptorTable.NumDescriptorRanges);
            for (UINT j = 0; j < s.DescriptorTable.NumDescriptorRanges; j++) {
                const D3D12_DESCRIPTOR_RANGE& r = s.DescriptorTable.pDescriptorRanges[j];
                D3D12_DESCRIPTOR_RANGE1& u = ranges[i][j];
                u.RangeType = r.RangeType; u.NumDescriptors = r.NumDescriptors; u.BaseShaderRegister = r.BaseShaderRegister;
                u.RegisterSpace = r.RegisterSpace; u.OffsetInDescriptorsFromTableStart = r.OffsetInDescriptorsFromTableStart;
                u.Flags = D3D12_DESCRIPTOR_RANGE_FLAG_NONE;
            }
            t.DescriptorTable.NumDescriptorRanges = s.DescriptorTable.NumDescriptorRanges;
            t.DescriptorTable.pDescriptorRanges = ranges[i].data();
        } else if (s.ParameterType == D3D12_ROOT_PARAMETER_TYPE_CBV || s.ParameterType == D3D12_ROOT_PARAMETER_TYPE_SRV ||
                   s.ParameterType == D3D12_ROOT_PARAMETER_TYPE_UAV) {
            t.Descriptor.ShaderRegister = s.Descriptor.ShaderRegister;
            t.Descriptor.RegisterSpace = s.Descriptor.RegisterSpace;
            t.Descriptor.Flags = D3D12_ROOT_DESCRIPTOR_FLAG_NONE;
        } else {
            t.Constants = s.Constants;
        }
    }
    D3D12_VERSIONED_ROOT_SIGNATURE_DESC v = {};
    v.Version = D3D_ROOT_SIGNATURE_VERSION_1_1;
    v.Desc_1_1.NumParameters = d->NumParameters;
    v.Desc_1_1.pParameters = params.data();
    v.Desc_1_1.NumStaticSamplers = d->NumStaticSamplers;
    v.Desc_1_1.pStaticSamplers = d->pStaticSamplers;
    v.Desc_1_1.Flags = d->Flags | D3D12_ROOT_SIGNATURE_FLAG_CBV_SRV_UAV_HEAP_DIRECTLY_INDEXED;
    ComPtr<ID3DBlob> sb, eb;
    HRESULT hr = D3D12SerializeVersionedRootSignature(&v, &sb, &eb);
    if (FAILED(hr)) {
        printf("  [root signature] heap-flag rewrite failed 0x%08lx, using the original\n", (unsigned long)hr);
        hr = real->CreateRootSignature(nodeMask, blob, size, riid, out);
        if (SUCCEEDED(hr) && out && *out) g_signatures.push_back((ID3D12RootSignature*)*out);
        return hr;
    }
    hr = real->CreateRootSignature(nodeMask, sb->GetBufferPointer(), sb->GetBufferSize(), riid, out);
    if (SUCCEEDED(hr) && out && *out) g_signatures.push_back((ID3D12RootSignature*)*out);
    return hr;
}

static ID3D12RootSignature* HeapSignature(ID3D12Device* dev) {
    if (g_heapSig) return g_heapSig.Get();
    D3D12_DESCRIPTOR_RANGE r[3] = {};
    r[0] = {D3D12_DESCRIPTOR_RANGE_TYPE_SRV, 16, 0, 0, 0};
    r[1] = {D3D12_DESCRIPTOR_RANGE_TYPE_UAV, 16, 0, 0, 0};
    r[2] = {D3D12_DESCRIPTOR_RANGE_TYPE_CBV, 16, 0, 0, 0};
    D3D12_ROOT_PARAMETER p = {};
    p.ParameterType = D3D12_ROOT_PARAMETER_TYPE_DESCRIPTOR_TABLE;
    p.DescriptorTable = {3, r};
    p.ShaderVisibility = D3D12_SHADER_VISIBILITY_ALL;
    D3D12_ROOT_SIGNATURE_DESC rs = {1, &p, 0, nullptr, D3D12_ROOT_SIGNATURE_FLAG_CBV_SRV_UAV_HEAP_DIRECTLY_INDEXED};
    ComPtr<ID3DBlob> sb, eb;
    HRESULT hr = D3D12SerializeRootSignature(&rs, D3D_ROOT_SIGNATURE_VERSION_1, &sb, &eb);
    if (FAILED(hr)) { printf("  [heap root signature] serialize failed 0x%08lx\n", (unsigned long)hr); return nullptr; }
    hr = dev->CreateRootSignature(0, sb->GetBufferPointer(), sb->GetBufferSize(), IID_PPV_ARGS(&g_heapSig));
    if (FAILED(hr)) { printf("  [heap root signature] create failed 0x%08lx\n", (unsigned long)hr); g_heapSig.Reset(); return nullptr; }
    return g_heapSig.Get();
}
static HRESULT TryCreate(ID3D12Device* dev, const D3D12_COMPUTE_PIPELINE_STATE_DESC* d, REFIID riid, void** out, bool* crashed) {
    __try { *crashed = false; return dev->CreateComputePipelineState(d, riid, out); }
    __except (EXCEPTION_EXECUTE_HANDLER) { *crashed = true; return E_FAIL; }
}
static HRESULT __fastcall Slot62(XboxObjectProxy* self, const D3D12_COMPUTE_PIPELINE_STATE_DESC* desc, void* a, void* b, REFIID riid, void** out) {
    int idx = g_pipe++;
    if (g_dump) {
        char path[MAX_PATH]; sprintf_s(path, "%s%sshader_%02d.dxil", g_dump, "\\", idx);
        FILE* f = nullptr; if (!fopen_s(&f, path, "wb")) { fwrite(desc->CS.pShaderBytecode, 1, desc->CS.BytecodeLength, f); fclose(f); }
    }
    static int only = getenv("BVH_ONLY") ? atoi(getenv("BVH_ONLY")) : -1;     // BVH_ONLY=n: really compile blob n, give every other one the empty shader
    if (only >= 0 && idx != only) {
        D3D12_COMPUTE_PIPELINE_STATE_DESC d2 = *desc;
        d2.CS.pShaderBytecode = g_dummy; d2.CS.BytecodeLength = sizeof(g_dummy);
        return ((ID3D12Device*)self->real)->CreateComputePipelineState(&d2, riid, out);
    }
    // BVH_REPLACE_DIR: use <dir>/repl_NN.dxil (a lowered, re-signed shader) instead of blob NN
    D3D12_COMPUTE_PIPELINE_STATE_DESC dr = *desc;
    static std::vector<char> replBytes;
    if (const char* rd = getenv("BVH_REPLACE_DIR")) {
        char path[MAX_PATH]; sprintf_s(path, "%s\\repl_%02d.dxil", rd, idx);
        FILE* rf = nullptr;
        if (!fopen_s(&rf, path, "rb")) {
            fseek(rf, 0, SEEK_END); long n = ftell(rf); fseek(rf, 0, SEEK_SET);
            replBytes.resize(n); fread(replBytes.data(), 1, n, rf); fclose(rf);
            dr.CS.pShaderBytecode = replBytes.data(); dr.CS.BytecodeLength = n;
            if (!getenv("XS_HEAP"))                        // with XS_HEAP the builder's own signature already carries the flag
                if (ID3D12RootSignature* hs = HeapSignature((ID3D12Device*)self->real)) dr.pRootSignature = hs;
            printf("  [slot 62] #%02d: using replacement %s (%ld bytes, heap-indexed root signature)\n", idx, path, n);
        }
    }
    desc = &dr;
    bool crashed = false;
    HRESULT hr = TryCreate((ID3D12Device*)self->real, desc, riid, out, &crashed);
    if (only >= 0) printf("RESULT %d %s 0x%08lx" "\n", idx, crashed ? "CRASH" : FAILED(hr) ? "FAIL" : "OK", (unsigned long)hr);
    if (crashed || FAILED(hr)) {
        (crashed ? g_crashed : g_failed)++;
        printf("  [slot 62] #%02d %llu bytes: %s (0x%08lx), substituting an empty shader\n", idx, (unsigned long long)desc->CS.BytecodeLength, crashed ? "COMPILER CRASH" : "failed", (unsigned long)hr);
        D3D12_COMPUTE_PIPELINE_STATE_DESC d2 = *desc;
        d2.CS.pShaderBytecode = g_dummy; d2.CS.BytecodeLength = sizeof(g_dummy);
        hr = ((ID3D12Device*)self->real)->CreateComputePipelineState(&d2, riid, out);
    }
    return hr;
}

// Exported names (decorated) of the class members used here; the object is opaque, so it is allocated oversized and zeroed.
static const char* CTOR = "??0XboxBvhBuilder@@QEAA@PEAUID3D12Device11@@_N@Z";
static const char* DTOR = "??1XboxBvhBuilder@@QEAA@XZ";
static const char* PREBUILD = "?GetRaytracingAccelerationStructurePrebuildInfo@XboxBvhBuilder@@QEAAXPEBUD3D12_BUILD_RAYTRACING_ACCELERATION_STRUCTURE_INPUTS@@PEBUD3D12XBOX_XDXR_STREAM_DESC@@PEAUD3D12_RAYTRACING_ACCELERATION_STRUCTURE_PREBUILD_INFO@@@Z";
static const char* MORTON = "?Set64BitMortonCodes@XboxBvhBuilder@@QEAAX_N@Z";
static const char* BUILD = "?BuildRaytracingAccelerationStructure@XboxBvhBuilder@@QEAAXPEAUID3D12GraphicsCommandList4@@PEBUD3D12_BUILD_RAYTRACING_ACCELERATION_STRUCTURE_DESC@@IPEBUD3D12_RAYTRACING_ACCELERATION_STRUCTURE_POSTBUILD_INFO_DESC@@@Z";
static const char* REHYDRATE = "?RehydrateRaytracingAccelerationStructureX@XboxBvhBuilder@@QEAAJPEAUID3D12Device11@@PEBUD3D12_BUILD_RAYTRACING_ACCELERATION_STRUCTURE_DESC@@@Z";
static const char* VALIDATE_REHYDRATE = "?ValidateRehydrateAccelerationStructureBuildDesc@XboxBvhBuilder@@QEAAJPEBUD3D12_BUILD_RAYTRACING_ACCELERATION_STRUCTURE_DESC@@@Z";
typedef void* (__fastcall* CtorFn)(void*, ID3D12Device*, bool);
typedef void(__fastcall* DtorFn)(void*);
typedef void(__fastcall* PrebuildFn)(void*, const D3D12_BUILD_RAYTRACING_ACCELERATION_STRUCTURE_INPUTS*, const void*, D3D12_RAYTRACING_ACCELERATION_STRUCTURE_PREBUILD_INFO*);
typedef void(__fastcall* MortonFn)(void*, bool);
typedef void(__fastcall* BuildFn)(void*, ID3D12GraphicsCommandList4*, const D3D12_BUILD_RAYTRACING_ACCELERATION_STRUCTURE_DESC*, UINT, const D3D12_RAYTRACING_ACCELERATION_STRUCTURE_POSTBUILD_INFO_DESC*);
typedef long(__fastcall* RehydrateFn)(void*, ID3D12Device11*, const D3D12_BUILD_RAYTRACING_ACCELERATION_STRUCTURE_DESC*);
typedef long(__fastcall* ValidateRehydrateFn)(void*, const D3D12_BUILD_RAYTRACING_ACCELERATION_STRUCTURE_DESC*);

// Describe a thrown C++ exception from its EXCEPTION_RECORD (MSVC x64: params = magic, object, ThrowInfo, image base; all links are RVAs)
static int Describe(EXCEPTION_POINTERS* p) {
    EXCEPTION_RECORD* r = p->ExceptionRecord;
    if (r->ExceptionCode == 0xe06d7363 && r->NumberParameters >= 4) {
        auto base = (const char*)r->ExceptionInformation[3];
        auto ti = (const int*)r->ExceptionInformation[2];
        auto arr = (const int*)(base + ti[3]);
        printf("  thrown C++ type(s):");
        for (int i = 0; i < arr[0] && i < 6; ++i) {
            auto ct = (const int*)(base + arr[1 + i]);
            auto tinfo = (const char*)(base + ct[1]);
            printf(" %s", tinfo + 16);
        }
        auto obj = (const unsigned char*)r->ExceptionInformation[1];
        printf("%s  first bytes of the object:", "\n");
        for (int i = 0; i < 16; ++i) printf(" %02x", obj[i]);
        printf("\n");
    }
    return EXCEPTION_EXECUTE_HANDLER;
}
// Print the current call stack as module+offset (offsets in XboxBvhBuilder.dll can be looked up in a disassembly)
static void PrintStack(const char* why) {
    void* frames[32];
    USHORT n = CaptureStackBackTrace(0, 32, frames, nullptr);
    printf("  %s, call stack:\n", why);
    for (USHORT i = 0; i < n; ++i) {
        HMODULE mod = nullptr;
        GetModuleHandleExA(GET_MODULE_HANDLE_EX_FLAG_FROM_ADDRESS | GET_MODULE_HANDLE_EX_FLAG_UNCHANGED_REFCOUNT, (LPCSTR)frames[i], &mod);
        char name[MAX_PATH] = "?";
        if (mod) GetModuleFileNameA(mod, name, MAX_PATH);
        const char* base = strrchr(name, 0x5c);
        printf("    %s+0x%llx\n", base ? base + 1 : name, (unsigned long long)((char*)frames[i] - (char*)mod));
    }
    fflush(stdout);
}
static LONG CALLBACK TraceThrow(EXCEPTION_POINTERS* p) {
    { DWORD c = p->ExceptionRecord->ExceptionCode; if (c == 0xe06d7363 || c == 0xc0000005) PrintStack(c == 0xc0000005 ? "access violation" : "C++ throw"); }
    return EXCEPTION_CONTINUE_SEARCH;
}
static void OnTerminate() { PrintStack("std::terminate"); _exit(0x409); }
static void OnAbort(int) { PrintStack("abort"); _exit(0x40a); }
// SEH wrappers (functions with __try cannot hold C++ objects needing unwinding)
static void CallCtor(CtorFn f, void* o, ID3D12Device* d) {
    try { f(o, d, false); }
    catch (const std::exception& e) { printf("  C++ exception std::exception: %s\n", e.what()); throw; }
}
static unsigned long SehCtor(CtorFn f, void* o, ID3D12Device* d) { __try { CallCtor(f, o, d); return 0; } __except (Describe(GetExceptionInformation())) { return GetExceptionCode(); } }
static unsigned long SehPrebuild(PrebuildFn f, void* o, const D3D12_BUILD_RAYTRACING_ACCELERATION_STRUCTURE_INPUTS* in, D3D12_RAYTRACING_ACCELERATION_STRUCTURE_PREBUILD_INFO* out) { __try { f(o, in, nullptr, out); return 0; } __except (Describe(GetExceptionInformation())) { return GetExceptionCode(); } }
static unsigned long SehDtor(DtorFn f, void* o) { __try { f(o); return 0; } __except (EXCEPTION_EXECUTE_HANDLER) { return GetExceptionCode(); } }
static unsigned long SehBuild(BuildFn f, void* o, ID3D12GraphicsCommandList4* cl, const D3D12_BUILD_RAYTRACING_ACCELERATION_STRUCTURE_DESC* d) { __try { f(o, cl, d, 0, nullptr); return 0; } __except (Describe(GetExceptionInformation())) { return GetExceptionCode(); } }
static long SehRehydrate(RehydrateFn f, void* o, ID3D12Device11* d, const D3D12_BUILD_RAYTRACING_ACCELERATION_STRUCTURE_DESC* desc) { __try { return f(o, d, desc); } __except (Describe(GetExceptionInformation())) { return -1; } }
static long SehValidateRehydrate(ValidateRehydrateFn f, void* o, const D3D12_BUILD_RAYTRACING_ACCELERATION_STRUCTURE_DESC* desc) { __try { return f(o, desc); } __except (Describe(GetExceptionInformation())) { return -1; } }

static void DumpMessages(ID3D12Device* dev) {
    ComPtr<ID3D12InfoQueue> q;
    if (FAILED(dev->QueryInterface(IID_PPV_ARGS(&q)))) { printf("  (no info queue: debug layer not installed)\n"); return; }
    UINT64 n = q->GetNumStoredMessages();
    for (UINT64 i = 0; i < n && i < 40; ++i) {
        SIZE_T len = 0;
        q->GetMessage(i, nullptr, &len);
        auto* m = (D3D12_MESSAGE*)malloc(len);
        q->GetMessage(i, m, &len);
        printf("  [d3d12 %d] %.300s\n", (int)m->Severity, m->pDescription);
        free(m);
    }
    q->ClearStoredMessages();
}

int main(int argc, char** argv) {
    setvbuf(stdout, nullptr, _IONBF, 0);
    bool warp = false, morton64 = false, nodebug = false;
    for (int i = 1; i < argc; ++i) { warp |= !strcmp(argv[i], "warp"); morton64 |= !strcmp(argv[i], "64bit-morton"); nodebug |= !strcmp(argv[i], "nodebug"); }
    ComPtr<ID3D12Debug> dbg;
    if (!nodebug && SUCCEEDED(D3D12GetDebugInterface(IID_PPV_ARGS(&dbg)))) dbg->EnableDebugLayer();
    ComPtr<IDXGIFactory4> fac;
    CreateDXGIFactory1(IID_PPV_ARGS(&fac));
    ComPtr<IDXGIAdapter> ad;
    if (warp) fac->EnumWarpAdapter(IID_PPV_ARGS(&ad)); else fac->EnumAdapters(0, &ad);
    DXGI_ADAPTER_DESC d; ad->GetDesc(&d);
    printf("adapter: %ls\n", d.Description);
    ComPtr<ID3D12Device> dev;
    if (FAILED(D3D12CreateDevice(ad.Get(), D3D_FEATURE_LEVEL_12_1, IID_PPV_ARGS(&dev)))) { printf("no D3D12 device\n"); return 1; }
    ComPtr<ID3D12Device11> dev11;
    printf("ID3D12Device11: %s\n", SUCCEEDED(dev.As(&dev11)) ? "yes" : "NO (the builder wants a Device11)");
    D3D12_FEATURE_DATA_D3D12_OPTIONS5 o5 = {};
    dev->CheckFeatureSupport(D3D12_FEATURE_D3D12_OPTIONS5, &o5, sizeof(o5));
    printf("raytracing tier: %d\n", (int)o5.RaytracingTier);
    D3D12_FEATURE_DATA_SHADER_MODEL sm = { D3D_SHADER_MODEL_6_8 };
    dev->CheckFeatureSupport(D3D12_FEATURE_SHADER_MODEL, &sm, sizeof(sm));
    printf("highest shader model: 0x%x\n", (unsigned)sm.HighestShaderModel);

    AddVectoredExceptionHandler(1, TraceThrow);
    std::set_terminate(OnTerminate);
    signal(SIGABRT, OnAbort);
    HMODULE m = LoadLibraryA("XboxBvhBuilder.dll");
    if (!m) { printf("LoadLibrary XboxBvhBuilder.dll failed %lu\n", GetLastError()); return 1; }
    auto ctor = (CtorFn)GetProcAddress(m, CTOR);
    auto dtor = (DtorFn)GetProcAddress(m, DTOR);
    auto prebuild = (PrebuildFn)GetProcAddress(m, PREBUILD);
    auto set64 = (MortonFn)GetProcAddress(m, MORTON);
    if (!ctor || !prebuild) { printf("exports not found\n"); return 1; }

    void* obj = _aligned_malloc(1 << 20, 64);
    memset(obj, 0, 1 << 20);
    printf("constructing XboxBvhBuilder(device, false)...\n"); fflush(stdout);
    XboxObjectProxy* proxy = getenv("BVH_BUILD") ? xbox_overrides::WrapDevice(dev.Get()) : XboxObjectProxy::Create(dev.Get());
    g_dump = getenv("BVH_DUMP_DIR");
    proxy->Override(62, (void*)Slot62);
    proxy->Override(16, (void*)SlotRootSignature);     // XS_HEAP=1: add the descriptor-heap flag to the signatures built from RTS0
    if (getenv("BVH_BUILD")) {                         // BVH_TRACE=1 then shows the last call before a fault in a builder thread
        proxy->Override(14, (void*)Trace14);
        proxy->Override(76, (void*)Trace76);
        proxy->Override(18, (void*)Trace18);
        proxy->Override(19, (void*)Trace19);
    }
    if (unsigned long ex = SehCtor(ctor, obj, (ID3D12Device*)proxy)) {
        printf("  constructor raised exception 0x%lx\n", ex);
        DumpMessages(dev.Get());
        return 2;
    }
    printf("  constructor returned\n");
    printf("pipelines requested %d, compiler crashes %d, other failures %d\n", g_pipe, g_crashed, g_failed);
    proxy->PrintHits("device");
    DumpMessages(dev.Get());
    if (morton64 && set64) set64(obj, true);

    // one triangle-geometry bottom-level structure, 1,000 triangles worth of vertices (sizes only, no memory is read)
    D3D12_RAYTRACING_GEOMETRY_DESC g = {};
    g.Type = D3D12_RAYTRACING_GEOMETRY_TYPE_TRIANGLES;
    g.Triangles.VertexFormat = DXGI_FORMAT_R32G32B32_FLOAT;
    g.Triangles.VertexCount = 3000;
    g.Triangles.VertexBuffer.StrideInBytes = 12;
    g.Triangles.IndexFormat = DXGI_FORMAT_UNKNOWN;
    D3D12_BUILD_RAYTRACING_ACCELERATION_STRUCTURE_INPUTS in = {};
    in.Type = D3D12_RAYTRACING_ACCELERATION_STRUCTURE_TYPE_BOTTOM_LEVEL;
    in.Flags = D3D12_RAYTRACING_ACCELERATION_STRUCTURE_BUILD_FLAG_PREFER_FAST_TRACE;
    in.DescsLayout = D3D12_ELEMENTS_LAYOUT_ARRAY;
    in.NumDescs = 1;
    in.pGeometryDescs = &g;
    D3D12_RAYTRACING_ACCELERATION_STRUCTURE_PREBUILD_INFO pc = {}, xb = {};
    ComPtr<ID3D12Device5> d5;
    if (SUCCEEDED(dev.As(&d5))) {
        d5->GetRaytracingAccelerationStructurePrebuildInfo(&in, &pc);
        printf("PC driver  prebuild: result %llu  scratch %llu  update-scratch %llu\n", pc.ResultDataMaxSizeInBytes, pc.ScratchDataSizeInBytes, pc.UpdateScratchDataSizeInBytes);
    }
    if (unsigned long ex = SehPrebuild(prebuild, obj, &in, &xb)) printf("prebuild raised exception 0x%lx\n", ex);
    else printf("Xbox builder prebuild: result %llu  scratch %llu  update-scratch %llu\n", xb.ResultDataMaxSizeInBytes, xb.ScratchDataSizeInBytes, xb.UpdateScratchDataSizeInBytes);
    DumpMessages(dev.Get());

    if (getenv("BVH_BUILD")) {
        // Experimental: reaches the builder's build path; a builder worker thread then takes an access violation before any dispatch.
        // What is still missing is listed in PLATFORM_SURFACE.md (heap/table for the lowered address accesses, unwrapping proxies in
        // command-list calls). Keep it behind BVH_BUILD=1 until that works.
        auto build = (BuildFn)GetProcAddress(m, BUILD);
        if (!build) { printf("Build export not found\n"); }
        else {
            printf("  [build] start\n");
            auto mkres = [&](UINT64 bytes, D3D12_HEAP_TYPE ht, D3D12_RESOURCE_FLAGS fl, D3D12_RESOURCE_STATES st) {
                D3D12_HEAP_PROPERTIES hp = {ht};
                D3D12_RESOURCE_DESC rd = {D3D12_RESOURCE_DIMENSION_BUFFER, 0, bytes, 1, 1, 1, DXGI_FORMAT_UNKNOWN, {1, 0}, D3D12_TEXTURE_LAYOUT_ROW_MAJOR, fl};
                ComPtr<ID3D12Resource> r;
                HRESULT hr = dev->CreateCommittedResource(&hp, D3D12_HEAP_FLAG_NONE, &rd, st, nullptr, IID_PPV_ARGS(&r));
                if (Trace()) fprintf(stderr, "  [build] mkres %llu -> 0x%08lx %p\n", (unsigned long long)bytes, (unsigned long)hr, (void*)r.Get());
                if (FAILED(hr)) printf("  resource %llu bytes failed 0x%08lx\n", (unsigned long long)bytes, (unsigned long)hr);
                return r;
            };
            ComPtr<ID3D12Resource> vbUp = mkres(36, D3D12_HEAP_TYPE_UPLOAD, D3D12_RESOURCE_FLAG_NONE, D3D12_RESOURCE_STATE_GENERIC_READ);
            { void* v = nullptr; if (SUCCEEDED(vbUp->Map(0, nullptr, &v)) && v) {
                float tri[9] = {0, 0, 0, 1, 0, 0, 0, 1, 0}; memcpy(v, tri, sizeof(tri)); vbUp->Unmap(0, nullptr); }
              else printf("  vertex upload Map failed\n"); }
            ComPtr<ID3D12Resource> vb = mkres(36, D3D12_HEAP_TYPE_DEFAULT, D3D12_RESOURCE_FLAG_ALLOW_UNORDERED_ACCESS, D3D12_RESOURCE_STATE_COPY_DEST);
            D3D12_RAYTRACING_GEOMETRY_DESC g2 = {};
            g2.Type = D3D12_RAYTRACING_GEOMETRY_TYPE_TRIANGLES;
            g2.Triangles.VertexFormat = DXGI_FORMAT_R32G32B32_FLOAT;
            g2.Triangles.VertexCount = 3;
            g2.Triangles.VertexBuffer.StartAddress = vb->GetGPUVirtualAddress();
            g2.Triangles.VertexBuffer.StrideInBytes = 12;
            g2.Triangles.IndexFormat = DXGI_FORMAT_UNKNOWN;
            D3D12_BUILD_RAYTRACING_ACCELERATION_STRUCTURE_INPUTS bin = {};
            bin.Type = D3D12_RAYTRACING_ACCELERATION_STRUCTURE_TYPE_BOTTOM_LEVEL;
            bin.Flags = D3D12_RAYTRACING_ACCELERATION_STRUCTURE_BUILD_FLAG_PREFER_FAST_TRACE;
            bin.DescsLayout = D3D12_ELEMENTS_LAYOUT_ARRAY;
            bin.NumDescs = 1;
            bin.pGeometryDescs = &g2;
            D3D12_RAYTRACING_ACCELERATION_STRUCTURE_PREBUILD_INFO info = {};
            printf("  [build] prebuild 2\n");
            if (unsigned long ex = SehPrebuild(prebuild, obj, &bin, &info)) printf("  build prebuild raised 0x%lx\n", ex);
            UINT64 resultBytes = (info.ResultDataMaxSizeInBytes + 255) & ~255ull, scratchBytes = (info.ScratchDataSizeInBytes + 255) & ~255ull;
            printf("  build sizes: result %llu scratch %llu\n", (unsigned long long)resultBytes, (unsigned long long)scratchBytes);
            printf("  [build] result/scratch\n");
            ComPtr<ID3D12Resource> result = mkres(resultBytes, D3D12_HEAP_TYPE_DEFAULT, D3D12_RESOURCE_FLAG_ALLOW_UNORDERED_ACCESS, D3D12_RESOURCE_STATE_UNORDERED_ACCESS);
            ComPtr<ID3D12Resource> scratch = mkres(scratchBytes, D3D12_HEAP_TYPE_DEFAULT, D3D12_RESOURCE_FLAG_ALLOW_UNORDERED_ACCESS, D3D12_RESOURCE_STATE_UNORDERED_ACCESS);
            D3D12_BUILD_RAYTRACING_ACCELERATION_STRUCTURE_DESC bd = {};
            bd.Inputs = bin;
            bd.ScratchAccelerationStructureData = scratch->GetGPUVirtualAddress();
            bd.DestAccelerationStructureData = result->GetGPUVirtualAddress();
            printf("  [build] list\n");
            ComPtr<ID3D12CommandAllocator> ba;
            dev->CreateCommandAllocator(D3D12_COMMAND_LIST_TYPE_DIRECT, IID_PPV_ARGS(&ba));
            ComPtr<ID3D12GraphicsCommandList4> list;
            dev->CreateCommandList(0, D3D12_COMMAND_LIST_TYPE_DIRECT, ba.Get(), nullptr, IID_PPV_ARGS(&list));
            list->CopyBufferRegion(vb.Get(), 0, vbUp.Get(), 0, 36);
            D3D12_RESOURCE_BARRIER bar = {};
            bar.Type = D3D12_RESOURCE_BARRIER_TYPE_TRANSITION; bar.Transition.pResource = vb.Get();
            bar.Transition.StateBefore = D3D12_RESOURCE_STATE_COPY_DEST; bar.Transition.StateAfter = D3D12_RESOURCE_STATE_UNORDERED_ACCESS;
            bar.Transition.Subresource = 0xffffffff;
            list->ResourceBarrier(1, &bar);
            XboxObjectProxy* clp = XboxObjectProxy::Create((IUnknown*)list.Get(), "cmdlist");
            clp->Override(14, (void*)ClDispatch);      clp->Override(15, (void*)ClCopyBuffer);
            clp->Override(25, (void*)ClSetPso);        clp->Override(26, (void*)ClBarrier);
            clp->Override(28, (void*)ClSetHeaps);      clp->Override(29, (void*)ClSetRootSig);
            clp->Override(31, (void*)ClSetTable);
            clp->Override(148, (void*)ClReserve);      // Xbox extension: command-stream reservation used by the builder
            clp->Override(33, (void*)ClSet32);
            if (!g_signatures.empty()) {              // on Xbox the caller's root signature is still bound; set one of the builder's own
                UINT pick = getenv("BVH_SIG") ? (UINT)atoi(getenv("BVH_SIG")) : 0;
                if (pick >= g_signatures.size()) pick = 0;
                list->SetComputeRootSignature(g_signatures[pick]);
                printf("  [build] root signature %u of %zu set before Build\n", pick, g_signatures.size());
            }
            printf("calling BuildRaytracingAccelerationStructure...\n"); fflush(stdout);
            if (unsigned long ex = SehBuild(build, obj, (ID3D12GraphicsCommandList4*)clp, &bd)) printf("  build raised exception 0x%lx\n", ex);
            else printf("  build returned\n");
            clp->PrintHits("cmdlist");
            DumpMessages(dev.Get());
            HRESULT ch = list->Close();
            printf("  command list close 0x%08lx\n", (unsigned long)ch);
            ComPtr<ID3D12CommandQueue> q; D3D12_COMMAND_QUEUE_DESC qd = {};
            dev->CreateCommandQueue(&qd, IID_PPV_ARGS(&q));
            ID3D12CommandList* lists[] = {list.Get()};
            q->ExecuteCommandLists(1, lists);
            ComPtr<ID3D12Fence> fence; dev->CreateFence(0, D3D12_FENCE_FLAG_NONE, IID_PPV_ARGS(&fence));
            q->Signal(fence.Get(), 1);
            HANDLE ev = CreateEvent(0, 0, 0, 0); fence->SetEventOnCompletion(1, ev);
            DWORD w = WaitForSingleObject(ev, 30000);
            HRESULT dr = dev->GetDeviceRemovedReason();
            printf("  wait %lu, device removed reason 0x%08lx\n", (unsigned long)w, (unsigned long)dr);
            DumpMessages(dev.Get());
            if (SUCCEEDED(dr)) {
                ComPtr<ID3D12Resource> rb = mkres(resultBytes, D3D12_HEAP_TYPE_READBACK, D3D12_RESOURCE_FLAG_NONE, D3D12_RESOURCE_STATE_COPY_DEST);
                D3D12_RESOURCE_BARRIER pb = {};
                pb.Type = D3D12_RESOURCE_BARRIER_TYPE_TRANSITION; pb.Transition.pResource = result.Get();
                pb.Transition.StateBefore = D3D12_RESOURCE_STATE_UNORDERED_ACCESS; pb.Transition.StateAfter = D3D12_RESOURCE_STATE_COPY_SOURCE;
                pb.Transition.Subresource = 0xffffffff;
                list->Reset(ba.Get(), nullptr);
                list->ResourceBarrier(1, &pb);
                list->CopyResource(rb.Get(), result.Get());
                list->Close();
                q->ExecuteCommandLists(1, lists);
                q->Signal(fence.Get(), 2);
                fence->SetEventOnCompletion(2, ev); WaitForSingleObject(ev, 30000);
                void* p; rb->Map(0, nullptr, &p);
                const unsigned* w32 = (const unsigned*)p;
                size_t nz = 0; for (int i = 0; i < 64; i++) if (w32[i]) nz++;
                printf("  result first words:");
                for (int i = 0; i < 16; i++) printf(" %08x", w32[i]);
                printf(" (%zu of 64 nonzero)\n", nz);
                rb->Unmap(0, nullptr);
            }
        }
    }

    if (const char* rdir = getenv("BVH_REHYDRATE")) {
        // RehydrateRaytracingAccelerationStructureX(device, desc): the compute path where DecompressHeader / Box32 / Box16 / Leaf run.
        // The fixture comes from tools/xbox/make_bvh_fixture.py (synthetic, no game data) and expected.bin is the CPU model's output.
        auto rehydrate = (RehydrateFn)GetProcAddress(m, REHYDRATE);
        auto validate = (ValidateRehydrateFn)GetProcAddress(m, VALIDATE_REHYDRATE);
        printf("  [rehydrate] %s\n", rehydrate ? "export found" : "EXPORT NOT FOUND");
        if (rehydrate) {
            auto read = [&](const char* name) {
                char p[MAX_PATH] = {}; sprintf_s(p, "%s\\%s", rdir, name);
                std::ifstream f(p, std::ios::binary);
                return std::vector<char>((std::istreambuf_iterator<char>(f)), {});
            };
            std::vector<char> src = read("src.bin"), vb = read("vb.bin"), expected = read("expected.bin");
            printf("  [rehydrate] fixture: compressed %zu, vertices %zu, expected %zu\n", src.size(), vb.size(), expected.size());
            auto mkdef = [&](UINT64 bytes, D3D12_RESOURCE_FLAGS fl) {
                D3D12_HEAP_PROPERTIES hp = {D3D12_HEAP_TYPE_DEFAULT};
                D3D12_RESOURCE_DESC rd = {D3D12_RESOURCE_DIMENSION_BUFFER, 0, bytes, 1, 1, 1, DXGI_FORMAT_UNKNOWN, {1, 0}, D3D12_TEXTURE_LAYOUT_ROW_MAJOR, fl};
                ComPtr<ID3D12Resource> r;
                HRESULT hr = dev->CreateCommittedResource(&hp, D3D12_HEAP_FLAG_NONE, &rd, D3D12_RESOURCE_STATE_COPY_DEST, nullptr, IID_PPV_ARGS(&r));
                if (FAILED(hr)) printf("  [rehydrate] buffer %llu failed 0x%08lx\n", (unsigned long long)bytes, (unsigned long)hr);
                return r;
            };
            auto upload = [&](const std::vector<char>& bytes) {
                D3D12_HEAP_PROPERTIES hp = {D3D12_HEAP_TYPE_UPLOAD};
                D3D12_RESOURCE_DESC rd = {D3D12_RESOURCE_DIMENSION_BUFFER, 0, bytes.size(), 1, 1, 1, DXGI_FORMAT_UNKNOWN, {1, 0}, D3D12_TEXTURE_LAYOUT_ROW_MAJOR, D3D12_RESOURCE_FLAG_NONE};
                ComPtr<ID3D12Resource> r;
                dev->CreateCommittedResource(&hp, D3D12_HEAP_FLAG_NONE, &rd, D3D12_RESOURCE_STATE_GENERIC_READ, nullptr, IID_PPV_ARGS(&r));
                void* p = nullptr; if (SUCCEEDED(r->Map(0, nullptr, &p)) && p) { memcpy(p, bytes.data(), bytes.size()); r->Unmap(0, nullptr); }
                return r;
            };
            const UINT64 VB_OFF = 0x8000;
            ComPtr<ID3D12Resource> dataBuf;
            void* aliasPtr = nullptr;
            for (int attempt = 0; attempt < 8 && !aliasPtr; attempt++) {
                dataBuf = mkdef(0x10000, D3D12_RESOURCE_FLAG_ALLOW_UNORDERED_ACCESS);
                aliasPtr = VirtualAlloc((void*)(uintptr_t)dataBuf->GetGPUVirtualAddress(), 0x10000, MEM_COMMIT | MEM_RESERVE, PAGE_READWRITE);
                if (!aliasPtr) dataBuf.Reset();
            }
            ComPtr<ID3D12Resource> dstBuf = mkdef(expected.size(), D3D12_RESOURCE_FLAG_ALLOW_UNORDERED_ACCESS);
            ComPtr<ID3D12Resource> scratchBuf = mkdef(1 << 20, D3D12_RESOURCE_FLAG_ALLOW_UNORDERED_ACCESS);
            printf("  [rehydrate] src+vb buffer VA %llx, CPU alias %p\n",
                   dataBuf ? (unsigned long long)dataBuf->GetGPUVirtualAddress() : 0ull, aliasPtr);
            std::vector<char> blob(0x10000, 0);
            memcpy(blob.data(), src.data(), src.size());
            memcpy(blob.data() + VB_OFF, vb.data(), vb.size());
            if (aliasPtr) memcpy(aliasPtr, blob.data(), blob.size());
            D3D12_HEAP_PROPERTIES uph = {D3D12_HEAP_TYPE_UPLOAD};
            D3D12_RESOURCE_DESC urd = {D3D12_RESOURCE_DIMENSION_BUFFER, 0, 0x10000, 1, 1, 1, DXGI_FORMAT_UNKNOWN, {1, 0}, D3D12_TEXTURE_LAYOUT_ROW_MAJOR, D3D12_RESOURCE_FLAG_NONE};
            ComPtr<ID3D12Resource> blobUp;
            dev->CreateCommittedResource(&uph, D3D12_HEAP_FLAG_NONE, &urd, D3D12_RESOURCE_STATE_GENERIC_READ, nullptr, IID_PPV_ARGS(&blobUp));
            { void* p = nullptr; if (SUCCEEDED(blobUp->Map(0, nullptr, &p)) && p) { memcpy(p, blob.data(), blob.size()); blobUp->Unmap(0, nullptr); } }
            ComPtr<ID3D12CommandAllocator> ua; dev->CreateCommandAllocator(D3D12_COMMAND_LIST_TYPE_DIRECT, IID_PPV_ARGS(&ua));
            ComPtr<ID3D12GraphicsCommandList4> ul;
            dev->CreateCommandList(0, D3D12_COMMAND_LIST_TYPE_DIRECT, ua.Get(), nullptr, IID_PPV_ARGS(&ul));
            ul->CopyResource(dataBuf.Get(), blobUp.Get());
            ID3D12Resource* t[2] = {dataBuf.Get(), dstBuf.Get()};
            for (int i = 0; i < 2; i++) {
                D3D12_RESOURCE_BARRIER b = {};
                b.Type = D3D12_RESOURCE_BARRIER_TYPE_TRANSITION; b.Transition.pResource = t[i];
                b.Transition.StateBefore = D3D12_RESOURCE_STATE_COPY_DEST; b.Transition.StateAfter = D3D12_RESOURCE_STATE_UNORDERED_ACCESS;
                b.Transition.Subresource = 0xffffffff; ul->ResourceBarrier(1, &b);
            }
            ul->Close();
            ComPtr<ID3D12CommandQueue> uq; D3D12_COMMAND_QUEUE_DESC qd = {};
            dev->CreateCommandQueue(&qd, IID_PPV_ARGS(&uq));
            ID3D12CommandList* lists[] = {ul.Get()}; uq->ExecuteCommandLists(1, lists);
            ComPtr<ID3D12Fence> uf; dev->CreateFence(0, D3D12_FENCE_FLAG_NONE, IID_PPV_ARGS(&uf)); uq->Signal(uf.Get(), 1);
            HANDLE uev = CreateEvent(0, 0, 0, 0); uf->SetEventOnCompletion(1, uev); WaitForSingleObject(uev, 30000);
            printf("  [rehydrate] data ready, device removed 0x%08lx\n", (unsigned long)dev->GetDeviceRemovedReason());

            D3D12_RAYTRACING_GEOMETRY_DESC g = {};
            g.Type = D3D12_RAYTRACING_GEOMETRY_TYPE_TRIANGLES;
            g.Triangles.VertexFormat = DXGI_FORMAT_R32G32B32_FLOAT;
            g.Triangles.VertexCount = (UINT)(vb.size() / 12);
            g.Triangles.VertexBuffer.StartAddress = dataBuf->GetGPUVirtualAddress() + VB_OFF;
            g.Triangles.VertexBuffer.StrideInBytes = 12;
            g.Triangles.IndexFormat = DXGI_FORMAT_UNKNOWN;
            D3D12_BUILD_RAYTRACING_ACCELERATION_STRUCTURE_DESC bd = {};
            bd.Inputs.Type = D3D12_RAYTRACING_ACCELERATION_STRUCTURE_TYPE_BOTTOM_LEVEL;
            bd.Inputs.Flags = D3D12_RAYTRACING_ACCELERATION_STRUCTURE_BUILD_FLAG_PREFER_FAST_TRACE;
            bd.Inputs.DescsLayout = D3D12_ELEMENTS_LAYOUT_ARRAY;
            bd.Inputs.NumDescs = 1;
            bd.Inputs.pGeometryDescs = &g;
            bd.SourceAccelerationStructureData = dataBuf->GetGPUVirtualAddress();
            printf("  [rehydrate] dst VA %llx, scratch VA %llx\n",
                   (unsigned long long)dstBuf->GetGPUVirtualAddress(), (unsigned long long)scratchBuf->GetGPUVirtualAddress());
            bd.DestAccelerationStructureData = dstBuf->GetGPUVirtualAddress();
            bd.ScratchAccelerationStructureData = scratchBuf->GetGPUVirtualAddress();
            if (validate) printf("  [rehydrate] validate desc -> 0x%08lx\n", (unsigned long)SehValidateRehydrate(validate, obj, &bd));
            long rh = SehRehydrate(rehydrate, obj, (ID3D12Device11*)proxy, &bd);
            printf("  [rehydrate] call -> 0x%08lx, device removed 0x%08lx\n", (unsigned long)rh, (unsigned long)dev->GetDeviceRemovedReason());
            DumpMessages(dev.Get());
            if (SUCCEEDED(dev->GetDeviceRemovedReason())) {
                ComPtr<ID3D12Resource> rb = mkdef(expected.size(), D3D12_RESOURCE_FLAG_NONE);
                D3D12_RESOURCE_BARRIER b = {};
                b.Type = D3D12_RESOURCE_BARRIER_TYPE_TRANSITION; b.Transition.pResource = dstBuf.Get();
                b.Transition.StateBefore = D3D12_RESOURCE_STATE_UNORDERED_ACCESS; b.Transition.StateAfter = D3D12_RESOURCE_STATE_COPY_SOURCE;
                b.Transition.Subresource = 0xffffffff;
                ul->Reset(ua.Get(), nullptr);
                ul->ResourceBarrier(1, &b);
                ul->CopyResource(rb.Get(), dstBuf.Get());
                ul->Close();
                uq->ExecuteCommandLists(1, lists);
                uq->Signal(uf.Get(), 2);
                uf->SetEventOnCompletion(2, uev); WaitForSingleObject(uev, 30000);
                void* p = nullptr;
                if (SUCCEEDED(rb->Map(0, nullptr, &p)) && p) {
                    size_t n = expected.size(), diff = 0, first = (size_t)-1;
                    for (size_t i = 0; i < n && i < expected.size(); i++)
                        if (((const unsigned char*)p)[i] != ((const unsigned char*)expected.data())[i]) { if (first == (size_t)-1) first = i; diff++; }
                    printf("  [rehydrate] %zu of %zu bytes differ from the CPU model%s\n", diff, n, diff ? "" : " (match)");
                    if (first != (size_t)-1) {
                        printf("    first at 0x%zx: got", first);
                        for (size_t i = first; i < first + 16 && i < n; i++) printf(" %02x", ((const unsigned char*)p)[i]);
                        printf(" expected");
                        for (size_t i = first; i < first + 16 && i < n; i++) printf(" %02x", ((const unsigned char*)expected.data())[i]);
                        printf("\n");
                    }
                    rb->Unmap(0, nullptr);
                }
            }
        }
    }

    if (dtor) { unsigned long ex = SehDtor(dtor, obj); if (ex) printf("destructor exception 0x%lx\n", ex); else printf("destructor ok\n"); }
    return 0;
}
