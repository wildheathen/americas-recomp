"""Raise lowered DXIL text (xbox_dxil_lower.py output) to shader model 6.6 and pin the wave size, e.g. to 64.

The Xbox shaders were compiled for 64-lane waves; D3D12 on RDNA PCs normally runs 32-lane waves, so wave-dependent code (prefix sums, ballots,
bit loops over a lane mask) breaks. SM 6.6's [WaveSize(N)] attribute fixes that. SM 6.6 also forbids dx.op.createHandle, so each one becomes
createHandleFromBinding + annotateHandle (the encodings were copied from what dxc emits for the same resource kinds; class 0 = SRV, 1 = UAV, 2 = CBV).

usage: python -I dxil_sm66_wave.py in.ll out.ll [wave_size=64]      (wave_size 0: raise to 6.6 but pin nothing)
"""
import re, sys

src, dst = sys.argv[1], sys.argv[2]
wave = int(sys.argv[3]) if len(sys.argv) > 3 else 64
t = open(src, encoding="utf-8").read()

t = re.sub(r"^!(\d+) = !\{i32 1, i32 5\}$", r"!\1 = !{i32 1, i32 6}", t, count=1, flags=re.M)         # dx.version (first match is it)
t = re.sub(r'^(!\d+) = !\{!"cs", i32 6, i32 5\}$', r'\1 = !{!"cs", i32 6, i32 6}', t, count=1, flags=re.M)
m = re.search(r"^(!\d+) = !\{i32 0, i64 (\d+), i32 4, (!\d+)\}$", t, re.M)
assert m, "entry properties not found"
if wave > 0:
    t = t.replace(m.group(0), "%s = !{i32 0, i64 %s, i32 4, %s, i32 11, !9999}\n!9999 = !{i32 %d}" % (m.group(1), m.group(2), m.group(3), wave))

cbsize = 16
mm = re.search(r"^!\d+ = !\{i32 0, %\w+\* undef, !\"[^\"]*\", i32 0, i32 \d+, i32 1, i32 (\d+), null\}$", t, re.M)   # cbuffer size from resource metadata
if mm: cbsize = int(mm.group(1))

def conv(m):
    ind, res, cls, rng, idx, nu = m.groups()
    raw = "%%hraw.%s" % res[1:]
    props = ("{ i32 13, i32 %d }" % cbsize if cls == "2" else
             "{ i32 4107, i32 0 }" if cls == "1" else "{ i32 11, i32 0 }")
    return ("%s%s = call %%dx.types.Handle @dx.op.createHandleFromBinding(i32 217, %%dx.types.ResBind { i32 %s, i32 %s, i32 0, i8 %s }, i32 %s, i1 %s)\n"
            "%s%s = call %%dx.types.Handle @dx.op.annotateHandle(i32 216, %%dx.types.Handle %s, %%dx.types.ResourceProperties %s)") % (ind, raw, idx, idx, cls, idx, nu, ind, res, raw, props)

# createHandle(i32 57, i8 class, i32 rangeId, i32 index, i1 nonUniform)  -- class 1 = UAV, 2 = CBV
t = re.sub(r"^(\s*)(%\d+) = call %dx\.types\.Handle @dx\.op\.createHandle\(i32 57, i8 (\d), i32 (\d+), i32 (\d+), i1 (\w+)\)[^\n]*$", conv, t, flags=re.M)
# the handle names are %N.raw: numbers must stay consecutive, so make every result named instead of numbered
t = t.replace("%dx.types.ResBind", "%dx.types.ResBind")
decl = ""
if "%dx.types.ResBind = type" not in t:
    decl += "%dx.types.ResBind = type { i32, i32, i32, i8 }\n"
if "%dx.types.ResourceProperties = type" not in t:
    decl += "%dx.types.ResourceProperties = type { i32, i32 }\n"
t = t.replace("%dx.types.Handle = type { i8* }\n", "%dx.types.Handle = type { i8* }\n" + decl, 1)
t = re.sub(r"declare %dx\.types\.Handle @dx\.op\.createHandle\([^\n]*\n\n?", "", t)
newdecls = ""
if "declare %dx.types.Handle @dx.op.createHandleFromBinding(" not in t:
    newdecls += ("; Function Attrs: nounwind readnone\n"
                 "declare %dx.types.Handle @dx.op.createHandleFromBinding(i32, %dx.types.ResBind, i32, i1) #0\n\n")
if "declare %dx.types.Handle @dx.op.annotateHandle(" not in t:
    newdecls += ("; Function Attrs: nounwind readnone\n"
                 "declare %dx.types.Handle @dx.op.annotateHandle(i32, %dx.types.Handle, %dx.types.ResourceProperties) #0\n\n")
m = re.search(r"^(?:declare|define) ", t, re.M)
if m:
    t = t[:m.start()] + newdecls + t[m.start():]
else:
    t = t.rstrip("\n") + "\n\n" + newdecls
open(dst, "w", encoding="utf-8", newline="\n").write(t)
print("wrote", dst, "wave size", wave)
