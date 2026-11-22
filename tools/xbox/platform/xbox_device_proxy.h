// Stand-in for an Xbox COM object (device, descriptor heap, ...): an object whose vtable mirrors the PC device's, except for slots where the Xbox layout is known
// to differ. Every slot starts as a generated thunk (mov rcx,[rcx+8]; mov rax,[rcx]; jmp [rax+slot*8]) that forwards to the same slot of
// the real PC device with `this` replaced; override(slot, fn) installs a C++ function that receives the proxy as `this` (the real device
// is proxy->real). This is a first step of the "D3D12.X runtime" row: which slots differ is learned from the DLLs' own device calls
// (docs/xbox/PLATFORM_SURFACE.md), so the table of overrides grows with what the game executable is seen to use.
#pragma once
#include <windows.h>
#include <d3d12.h>
#include <cstdio>
#include <cstring>

struct XboxObjectProxy {
    void** vtbl;                 // must stay the first member: the callee dereferences `this`
    IUnknown* real;              // second member: the thunks load it from this+8
    void* slots[160];
    void* thunks;
    unsigned long long hits[160];  // how often each forwarded slot was called (overridden slots are not counted)
    const char* name;              // what this object is, for the trace
    // XS_TRACE=1: the first call of every forwarded slot is logged to stderr with its argument registers and caller, so a game's use of the
    // Xbox-only vtable slots is discovered from one run (rdx/r8/r9 that look like pointers are pointer arguments, small values are flags or counts).
    static void __stdcall LogFirstHit(unsigned slot, unsigned long long* regs) {   // regs: r9, r8, rdx, rcx(this), slot, return address
        static const bool on = GetEnvironmentVariableA("XS_TRACE", nullptr, 0) != 0;
        if (!on) return;
        auto* self = (XboxObjectProxy*)regs[3];
        fprintf(stderr, "XS %s slot %u first call: rdx=%llx r8=%llx r9=%llx caller=%llx\n", self->name ? self->name : "object", slot, regs[2], regs[1], regs[0], regs[5]);
    }
    // QueryInterface returns this proxy when the real object answers with itself, so a caller that queries for a newer interface
    // (ID3D12Device11, ID3D12GraphicsCommandList8, ...) keeps going through the overrides instead of getting the real object.
    // Interfaces implemented by another object (an info queue, for example) are passed through unchanged.
    static HRESULT __fastcall QueryInterfaceProxy(XboxObjectProxy* self, REFIID riid, void** out) {
        if (!out) return E_POINTER;
        *out = nullptr;
        HRESULT hr = ((IUnknown*)self->real)->QueryInterface(riid, out);
        if (SUCCEEDED(hr) && *out == (void*)self->real) {
            ((IUnknown*)self)->AddRef();
            *out = self;
        }
        return hr;
    }
    static XboxObjectProxy* Create(IUnknown* real, const char* name = "object") {
        auto* p = (XboxObjectProxy*)VirtualAlloc(nullptr, sizeof(XboxObjectProxy), MEM_COMMIT | MEM_RESERVE, PAGE_READWRITE);
        p->real = real;
        p->name = name;
        p->vtbl = p->slots;
        const int stride = 64, commonAt = 160 * stride;
        auto* code = (unsigned char*)VirtualAlloc(nullptr, commonAt + 128, MEM_COMMIT | MEM_RESERVE, PAGE_EXECUTE_READWRITE);
        p->thunks = code;
        // common first-hit path: save the argument registers, call LogFirstHit(slot in eax, &saved), restore, then forward through slot r10
        unsigned char* c = code + commonAt;
        const unsigned char c1[] = { 0x50, 0x51, 0x52, 0x41, 0x50, 0x41, 0x51,   // push rax, rcx, rdx, r8, r9
                                     0x48, 0x89, 0xe2,                           // mov rdx, rsp
                                     0x89, 0xc1,                                 // mov ecx, eax
                                     0x48, 0x83, 0xec, 0x20,                     // sub rsp, 0x20
                                     0x48, 0xb8 };                               // mov rax, imm64
        memcpy(c, c1, sizeof(c1)); c += sizeof(c1);
        void* logfn = (void*)&XboxObjectProxy::LogFirstHit;
        memcpy(c, &logfn, 8); c += 8;
        const unsigned char c2[] = { 0xff, 0xd0,                                 // call rax
                                     0x48, 0x83, 0xc4, 0x20,                     // add rsp, 0x20
                                     0x41, 0x59, 0x41, 0x58, 0x5a, 0x59,         // pop r9, r8, rdx, rcx
                                     0x41, 0x5a,                                 // pop r10 (the slot)
                                     0x48, 0x8b, 0x49, 0x08,                     // mov rcx, [rcx+8]
                                     0x48, 0x8b, 0x01,                           // mov rax, [rcx]
                                     0x42, 0xff, 0x24, 0xd0 };                   // jmp [rax+r10*8]
        memcpy(c, c2, sizeof(c2));
        for (int i = 0; i < 160; ++i) {
            unsigned char* t = code + i * stride; unsigned char* t0 = t;
            *t++ = 0x48; *t++ = 0xb8;                                   // mov rax, &hits[i]
            unsigned long long a = (unsigned long long)&p->hits[i];
            memcpy(t, &a, 8); t += 8;
            const unsigned char x[] = { 0x41, 0xba, 1, 0, 0, 0,                // mov r10d, 1
                                        0xf0, 0x4c, 0x0f, 0xc1, 0x10,          // lock xadd [rax], r10   (r10 = old count)
                                        0x4d, 0x85, 0xd2,                      // test r10, r10
                                        0x75, 0x0a,                            // jnz forward (skip the next 10 bytes)
                                        0xb8 };                                // mov eax, imm32 (slot)
            memcpy(t, x, sizeof(x)); t += sizeof(x);
            *(int*)t = i; t += 4;
            *t++ = 0xe9;                                                // jmp common
            *(int*)t = (int)((code + commonAt) - (t + 4)); t += 4;
            const unsigned char head[] = { 0x48, 0x8b, 0x49, 0x08,        // forward: mov rcx, [rcx+8]   (real object)
                                           0x48, 0x8b, 0x01,              // mov rax, [rcx]              (its vtable)
                                           0xff, 0xa0 };                  // jmp [rax+disp32]
            memcpy(t, head, sizeof(head));
            *(int*)(t + sizeof(head)) = i * 8;
            (void)t0;
            p->slots[i] = code + i * stride;
        }
        p->slots[0] = (void*)&XboxObjectProxy::QueryInterfaceProxy;   // keep newer-interface callers on the proxy
        return p;
    }
    void PrintHits(const char* what) {
        printf("  %s: forwarded slots called:", what);
        for (int i = 0; i < 160; ++i) if (hits[i]) printf(" %d(x%llu)", i, hits[i]);
        printf("\n");
    }
    void Override(int slot, void* fn) { slots[slot] = fn; }
};

typedef XboxObjectProxy XboxDeviceProxy;
