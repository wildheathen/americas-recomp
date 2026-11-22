// rehydrate_cpu: drive XboxBvhBuilder.dll's CPU rehydration entry (RehydrateRaytracingAccelerationStructureX) for a compressed BLAS.
// The export reads its source, vertices, destination and scratch through CPU pointers, as on the console where an allocation's CPU and
// GPU addresses are the same; this tool maps memory at each buffer's GPU virtual address and copies the data there, so the same number
// is a CPU pointer for the builder and a GPU address for its resources.
// build: cl /nologo /EHsc /O2 /std:c++17 rehydrate_cpu.cpp d3d12.lib dxgi.lib /Fe:rehydrate_cpu.exe
// usage: rehydrate_cpu <src.bin> <vb.bin> <out.bin> [stride=12] [vertexCount=vb/stride] [vertexFormat=6] [index.bin] [indexFormat=42]
//        rehydrate_cpu <src.bin> <out.bin> --geom <vb.bin> <index.bin|-> <stride> <vertexCount> <vertexFormat> <indexFormat> [vertexByteOffset] [--geom ...]
//   src.bin starts with the 64-byte Xbox BVH header; the --geom form passes one D3D12_RAYTRACING_GEOMETRY_DESC per geometry, in index
//   order. XboxBvhBuilder.dll must be loadable.
#include <windows.h>
#include <d3d12.h>
#include <dxgi1_4.h>
#include <wrl/client.h>
#include <cstdio>
#include <cstdint>
#include <cstring>
#include <cstdlib>
#include <vector>
#include <fstream>
#include <iterator>
#include <string>
using Microsoft::WRL::ComPtr;
#include "xbox_device_proxy.h"
#include "bvh_dummy_cs.h"

typedef long(__fastcall* RehydrateFn)(void*, ID3D12Device11*, const D3D12_BUILD_RAYTRACING_ACCELERATION_STRUCTURE_DESC*);
typedef long(__fastcall* ValidateFn)(void*, const D3D12_BUILD_RAYTRACING_ACCELERATION_STRUCTURE_DESC*);
static const char* REHYDRATE = "?RehydrateRaytracingAccelerationStructureX@XboxBvhBuilder@@QEAAJPEAUID3D12Device11@@PEBUD3D12_BUILD_RAYTRACING_ACCELERATION_STRUCTURE_DESC@@@Z";
static const char* VALIDATE = "?ValidateRehydrateAccelerationStructureBuildDesc@XboxBvhBuilder@@QEAAJPEBUD3D12_BUILD_RAYTRACING_ACCELERATION_STRUCTURE_DESC@@@Z";

static std::vector<char> Read(const char* path) {
    std::ifstream f(path, std::ios::binary);
    if (!f) { printf("cannot open %s\n", path); exit(2); }
    return std::vector<char>((std::istreambuf_iterator<char>(f)), {});
}

// The builder's constructor compiles its 53 pipelines, which the PC driver cannot take from the Xbox DXIL: slot 62 hands it an empty
// compute shader for every one of them. Only the CPU rehydration entry is used here anyway.
static HRESULT __fastcall DummyPso(XboxObjectProxy* self, const D3D12_COMPUTE_PIPELINE_STATE_DESC* d, void*, void*, REFIID riid, void** out) {
    D3D12_COMPUTE_PIPELINE_STATE_DESC d2 = *d;
    d2.CS.pShaderBytecode = g_dummy;
    d2.CS.BytecodeLength = sizeof(g_dummy);
    return ((ID3D12Device*)self->real)->CreateComputePipelineState(&d2, riid, out);
}
static long CallRehydrate(RehydrateFn f, void* obj, ID3D12Device11* dev, const D3D12_BUILD_RAYTRACING_ACCELERATION_STRUCTURE_DESC* bd) {
    __try { return f(obj, dev, bd); }
    __except (EXCEPTION_EXECUTE_HANDLER) { printf("rehydrate raised 0x%lx\n", GetExceptionCode()); return -1; }
}

// Allocate a default-heap UAV buffer and a CPU mapping at its GPU virtual address (rounded down to 64 KiB by VirtualAlloc).
struct Aliased {
    ComPtr<ID3D12Resource> res;
    void* base = nullptr;
    size_t off = 0;
    char* cpu() const { return (char*)base + off; }
};

static Aliased MakeAliased(ID3D12Device* dev, UINT64 bytes, bool zero) {
    D3D12_HEAP_PROPERTIES hp = {D3D12_HEAP_TYPE_DEFAULT};
    D3D12_RESOURCE_DESC rd = {D3D12_RESOURCE_DIMENSION_BUFFER, 0, bytes, 1, 1, 1, DXGI_FORMAT_UNKNOWN, {1, 0}, D3D12_TEXTURE_LAYOUT_ROW_MAJOR, D3D12_RESOURCE_FLAG_ALLOW_UNORDERED_ACCESS};
    Aliased a;
    for (int attempt = 0; attempt < 8; attempt++) {
        if (FAILED(dev->CreateCommittedResource(&hp, D3D12_HEAP_FLAG_NONE, &rd, D3D12_RESOURCE_STATE_COPY_DEST, nullptr, IID_PPV_ARGS(&a.res)))) return a;
        SIZE_T n = (SIZE_T)((bytes + 0xFFF) & ~0xFFFull);
        void* p = VirtualAlloc((void*)(uintptr_t)a.res->GetGPUVirtualAddress(), n, MEM_COMMIT | MEM_RESERVE, PAGE_READWRITE);
        if (p) {
            a.base = p;
            a.off = (size_t)(a.res->GetGPUVirtualAddress() - (UINT64)(uintptr_t)p);
            if (zero) memset(p, 0, n);
            return a;
        }
        a.res.Reset();
    }
    return a;
}

struct Geometry {
    std::vector<char> vb, ib;
    UINT stride = 12, count = 0, vfmt = 6, ifmt = 42;
    UINT64 vbOff = 0, ibOff = 0, voff = 0;   // voff: where the positions start inside the given vertex buffer
};

int main(int argc, char** argv) {
    setvbuf(stdout, nullptr, _IONBF, 0);
    bool multi = argc >= 4 && !strcmp(argv[3], "--geom");
    std::vector<Geometry> geoms;
    const char* srcPath = nullptr;
    const char* outPath = nullptr;
    if (multi) {
        srcPath = argv[1];
        outPath = argv[2];
        for (int i = 3; i < argc;) {
            if (strcmp(argv[i], "--geom") || i + 6 >= argc) {
                printf("--geom needs <vb> <index|-> <stride> <count> <vertexFormat> <indexFormat> [vertexByteOffset]\n");
                return 2;
            }
            Geometry g;
            g.vb = Read(argv[i + 1]);
            if (strcmp(argv[i + 2], "-")) g.ib = Read(argv[i + 2]);
            g.stride = (UINT)atoi(argv[i + 3]);
            g.count = (UINT)atoi(argv[i + 4]);
            g.vfmt = (UINT)atoi(argv[i + 5]);
            g.ifmt = (UINT)atoi(argv[i + 6]);
            int used = 7;
            if (i + 7 < argc && argv[i + 7][0] >= '0' && argv[i + 7][0] <= '9') {   // optional 7th value: positions start here
                g.voff = (UINT64)atoi(argv[i + 7]);
                used = 8;
            }
            geoms.push_back(std::move(g));
            i += used;
        }
        if (geoms.empty()) { printf("no --geom given\n"); return 2; }
    } else {
        if (argc < 4) {
            printf("usage: rehydrate_cpu <src.bin> <vb.bin> <out.bin> [stride=12] [vertexCount=vb/stride] [vertexFormat=6] [index.bin] [indexFormat=42]\n");
            printf("       rehydrate_cpu <src.bin> <out.bin> --geom <vb> <index|-> <stride> <count> <vertexFormat> <indexFormat> [vertexByteOffset] [--geom ...]\n");
            return 2;
        }
        srcPath = argv[1];
        outPath = argv[3];
        Geometry g;
        g.vb = Read(argv[2]);
        g.stride = argc > 4 ? (UINT)atoi(argv[4]) : 12;
        g.count = argc > 5 ? (UINT)atoi(argv[5]) : (UINT)(g.vb.size() / (g.stride ? g.stride : 1));
        g.vfmt = argc > 6 ? (UINT)atoi(argv[6]) : 6;
        if (argc > 7) g.ib = Read(argv[7]);
        g.ifmt = argc > 8 ? (UINT)atoi(argv[8]) : 42;
        geoms.push_back(std::move(g));
    }
    std::vector<char> src = Read(srcPath);
    if (src.size() < 64) { printf("source shorter than the 64-byte header\n"); return 2; }
    UINT64 total = ((UINT64)*(const UINT32*)(src.data() + 4) + 1) << 6;

    // data buffer: source at 0, then each geometry's vertex and index buffer at 4 KiB boundaries
    UINT64 cursor = (src.size() + 0xFFF) & ~(UINT64)0xFFF;
    for (auto& g : geoms) {
        g.vbOff = cursor;
        cursor += (g.vb.size() + 0xFFF) & ~(UINT64)0xFFF;
        if (!g.ib.empty()) {
            g.ibOff = cursor;
            cursor += (g.ib.size() + 0xFFF) & ~(UINT64)0xFFF;
        }
    }
    const UINT64 DATA_BYTES = cursor ? cursor : 0x1000;

    ComPtr<IDXGIFactory4> fac; CreateDXGIFactory1(IID_PPV_ARGS(&fac));
    ComPtr<IDXGIAdapter> ad; fac->EnumAdapters(0, &ad);
    ComPtr<ID3D12Device> dev;
    if (FAILED(D3D12CreateDevice(ad.Get(), D3D_FEATURE_LEVEL_12_0, IID_PPV_ARGS(&dev)))) { printf("no D3D12 device\n"); return 2; }
    HMODULE m = LoadLibraryA("XboxBvhBuilder.dll");
    if (!m) { printf("LoadLibrary XboxBvhBuilder.dll failed %lu\n", GetLastError()); return 2; }
    auto rehydrate = (RehydrateFn)GetProcAddress(m, REHYDRATE);
    auto validate = (ValidateFn)GetProcAddress(m, VALIDATE);
    if (!rehydrate) { printf("rehydrate export not found\n"); return 2; }

    Aliased data = MakeAliased(dev.Get(), DATA_BYTES, false), dst = MakeAliased(dev.Get(), total, true), scratch = MakeAliased(dev.Get(), 1 << 20, true);
    if (!data.res || !dst.res || !scratch.res || !data.base || !dst.base || !scratch.base) { printf("buffer/alias setup failed\n"); return 2; }
    memset(data.base, 0, (size_t)((DATA_BYTES + 0xFFF) & ~0xFFFull));
    memcpy(data.cpu(), src.data(), src.size());
    for (auto& g : geoms) {
        memcpy(data.cpu() + g.vbOff, g.vb.data(), g.vb.size());
        if (!g.ib.empty()) memcpy(data.cpu() + g.ibOff, g.ib.data(), g.ib.size());
    }

    // upload the blob into the GPU buffer at the same address
    ComPtr<ID3D12Resource> up;
    D3D12_HEAP_PROPERTIES uhp = {D3D12_HEAP_TYPE_UPLOAD};
    D3D12_RESOURCE_DESC urd = {D3D12_RESOURCE_DIMENSION_BUFFER, 0, DATA_BYTES, 1, 1, 1, DXGI_FORMAT_UNKNOWN, {1, 0}, D3D12_TEXTURE_LAYOUT_ROW_MAJOR, D3D12_RESOURCE_FLAG_NONE};
    dev->CreateCommittedResource(&uhp, D3D12_HEAP_FLAG_NONE, &urd, D3D12_RESOURCE_STATE_GENERIC_READ, nullptr, IID_PPV_ARGS(&up));
    { void* p = nullptr; if (SUCCEEDED(up->Map(0, nullptr, &p)) && p) { memcpy(p, data.cpu(), (size_t)DATA_BYTES); up->Unmap(0, nullptr); } }
    ComPtr<ID3D12CommandAllocator> al; dev->CreateCommandAllocator(D3D12_COMMAND_LIST_TYPE_DIRECT, IID_PPV_ARGS(&al));
    ComPtr<ID3D12GraphicsCommandList> cl; dev->CreateCommandList(0, D3D12_COMMAND_LIST_TYPE_DIRECT, al.Get(), nullptr, IID_PPV_ARGS(&cl));
    cl->CopyResource(data.res.Get(), up.Get());
    D3D12_RESOURCE_BARRIER b = {};
    b.Type = D3D12_RESOURCE_BARRIER_TYPE_TRANSITION; b.Transition.pResource = data.res.Get();
    b.Transition.StateBefore = D3D12_RESOURCE_STATE_COPY_DEST; b.Transition.StateAfter = D3D12_RESOURCE_STATE_UNORDERED_ACCESS;
    b.Transition.Subresource = 0xffffffff; cl->ResourceBarrier(1, &b);
    cl->Close();
    ComPtr<ID3D12CommandQueue> q; D3D12_COMMAND_QUEUE_DESC qd = {}; dev->CreateCommandQueue(&qd, IID_PPV_ARGS(&q));
    ID3D12CommandList* lists[] = {cl.Get()}; q->ExecuteCommandLists(1, lists);
    ComPtr<ID3D12Fence> f; dev->CreateFence(0, D3D12_FENCE_FLAG_NONE, IID_PPV_ARGS(&f)); q->Signal(f.Get(), 1);
    HANDLE ev = CreateEvent(0, 0, 0, 0); f->SetEventOnCompletion(1, ev); WaitForSingleObject(ev, 30000);

    std::vector<D3D12_RAYTRACING_GEOMETRY_DESC> descs(geoms.size());
    for (size_t i = 0; i < geoms.size(); i++) {
        D3D12_RAYTRACING_GEOMETRY_DESC& g = descs[i];
        const Geometry& s = geoms[i];
        g.Type = D3D12_RAYTRACING_GEOMETRY_TYPE_TRIANGLES;
        g.Triangles.VertexFormat = (DXGI_FORMAT)s.vfmt;
        g.Triangles.VertexCount = s.count;
        g.Triangles.VertexBuffer.StartAddress = data.res->GetGPUVirtualAddress() + s.vbOff + s.voff;
        g.Triangles.VertexBuffer.StrideInBytes = s.stride;
        g.Triangles.IndexFormat = s.ib.empty() ? DXGI_FORMAT_UNKNOWN : (DXGI_FORMAT)s.ifmt;
        if (!s.ib.empty()) {
            g.Triangles.IndexBuffer = data.res->GetGPUVirtualAddress() + s.ibOff;
            g.Triangles.IndexCount = (UINT)(s.ib.size() / ((s.ifmt == 57) ? 2 : 4));
        }
    }
    D3D12_BUILD_RAYTRACING_ACCELERATION_STRUCTURE_DESC bd = {};
    bd.Inputs.Type = D3D12_RAYTRACING_ACCELERATION_STRUCTURE_TYPE_BOTTOM_LEVEL;
    bd.Inputs.Flags = D3D12_RAYTRACING_ACCELERATION_STRUCTURE_BUILD_FLAG_PREFER_FAST_TRACE;
    bd.Inputs.DescsLayout = D3D12_ELEMENTS_LAYOUT_ARRAY;
    bd.Inputs.NumDescs = (UINT)descs.size();
    bd.Inputs.pGeometryDescs = descs.data();
    // The library was built with the Xbox SDK; if its D3D12_RAYTRACING_GEOMETRY_DESC is padded differently from the PC one, the
    // descriptors have to be repacked with that stride. REHYDRATE_GEOM_STRIDE=<bytes> is the probe for it.
    std::vector<unsigned char> packed;
    if (const char* st = getenv("REHYDRATE_GEOM_STRIDE")) {
        size_t stride = (size_t)atoi(st);
        if (stride >= sizeof(D3D12_RAYTRACING_GEOMETRY_DESC) && descs.size() > 1) {
            packed.assign(descs.size() * stride, 0);
            for (size_t i = 0; i < descs.size(); i++) memcpy(packed.data() + i * stride, &descs[i], sizeof(descs[i]));
            bd.Inputs.pGeometryDescs = reinterpret_cast<const D3D12_RAYTRACING_GEOMETRY_DESC*>(packed.data());
            printf("geometry descriptors packed with stride %zu (PC sizeof %zu)\n", stride, sizeof(D3D12_RAYTRACING_GEOMETRY_DESC));
        }
    }
    bd.SourceAccelerationStructureData = data.res->GetGPUVirtualAddress();
    bd.DestAccelerationStructureData = dst.res->GetGPUVirtualAddress();
    bd.ScratchAccelerationStructureData = scratch.res->GetGPUVirtualAddress();

    // the builder object is opaque; allocate it oversized and zeroed like bvh_test does
    void* obj = _aligned_malloc(1 << 20, 64);
    memset(obj, 0, 1 << 20);
    auto ctor = (void* (__fastcall*)(void*, ID3D12Device*, bool))GetProcAddress(m, "??0XboxBvhBuilder@@QEAA@PEAUID3D12Device11@@_N@Z");
    auto dtor = (void(__fastcall*)(void*))GetProcAddress(m, "??1XboxBvhBuilder@@QEAA@XZ");
    if (!ctor) { printf("constructor export not found\n"); return 2; }
    XboxObjectProxy* proxy = XboxObjectProxy::Create(dev.Get(), "device");
    proxy->Override(62, (void*)DummyPso);
    ctor(obj, (ID3D12Device*)proxy, false);
    long vh = validate ? validate(obj, &bd) : -1;
    long rh = CallRehydrate(rehydrate, obj, (ID3D12Device11*)proxy, &bd);
    if (dtor) dtor(obj);
    printf("validate 0x%08lx, rehydrate 0x%08lx, %llu bytes, %zu geometries\n", (unsigned long)vh, (unsigned long)rh,
           (unsigned long long)total, geoms.size());
    if (rh != 0) return 1;
    std::ofstream out(outPath, std::ios::binary);
    out.write(dst.cpu(), (std::streamsize)total);
    printf("wrote %s (%llu bytes)\n", outPath, (unsigned long long)total);
    return 0;
}
