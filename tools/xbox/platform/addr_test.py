"""Build the address-mapping test kernel and run it: lower addr_kernel.ll, raise it to SM 6.6, assemble and sign it with
dxasm, then dispatch it with addrmap_test (descriptor-heap table at slot 0, dummy at 1, resources from 2).

usage: python -I addr_test.py [groups] [dxasm.exe] [addrmap_test.exe]   (defaults: 4, ./dxasm.exe, ./addrmap_test.exe)
Build the two exes first: cl /nologo /EHsc /O2 /std:c++17 addrmap_test.cpp d3d12.lib dxgi.lib /Fe:addrmap_test.exe
and:                     cl /nologo /EHsc /O2 /std:c++17 dxasm.cpp /Fe:dxasm.exe
"""
import os, re, subprocess, sys, tempfile

here = os.path.dirname(os.path.abspath(__file__))
groups = sys.argv[1] if len(sys.argv) > 1 else "4"
dxasm = sys.argv[2] if len(sys.argv) > 2 else "dxasm.exe"
runner = sys.argv[3] if len(sys.argv) > 3 else "addrmap_test.exe"
work = tempfile.mkdtemp(prefix="addr_test_")
low, sm66, dxil = (os.path.join(work, n) for n in ("kernel.ll", "kernel66.ll", "kernel.dxil"))


def run(cmd):
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode:
        sys.exit("%s failed:\n%s%s" % (cmd[0], r.stdout, r.stderr))
    return r.stdout


run([sys.executable, "-I", os.path.join(here, "xbox_dxil_lower.py"), os.path.join(here, "addr_kernel.ll"), low])
# no wave operations in this kernel: 6.6 without pinning a wave size, so WARP accepts it too
run([sys.executable, "-I", os.path.join(here, "dxil_sm66_wave.py"), low, sm66, "0"])
r = subprocess.run([dxasm, sm66, dxil], capture_output=True, text=True)
m = re.search(r"Flags declared=(\d+), actual=(\d+)", r.stdout)
if m:                                   # the entry-point flags word must describe what the lowering now uses
    t = open(sm66, encoding="utf-8").read()
    t = re.sub(r"(!\d+ = !\{i32 0, i64 )\d+(, i32 4)", r"\g<1>%s\g<2>" % m.group(2), t)
    open(sm66, "w", encoding="utf-8", newline="\n").write(t)
    r = subprocess.run([dxasm, sm66, dxil], capture_output=True, text=True)
if r.returncode:
    sys.exit("dxasm failed:\n" + r.stdout)
print(r.stdout.strip())
print(run([runner, dxil, groups]))
print("ok, artifacts in", work)
