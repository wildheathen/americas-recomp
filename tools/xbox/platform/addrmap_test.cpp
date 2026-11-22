// addrmap_test: run a lowered kernel whose 64-bit addresses are resolved through a descriptor-heap table (slot 0 = table,
// slot 1 = dummy, slot 2+ = resources) and compare every output word with a CPU model, on the GPU and on WARP.
// The input buffer holds {u64 load address, u64 store/atomic address} per thread; each thread loads one dword and one
// dwordx4 from the load address, scalar-loads the same word, stores 0xDEADBEEF at the target, then adds 7 and subtracts 5
// there. Addresses in no table entry must read 0 and leave the targets untouched.
// build: cl /nologo /EHsc /O2 /std:c++17 addrmap_test.cpp d3d12.lib dxgi.lib /Fe:addrmap_test.exe
// usage: addrmap_test shader.dxil [groups]
#include <windows.h>
#include <d3d12.h>
#include <dxgi1_4.h>
#include <wrl/client.h>
#include <cstdio>
#include <cstdint>
#include <cstdlib>
#include <vector>
#include <fstream>
#include <iterator>
using Microsoft::WRL::ComPtr;

static const uint64_t BASE_A = 0x100000000ull, BASE_B = 0x200000000ull, BASE_C = 0x300000000ull, UNMAPPED = 0x400000000ull;
static const UINT BUF = 4096, THREADS = 64, OUT_WORDS = 8;

struct Model { std::vector<uint32_t> out, c; };

static Model Expect(UINT threads, const std::vector<uint32_t>& a, const std::vector<uint32_t>& b, const std::vector<uint8_t>& addrs) {
    Model m; m.out.resize(threads * OUT_WORDS, 0xffffffffu); m.c.assign(BUF / 4, 0);
    for (UINT t = 0; t < threads; t++) {
        uint64_t al, at;
        memcpy(&al, addrs.data() + t * 16, 8); memcpy(&at, addrs.data() + t * 16 + 8, 8);
        uint32_t* o = &m.out[t * OUT_WORDS];
        const std::vector<uint32_t>* src = nullptr; uint64_t off = 0;
        if (al >= BASE_A && al < BASE_A + BUF) { src = &a; off = al - BASE_A; }
        else if (al >= BASE_B && al < BASE_B + BUF) { src = &b; off = al - BASE_B; }
        for (int k = 0; k < 8; k++) o[k] = 0;
        if (src) {
            o[0] = (*src)[off / 4];                          // global_load_dword
            o[1] = (*src)[off / 4];                          // s_load_dword
            for (int k = 0; k < 4; k++) o[4 + k] = (*src)[off / 4 + k];
        }
        if (at >= BASE_C && at < BASE_C + BUF) {
            m.c[(at - BASE_C) / 4] = 0xDEADBEEFu + 7 - 5;
            o[2] = 0xDEADBEEFu;                              // atomic add returns the stored value
            o[3] = 0xDEADBEEFu + 7;                          // atomic sub returns the value after the add
        }
    }
    return m;
}

static int Run(bool warp, const std::vector<char>& dxil, UINT groups, char* name) {
    UINT threads = groups * THREADS;
    ComPtr<IDXGIFactory4> fac; CreateDXGIFactory1(IID_PPV_ARGS(&fac));
    ComPtr<IDXGIAdapter> ad;
    if (warp) fac->EnumWarpAdapter(IID_PPV_ARGS(&ad)); else fac->EnumAdapters(0, &ad);
    DXGI_ADAPTER_DESC dd; ad->GetDesc(&dd);
    WideCharToMultiByte(CP_ACP, 0, dd.Description, -1, name, 128, 0, 0);
    ComPtr<ID3D12Device> dev;
    if (FAILED(D3D12CreateDevice(ad.Get(), D3D_FEATURE_LEVEL_12_0, IID_PPV_ARGS(&dev)))) { printf("  no device\n"); return 1; }

    D3D12_ROOT_PARAMETER rp[2] = {};
    rp[0].ParameterType = D3D12_ROOT_PARAMETER_TYPE_UAV; rp[0].Descriptor = {0, 0};
    rp[1].ParameterType = D3D12_ROOT_PARAMETER_TYPE_UAV; rp[1].Descriptor = {1, 0};
    D3D12_ROOT_SIGNATURE_DESC rs = {2, rp, 0, nullptr, D3D12_ROOT_SIGNATURE_FLAG_CBV_SRV_UAV_HEAP_DIRECTLY_INDEXED};
    ComPtr<ID3DBlob> sb, eb; D3D12SerializeRootSignature(&rs, D3D_ROOT_SIGNATURE_VERSION_1, &sb, &eb);
    ComPtr<ID3D12RootSignature> sig;
    if (FAILED(dev->CreateRootSignature(0, sb->GetBufferPointer(), sb->GetBufferSize(), IID_PPV_ARGS(&sig)))) { printf("  root signature failed\n"); return 1; }
    D3D12_COMPUTE_PIPELINE_STATE_DESC pd = {}; pd.pRootSignature = sig.Get(); pd.CS = {dxil.data(), dxil.size()};
    ComPtr<ID3D12PipelineState> pso;
    HRESULT hr = dev->CreateComputePipelineState(&pd, IID_PPV_ARGS(&pso));
    if (FAILED(hr)) { printf("  pipeline creation failed 0x%08x\n", (unsigned)hr); return 1; }

    auto mk = [&](UINT bytes, D3D12_HEAP_TYPE ht, D3D12_RESOURCE_FLAGS fl, D3D12_RESOURCE_STATES st) {
        D3D12_HEAP_PROPERTIES hp = {ht}; D3D12_RESOURCE_DESC rd = {D3D12_RESOURCE_DIMENSION_BUFFER, 0, bytes, 1, 1, 1, DXGI_FORMAT_UNKNOWN, {1, 0}, D3D12_TEXTURE_LAYOUT_ROW_MAJOR, fl};
        ComPtr<ID3D12Resource> r; dev->CreateCommittedResource(&hp, D3D12_HEAP_FLAG_NONE, &rd, st, nullptr, IID_PPV_ARGS(&r)); return r;
    };
    UINT addrsBytes = threads * 16, outBytes = threads * OUT_WORDS * 4;
    auto addrs = mk(addrsBytes, D3D12_HEAP_TYPE_DEFAULT, D3D12_RESOURCE_FLAG_ALLOW_UNORDERED_ACCESS, D3D12_RESOURCE_STATE_COPY_DEST);
    auto out = mk(outBytes, D3D12_HEAP_TYPE_DEFAULT, D3D12_RESOURCE_FLAG_ALLOW_UNORDERED_ACCESS, D3D12_RESOURCE_STATE_COPY_DEST);
    auto table = mk(512, D3D12_HEAP_TYPE_DEFAULT, D3D12_RESOURCE_FLAG_ALLOW_UNORDERED_ACCESS, D3D12_RESOURCE_STATE_COPY_DEST);
    auto dummy = mk(256, D3D12_HEAP_TYPE_DEFAULT, D3D12_RESOURCE_FLAG_ALLOW_UNORDERED_ACCESS, D3D12_RESOURCE_STATE_COPY_DEST);
    auto a = mk(BUF, D3D12_HEAP_TYPE_DEFAULT, D3D12_RESOURCE_FLAG_ALLOW_UNORDERED_ACCESS, D3D12_RESOURCE_STATE_COPY_DEST);
    auto b = mk(BUF, D3D12_HEAP_TYPE_DEFAULT, D3D12_RESOURCE_FLAG_ALLOW_UNORDERED_ACCESS, D3D12_RESOURCE_STATE_COPY_DEST);
    auto c = mk(BUF, D3D12_HEAP_TYPE_DEFAULT, D3D12_RESOURCE_FLAG_ALLOW_UNORDERED_ACCESS, D3D12_RESOURCE_STATE_COPY_DEST);
    auto upA = mk(addrsBytes, D3D12_HEAP_TYPE_UPLOAD, D3D12_RESOURCE_FLAG_NONE, D3D12_RESOURCE_STATE_GENERIC_READ);
    auto upO = mk(outBytes, D3D12_HEAP_TYPE_UPLOAD, D3D12_RESOURCE_FLAG_NONE, D3D12_RESOURCE_STATE_GENERIC_READ);
    auto upT = mk(512, D3D12_HEAP_TYPE_UPLOAD, D3D12_RESOURCE_FLAG_NONE, D3D12_RESOURCE_STATE_GENERIC_READ);
    auto upA2 = mk(BUF, D3D12_HEAP_TYPE_UPLOAD, D3D12_RESOURCE_FLAG_NONE, D3D12_RESOURCE_STATE_GENERIC_READ);
    auto upB = mk(BUF, D3D12_HEAP_TYPE_UPLOAD, D3D12_RESOURCE_FLAG_NONE, D3D12_RESOURCE_STATE_GENERIC_READ);
    auto upC = mk(BUF, D3D12_HEAP_TYPE_UPLOAD, D3D12_RESOURCE_FLAG_NONE, D3D12_RESOURCE_STATE_GENERIC_READ);
    auto rbO = mk(outBytes, D3D12_HEAP_TYPE_READBACK, D3D12_RESOURCE_FLAG_NONE, D3D12_RESOURCE_STATE_COPY_DEST);
    auto rbC = mk(BUF, D3D12_HEAP_TYPE_READBACK, D3D12_RESOURCE_FLAG_NONE, D3D12_RESOURCE_STATE_COPY_DEST);

    std::vector<uint32_t> av(BUF / 4), bv(BUF / 4);
    for (UINT i = 0; i < BUF / 4; i++) { av[i] = 0x10000000u + i; bv[i] = 0x20000000u + i; }
    std::vector<uint8_t> addrBytes(addrsBytes, 0);
    for (UINT t = 0; t < threads; t++) {
        uint64_t al = (t % 3 == 0) ? BASE_A + (t * 16) % (BUF - 16)
                    : (t % 3 == 1) ? BASE_B + (t * 16) % (BUF - 16) : UNMAPPED;
        uint64_t at = BASE_C + t * 16;
        memcpy(addrBytes.data() + t * 16, &al, 8); memcpy(addrBytes.data() + t * 16 + 8, &at, 8);
    }
    { uint64_t e[16 * 3] = {}; for (int i = 0; i < 16; i++) { e[i * 3] = ~0ull; e[i * 3 + 1] = 0; e[i * 3 + 2] = 1; }
      e[0] = BASE_A; e[1] = BUF; e[2] = 2; e[3] = BASE_B; e[4] = BUF; e[5] = 3; e[6] = BASE_C; e[7] = BUF; e[8] = 4;
      void* p; upT->Map(0, nullptr, &p); memcpy(p, e, sizeof(e)); upT->Unmap(0, nullptr); }
    { void* p; upA->Map(0, nullptr, &p); memcpy(p, addrBytes.data(), addrsBytes); upA->Unmap(0, nullptr); }
    { void* p; upO->Map(0, nullptr, &p); memset(p, 0xCD, outBytes); upO->Unmap(0, nullptr); }
    { void* p; upA2->Map(0, nullptr, &p); memcpy(p, av.data(), BUF); upA2->Unmap(0, nullptr); }
    { void* p; upB->Map(0, nullptr, &p); memcpy(p, bv.data(), BUF); upB->Unmap(0, nullptr); }
    { void* p; upC->Map(0, nullptr, &p); memset(p, 0, BUF); upC->Unmap(0, nullptr); }

    D3D12_DESCRIPTOR_HEAP_DESC hd = {D3D12_DESCRIPTOR_HEAP_TYPE_CBV_SRV_UAV, 5, D3D12_DESCRIPTOR_HEAP_FLAG_SHADER_VISIBLE};
    ComPtr<ID3D12DescriptorHeap> heap; dev->CreateDescriptorHeap(&hd, IID_PPV_ARGS(&heap));
    ID3D12Resource* resources[5] = {table.Get(), dummy.Get(), a.Get(), b.Get(), c.Get()};
    UINT sizes[5] = {512, 256, BUF, BUF, BUF};
    UINT inc = dev->GetDescriptorHandleIncrementSize(D3D12_DESCRIPTOR_HEAP_TYPE_CBV_SRV_UAV);
    for (int i = 0; i < 5; i++) {
        D3D12_UNORDERED_ACCESS_VIEW_DESC uv = {}; uv.Format = DXGI_FORMAT_R32_TYPELESS; uv.ViewDimension = D3D12_UAV_DIMENSION_BUFFER;
        uv.Buffer.NumElements = sizes[i] / 4; uv.Buffer.Flags = D3D12_BUFFER_UAV_FLAG_RAW;
        D3D12_CPU_DESCRIPTOR_HANDLE h = heap->GetCPUDescriptorHandleForHeapStart(); h.ptr += (SIZE_T)i * inc;
        dev->CreateUnorderedAccessView(resources[i], nullptr, &uv, h);
    }

    ComPtr<ID3D12CommandAllocator> al; dev->CreateCommandAllocator(D3D12_COMMAND_LIST_TYPE_DIRECT, IID_PPV_ARGS(&al));
    ComPtr<ID3D12GraphicsCommandList> cl; dev->CreateCommandList(0, D3D12_COMMAND_LIST_TYPE_DIRECT, al.Get(), nullptr, IID_PPV_ARGS(&cl));
    cl->CopyResource(addrs.Get(), upA.Get()); cl->CopyResource(out.Get(), upO.Get()); cl->CopyResource(table.Get(), upT.Get());
    cl->CopyResource(a.Get(), upA2.Get()); cl->CopyResource(b.Get(), upB.Get()); cl->CopyResource(c.Get(), upC.Get());
    ID3D12Resource* targets[7] = {addrs.Get(), out.Get(), table.Get(), dummy.Get(), a.Get(), b.Get(), c.Get()};
    for (int i = 0; i < 7; i++) {
        D3D12_RESOURCE_BARRIER br = {}; br.Type = D3D12_RESOURCE_BARRIER_TYPE_TRANSITION; br.Transition.pResource = targets[i];
        br.Transition.StateBefore = D3D12_RESOURCE_STATE_COPY_DEST; br.Transition.StateAfter = D3D12_RESOURCE_STATE_UNORDERED_ACCESS;
        br.Transition.Subresource = 0xffffffff; cl->ResourceBarrier(1, &br);
    }
    cl->SetComputeRootSignature(sig.Get()); cl->SetPipelineState(pso.Get());
    cl->SetComputeRootUnorderedAccessView(0, addrs->GetGPUVirtualAddress());
    cl->SetComputeRootUnorderedAccessView(1, out->GetGPUVirtualAddress());
    ID3D12DescriptorHeap* hs[] = {heap.Get()}; cl->SetDescriptorHeaps(1, hs);
    cl->Dispatch(groups, 1, 1);
    D3D12_RESOURCE_BARRIER post[2] = {};
    for (int i = 0; i < 2; i++) { D3D12_RESOURCE_BARRIER& br = post[i]; br.Type = D3D12_RESOURCE_BARRIER_TYPE_TRANSITION;
        br.Transition.pResource = i ? c.Get() : out.Get(); br.Transition.StateBefore = D3D12_RESOURCE_STATE_UNORDERED_ACCESS;
        br.Transition.StateAfter = D3D12_RESOURCE_STATE_COPY_SOURCE; br.Transition.Subresource = 0xffffffff; }
    cl->ResourceBarrier(2, post);
    cl->CopyResource(rbO.Get(), out.Get()); cl->CopyResource(rbC.Get(), c.Get());
    cl->Close();
    ComPtr<ID3D12CommandQueue> q; D3D12_COMMAND_QUEUE_DESC qd = {}; dev->CreateCommandQueue(&qd, IID_PPV_ARGS(&q));
    ID3D12CommandList* lists[] = {cl.Get()}; q->ExecuteCommandLists(1, lists);
    ComPtr<ID3D12Fence> f; dev->CreateFence(0, D3D12_FENCE_FLAG_NONE, IID_PPV_ARGS(&f)); q->Signal(f.Get(), 1);
    HANDLE ev = CreateEvent(0, 0, 0, 0); f->SetEventOnCompletion(1, ev);
    if (WaitForSingleObject(ev, 60000) != WAIT_OBJECT_0) { printf("  dispatch timed out\n"); return 1; }
    if (FAILED(dev->GetDeviceRemovedReason())) { printf("  device removed 0x%08x\n", (unsigned)dev->GetDeviceRemovedReason()); return 1; }

    Model m = Expect(threads, av, bv, addrBytes);
    std::vector<uint32_t> got(outBytes / 4), gotC(BUF / 4);
    { void* p; rbO->Map(0, nullptr, &p); memcpy(got.data(), p, outBytes); rbO->Unmap(0, nullptr); }
    { void* p; rbC->Map(0, nullptr, &p); memcpy(gotC.data(), p, BUF); rbC->Unmap(0, nullptr); }
    size_t bad = 0, shown = 0, badC = 0;
    for (size_t i = 0; i < got.size(); i++) if (got[i] != m.out[i]) { if (shown++ < 4) printf("  word %zu: got %08x expected %08x\n", i, got[i], m.out[i]); bad++; }
    for (size_t i = 0; i < gotC.size(); i++) if (gotC[i] != m.c[i]) { if (shown++ < 8) printf("  target word %zu: got %08x expected %08x\n", i, gotC[i], m.c[i]); badC++; }
    printf("  %-40s %zu/%zu output words and %zu/%zu target words differ\n", name, bad, got.size(), badC, gotC.size());
    return (bad || badC) ? 1 : 0;
}

int main(int argc, char** argv) {
    setvbuf(stdout, nullptr, _IONBF, 0);
    if (argc < 2) { printf("usage: addrmap_test shader.dxil [groups]\n"); return 2; }
    std::ifstream f(argv[1], std::ios::binary); std::vector<char> dxil((std::istreambuf_iterator<char>(f)), {});
    if (dxil.empty()) { printf("cannot read %s\n", argv[1]); return 2; }
    UINT groups = argc > 2 ? atoi(argv[2]) : 4;
    char name[2][128] = {};
    int a = Run(false, dxil, groups, name[0]), b = Run(true, dxil, groups, name[1]);
    printf("%s\n", (a || b) ? "FAIL" : "PASS");
    return a || b;
}
