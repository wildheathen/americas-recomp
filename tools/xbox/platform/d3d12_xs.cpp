// d3d12_xs.dll stand-in. The package's DLLs import exactly two functions from it (XboxBvhBuilder: D3D12SerializeVersionedRootSignature,
// amd_fidelityfx_upscaler_xbox: D3D12CreateRootSignatureDeserializer) and the PC d3d12.dll exports both with the same signature, so each
// call is forwarded. (A .def forwarder would be simpler, but link.exe read it as an unresolved symbol, so this resolves d3d12.dll at run time.)
// The game executable will import far more from d3d12_xs (Xbox device creation and the XBOX command-list extensions): not known until it exists.
#include <windows.h>
#include <cstdio>
#include <cstring>
#include <cstdarg>
static FARPROC Pc(const char* name) {
    static HMODULE m = LoadLibraryW(L"d3d12.dll");
    return m ? GetProcAddress(m, name) : nullptr;
}
extern "C" {
// XS_LOG=<file>: append the arguments' first bytes, the result and the runtime's error text for every serialize call
static void Log(const char* fmt, ...) {
    char path[MAX_PATH];
    if (!GetEnvironmentVariableA("XS_LOG", path, MAX_PATH)) return;
    FILE* f = nullptr;
    if (fopen_s(&f, path, "a")) return;
    va_list a; va_start(a, fmt); vfprintf(f, fmt, a); va_end(a);
    fclose(f);
}
__declspec(dllexport) HRESULT WINAPI D3D12SerializeVersionedRootSignature(const void* desc, void** blob, void** error) {
    using Fn = HRESULT(WINAPI*)(const void*, void**, void**);
    auto f = reinterpret_cast<Fn>(Pc("D3D12SerializeVersionedRootSignature"));
    // The Xbox root signature flags carry bits the PC runtime does not know (the BVH builder sets 0x3000000 on every one of its
    // signatures and the PC serializer then fails with E_INVALIDARG). The PC flags all live in the low 16 bits, so the high byte is
    // cleared on a copy of the descriptor header (Flags is the dword at offset 40 for versions 1.0, 1.1 and 1.2). What the bits mean is
    // not known: this only lets the signature be created. XS_KEEP_XBOX_FLAGS=1 forwards the descriptor unchanged.
    unsigned char copy[48];
    const void* use = desc;
    unsigned stripped = 0, added = 0;
    if (desc && !GetEnvironmentVariableA("XS_KEEP_XBOX_FLAGS", nullptr, 0)) {
        memcpy(copy, desc, sizeof(copy));
        unsigned* fl = (unsigned*)(copy + 40);
        stripped = *fl & 0xff000000u;
        *fl &= 0x00ffffffu;
        if (GetEnvironmentVariableA("XS_HEAP", nullptr, 0)) {   // let the shaders use ResourceDescriptorHeap (SM 6.6 address lowering)
            added = 0x10u  /* CBV_SRV_UAV_HEAP_DIRECTLY_INDEXED */ & ~*fl;
            *fl |= 0x10u  /* CBV_SRV_UAV_HEAP_DIRECTLY_INDEXED */;
        }
        use = copy;
    }
    HRESULT hr = f ? f(use, blob, error) : E_NOTIMPL;
    if (stripped) Log("  (cleared Xbox-only root signature flag bits 0x%08x)\n", stripped);
    if (added) Log("  (added heap-indexed flag 0x%08x)\n", added);
    if (FAILED(hr) || GetEnvironmentVariableA("XS_LOG", nullptr, 0)) {
        const unsigned* d = (const unsigned*)desc;
        Log("D3D12SerializeVersionedRootSignature: version %u, hr 0x%08lx\n", d[0], (unsigned long)hr);
        if (d[0] == 2) {   // 1.1 layout, read raw (this file does not include d3d12.h): NumParameters @8, pParameters @16, NumStaticSamplers @24, Flags @40
            unsigned n = d[2];
            const unsigned char* ps = *(const unsigned char* const*)((const char*)desc + 16);
            Log("  params %u, static samplers %u, flags 0x%x\n", n, d[6], d[10]);
            for (unsigned i = 0; i < n && i < 16; ++i) {
                const unsigned* p = (const unsigned*)(ps + 32 * i);
                Log("  [%u] type %u  words: %08x %08x %08x %08x %08x %08x %08x %08x\n", i, p[0], p[0], p[1], p[2], p[3], p[4], p[5], p[6], p[7]);
            }
        }
        if (FAILED(hr) && error && *error) {
            struct Blob : IUnknown { virtual LPVOID STDMETHODCALLTYPE GetBufferPointer() = 0; virtual SIZE_T STDMETHODCALLTYPE GetBufferSize() = 0; };
            auto* b = (Blob*)*error;
            Log("  runtime says: %.*s\n", (int)b->GetBufferSize(), (const char*)b->GetBufferPointer());
        }
    }
    return hr;
}
__declspec(dllexport) HRESULT WINAPI D3D12CreateRootSignatureDeserializer(const void* data, SIZE_T size, const GUID& iid, void** out) {
    using Fn = HRESULT(WINAPI*)(const void*, SIZE_T, const GUID&, void**);
    auto f = reinterpret_cast<Fn>(Pc("D3D12CreateRootSignatureDeserializer"));
    return f ? f(data, size, iid, out) : E_NOTIMPL;
}
// Not imported by the package's DLLs, but the same family a game links against: forwarded on the same terms.
__declspec(dllexport) HRESULT WINAPI D3D12CreateVersionedRootSignatureDeserializer(const void* data, SIZE_T size, const GUID& iid, void** out) {
    using Fn = HRESULT(WINAPI*)(const void*, SIZE_T, const GUID&, void**);
    auto f = reinterpret_cast<Fn>(Pc("D3D12CreateVersionedRootSignatureDeserializer"));
    return f ? f(data, size, iid, out) : E_NOTIMPL;
}
// Version 1.0 layout (D3D12_ROOT_SIGNATURE_DESC): NumParameters @0, pParameters @8, NumStaticSamplers @16, pStaticSamplers @24, Flags @32.
// The Xbox-only high flag byte is cleared on a copy, as in the versioned call above (same guess about what the bits mean).
__declspec(dllexport) HRESULT WINAPI D3D12SerializeRootSignature(const void* desc, int version, void** blob, void** error) {
    using Fn = HRESULT(WINAPI*)(const void*, int, void**, void**);
    auto f = reinterpret_cast<Fn>(Pc("D3D12SerializeRootSignature"));
    unsigned char copy[40];
    const void* use = desc;
    if (desc && !GetEnvironmentVariableA("XS_KEEP_XBOX_FLAGS", nullptr, 0)) {
        memcpy(copy, desc, sizeof(copy));
        *(unsigned*)(copy + 32) &= 0x00ffffffu;
        if (GetEnvironmentVariableA("XS_HEAP", nullptr, 0))
            *(unsigned*)(copy + 32) |= 0x10u  /* CBV_SRV_UAV_HEAP_DIRECTLY_INDEXED */;
        use = copy;
    }
    return f ? f(use, version, blob, error) : E_NOTIMPL;
}
}
