"""Lower, pin the wave size and assemble every shader_NN.ll of a directory; report which ones validate.
usage: python -I lower_all.py <dir with shader_NN.ll> <out_dir> [wave=64] [dxasm=dxasm.exe]
Each shader is raised to SM 6.6 (the address lowering needs createHandleFromHeap); the wave size is pinned only where wave operations are used.
For each shader writes <out>/sNN.ll (lowered), <out>/wNN.ll (SM 6.6 + wave size) and <out>/repl_NN.dxil (signed; the file bvh_test's BVH_REPLACE_DIR reads).
A validator complaint "Flags must match usage" (declared=X, actual=Y) is repaired by rewriting the declared shader-flags word and retrying."""
import glob, os, re, subprocess, sys

here = os.path.dirname(os.path.abspath(__file__))
src, out = sys.argv[1], sys.argv[2]
wave = sys.argv[3] if len(sys.argv) > 3 else "64"
dxasm = sys.argv[4] if len(sys.argv) > 4 else os.path.join(out, "dxasm.exe")
os.makedirs(out, exist_ok=True)
res = {}
for f in sorted(glob.glob(os.path.join(src, "shader_*.ll"))):
    n = re.search(r"shader_(\d+)", f).group(1)
    s_ll, w_ll, dxil = (os.path.join(out, p % n) for p in ("s%s.ll", "w%s.ll", "repl_%s.dxil"))
    r = subprocess.run([sys.executable, "-I", os.path.join(here, "xbox_dxil_lower.py"), f, s_ll], capture_output=True, text=True)
    if r.returncode:
        res[n] = "intrinsics left: " + r.stdout.strip().split(": ", 1)[-1]
        continue
    if "@dx.op.wave" in open(s_ll, encoding="utf-8").read():
        r = subprocess.run([sys.executable, "-I", os.path.join(here, "dxil_sm66_wave.py"), s_ll, w_ll, wave], capture_output=True, text=True)
        if r.returncode:
            res[n] = "sm66 rewrite failed: " + (r.stderr.strip().splitlines() or ["?"])[-1]
            continue
    else:
        # no wave operations: still raise to 6.6 (the address lowering needs createHandleFromHeap) but do not pin a wave size
        r = subprocess.run([sys.executable, "-I", os.path.join(here, "dxil_sm66_wave.py"), s_ll, w_ll, "0"], capture_output=True, text=True)
        if r.returncode:
            res[n] = "sm66 rewrite failed: " + (r.stderr.strip().splitlines() or ["?"])[-1]
            continue
    for attempt in range(2):
        r = subprocess.run([dxasm, w_ll, dxil], capture_output=True, text=True)
        m = re.search(r"Flags declared=(\d+), actual=(\d+)", r.stdout)
        if m and attempt == 0:
            t = open(w_ll, encoding="utf-8").read()
            t = re.sub(r"(!\d+ = !\{i32 0, i64 )\d+(, i32 4)", r"\g<1>%s\g<2>" % m.group(2), t)
            open(w_ll, "w", encoding="utf-8", newline="\n").write(t)
            continue
        break
    res[n] = "ok" if r.returncode == 0 else "validation: " + " | ".join(l for l in r.stdout.splitlines() if "error" in l)[:300]
ok = sum(1 for v in res.values() if v == "ok")
for n, v in res.items():
    print(n, v)
print("%d of %d assembled and signed" % (ok, len(res)))
