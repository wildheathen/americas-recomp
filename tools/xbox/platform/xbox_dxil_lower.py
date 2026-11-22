"""Rewrite Xbox DXIL text (the output of `dxc -dumpbin`) so it no longer calls the dxcxbox `@xbox.*` intrinsics.

Each intrinsic that has a standard DXIL equivalent is replaced by it; a shader that still calls an intrinsic with no replacement is
reported (exit status 1). The first argument of every xbox.* call (an instruction number) is dropped. The Xbox target triple
(dxil-ms-xb2) and the `xbox.compiler.options` module metadata are rewritten, because the standard validator rejects them.
The Xbox validator also accepts first-class aggregates such as `{ half, half }` and `{ i32, i32 }` (a 64-bit lane mask); standard DXIL does not,
so every such pair value is split into two scalars (its insertvalue/extractvalue chains disappear).
Numeric type IDs (`%N = type ...`) are not supported; the input is expected to use named type tokens as `dxc -dumpbin` does.

usage: python -I xbox_dxil_lower.py in.ll out.ll        then: dxasm out.ll out.dxil

Lowered so far (more are added as shaders need them):
  Med3_F32                 max(min(a,b), min(max(a,b),c))                              dx.op.binary.f32
  V_PK_MIN_F16/MAX_F16     lane-wise min/max of a half pair                            dx.op.binary.f16
  GetLaneID                WaveGetLaneIndex
  MakeUniform              WaveReadLaneFirst (v_readfirstlane)
  ReadLane                 WaveReadLaneAt (v_readlane)
  LaneSwizzle              ds_swizzle_b32: offset bit 15 = 0 is the bitmask mode ((lane & and) | or) ^ xor inside a group of 32 lanes
                           (and = off[4:0], or = off[9:5], xor = off[14:10]); bit 15 = 1 is the quad-permute mode. -> WaveReadLaneAt
  Ballot64                 WaveActiveBallot, returning the low two dwords as the 64-bit mask
  V_CNDMASK_B32            (mask pair, {src0, src1}): the mask bit of this lane picks src1, else src0. Argument order inferred from the
                           Hillis-Steele prefix sums these shaders contain (masks 0xAAAAAAAA, 0xCCCCCCCC, 0xF0F0F0F0 with swizzles 30, 60, 120)
  MBCNT64                  number of set mask bits below this lane (v_mbcnt_lo + v_mbcnt_hi)
  S_BCNT1_U64              popcount of the 64-bit mask
  Min3/Max3_F32, Min3/Max3/Med3_U32, BFI, V_ALIGNBYTE_B32, WaveMin_U32, MakeUniform.i64   the obvious standard operations
  TypedLoad.i32            (handle, byte offset, format 65 = 8_UINT): the dword at (offset & ~3) of the raw buffer, byte (offset & 3);
                           only component 0 of the result may be used
  llvm.copysign.f32        magnitude of the first operand with the sign of the second (bit operations)
  { i32, i32, i32[, i32] } first-class aggregates are flattened like the pairs above; rawBufferLoad/Store calls that carry a second
                           coordinate (an Xbox DXC habit) get it added to the byte index
  global_load_dword{,x2,x3,x4}_with_cache, global_store_dword{,x3,x4}_with_cache, s_load_dword_with_cache,
  global_atomic_{add,sub,fmin,fmax}: the 64-bit address is looked up in a table of (base, size, heap slot) entries and the access goes through
                           dx.op.createHandleFromHeap (SM 6.6 ResourceDescriptorHeap). Heap slot 0 holds the table itself (raw UAV), slot 1
                           is a dummy for addresses that no entry covers, slots 2 and up are the resources. The table is sorted by base and
                           padded to 16 entries with (base = 2^64-1, size = 0); the lookup is a branchless binary search. Offsets past the
                           start of the covered range are passed as a byte offset into the resource's descriptor, so the view is limited to
                           4 GiB. Multi-dword accesses become one access per dword (a multi-component raw load through a heap handle crashes WARP).
                           fmin/fmax use UMin/UMax on the order-preserving float key (correct except for NaN stored in memory, which is replaced).
The wave intrinsics assume the 64-lane waves the shaders were built for: run the result with the wave size pinned to 64 (dxil_sm66_wave.py).
"""
import re, sys

PAIR_RE = r"\{ (?:half, half|i32, i32) \}"


def split_params(text):
    out, depth, current = [], 0, ""
    for ch in text:
        if ch in "([{":
            depth += 1
        elif ch in ")]}":
            depth -= 1
        if ch == "," and depth == 0:
            out.append(current.strip())
            current = ""
        else:
            current += ch
    if current.strip():
        out.append(current.strip())
    return out


def param_text(header):
    start = header.find("(")
    if start < 0:
        return ""
    depth = 0
    for i in range(start, len(header)):
        if header[i] == "(":
            depth += 1
        elif header[i] == ")":
            depth -= 1
            if depth == 0:
                return header[start + 1:i]
    return ""


def renumber_function(header, body):
    mapping, n, original = {}, 0, 0
    for param in split_params(param_text(header)):
        tokens = param.split()
        names = [t for t in tokens if t.startswith("%")]
        if not names:
            mapping[str(original)] = str(n)
            original += 1
            n += 1
        elif re.fullmatch(r"%\d+", names[-1]):
            mapping[names[-1][1:]] = str(n)
            original = max(original, int(names[-1][1:]) + 1)
            n += 1
        elif not (len(tokens) > 1 and tokens[-1] == names[-1]):
            mapping[str(original)] = str(n)
            original += 1
            n += 1
    lines = [line.strip() for line in body.split("\n")]
    lines = [line for line in lines if line and (not line.startswith(";") or line.startswith("; <label>:"))]
    if lines and not any(re.match(pattern, lines[0]) for pattern in
                         (r"^; <label>:", r"^%?\d+:", r"^[-\w.$]+:\s*$")):
        mapping[str(original)] = str(n)
        original += 1
        n += 1
    for line in lines:
        label = re.match(r"^; <label>:(\d+)", line)
        value = re.match(r"^%(\d+) = ", line)
        numbered_label = re.match(r"^%?(\d+):", line)
        if label or numbered_label:
            mapping[(label or numbered_label).group(1)] = str(n)
            n += 1
        elif value:
            mapping[value.group(1)] = str(n)
            n += 1
    sub = lambda x: "%" + mapping.get(x.group(1), x.group(1))
    header = re.sub(r"(?<=\s)%(\d+)(?=\s*(?:,|\)))", sub, header)
    body = re.sub(r"%(\d+)\b", sub, body)
    body = re.sub(r"^; <label>:(\d+)", lambda x: "; <label>:" + mapping.get(x.group(1), x.group(1)), body, flags=re.M)
    body = re.sub(r"^(\s*)(\d+):", lambda x: x.group(1) + mapping.get(x.group(2), x.group(2)) + ":", body, flags=re.M)
    return header + body


def renumber(text):
    """Unnamed values and blocks (%N, '; <label>:N') must be numbered consecutively; removing instructions leaves holes."""
    out, pos = [], 0
    for m in re.finditer(r"^define [^\n]*\{\n", text, re.M):
        if m.start() < pos:
            continue
        start = m.end()
        end = text.find("\n}\n", start)
        if end < 0:
            continue
        out.append(text[pos:m.start()])
        out.append(renumber_function(text[m.start():start], text[start:end]))
        pos = end
    out.append(text[pos:])
    return "".join(out)


def split_args(s):
    """Split a call's argument text at top-level commas (braces nest)."""
    out, depth, cur = [], 0, ""
    for ch in s:
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
        if ch == "," and depth == 0:
            out.append(cur.strip())
            cur = ""
        else:
            cur += ch
    if cur.strip():
        out.append(cur.strip())
    return out


# declarations a lowering may need: marker -> text
DECLS = {
    "@dx.op.binary.f32(": "declare float @dx.op.binary.f32(i32, float, float) #0",
    "@dx.op.binary.f16(": "declare half @dx.op.binary.f16(i32, half, half) #0",
    "@dx.op.waveGetLaneIndex(": "declare i32 @dx.op.waveGetLaneIndex(i32) #0",
    "@dx.op.waveReadLaneFirst.i32(": "declare i32 @dx.op.waveReadLaneFirst.i32(i32, i32) #1",
    "@dx.op.waveReadLaneAt.i32(": "declare i32 @dx.op.waveReadLaneAt.i32(i32, i32, i32) #1",
    "@dx.op.waveActiveBallot(": "declare %dx.types.fouri32 @dx.op.waveActiveBallot(i32, i1) #1",
    "@dx.op.unaryBits.i32(": "declare i32 @dx.op.unaryBits.i32(i32, i32) #0",
    "@dx.op.binary.i32(": "declare i32 @dx.op.binary.i32(i32, i32, i32) #0",
    "@dx.op.waveActiveOp.i32(": "declare i32 @dx.op.waveActiveOp.i32(i32, i32, i8, i8) #1",
    "@dx.op.waveReadLaneFirst.i64(": "declare i64 @dx.op.waveReadLaneFirst.i64(i32, i64) #1",
    "@dx.op.rawBufferLoad.i32(": "declare %dx.types.ResRet.i32 @dx.op.rawBufferLoad.i32(i32, %dx.types.Handle, i32, i32, i8, i32) #1",
    "@dx.op.rawBufferLoad.i64(": "declare %dx.types.ResRet.i64 @dx.op.rawBufferLoad.i64(i32, %dx.types.Handle, i32, i32, i8, i32) #1",
    "@dx.op.rawBufferStore.i32(": "declare void @dx.op.rawBufferStore.i32(i32, %dx.types.Handle, i32, i32, i32, i32, i32, i32, i8, i32) #1",
    "@dx.op.atomicBinOp.i32(": "declare i32 @dx.op.atomicBinOp.i32(i32, %dx.types.Handle, i32, i32, i32, i32, i32) #1",
    "@dx.op.createHandleFromHeap(": "declare %dx.types.Handle @dx.op.createHandleFromHeap(i32, i32, i1, i1) #0",
    "@dx.op.annotateHandle(": "declare %dx.types.Handle @dx.op.annotateHandle(i32, %dx.types.Handle, %dx.types.ResourceProperties) #0",
}


def insert_decls(text, decls):
    block = "\n".join(decls) + "\n\n"
    for prefix in ("declare ", "define "):
        m = re.search(r"^" + prefix, text, re.M)
        if m:
            return text[:m.start()] + block + text[m.start():]
    return text.rstrip("\n") + "\n\n" + block


def lower(text):
    n_lowered = 0
    pairs = {}            # SSA name of a pair value -> (scalar0, scalar1)
    alias = {}            # SSA name of a scalar produced by extractvalue -> the scalar that replaces it
    bytes_ = {}
    agg_comps = {}        # SSA name of a multi-dword load -> [component SSA value]
    aggs = {}             # SSA name of a {i32,i32,i32[,i32]} value -> [element SSA/literal]
    out = []
    counter = [0]
    used = set()

    def fresh():
        counter[0] += 1
        return "%%xl.%d" % counter[0]

    def sub(line):
        if not alias:
            return line
        return re.sub(r"%[\w.]+", lambda m: alias.get(m.group(0), m.group(0)), line)

    ins_re = re.compile(r"^\s*(%[\w.]+) = insertvalue " + PAIR_RE + r" (undef|%[\w.]+), (?:half|i32) (%[\w.]+|[-+\w.]+), ([01])\s*$")
    ins_any_re = re.compile(r"^\s*(%[\w.]+) = insertvalue \{ i32, i32, i32(?:, i32)? \} (undef|%[\w.]+|\{[^}]*\}), i32 (%[\w.]+|[-+]?\d+), ([0-3])\s*$")
    ext_re = re.compile(r"^\s*(%[\w.]+) = extractvalue " + PAIR_RE + r" (%[\w.]+), ([01])\s*$")
    ext_any_re = re.compile(r"^(\s*)(%[\w.]+) = extractvalue \{ i32(?:, i32)+ \} (%[\w.]+), (\d+)\s*$")
    resret_re = re.compile(r"^(\s*)(%[\w.]+) = extractvalue %dx\.types\.ResRet\.i32 (%[\w.]+), (\d+)\s*$")
    copysign_re = re.compile(r"^(\s*)(%[\w.]+) = call float @llvm\.copysign\.f32\(float (%[\w.]+|[-+]?[\d.eE+]+), float (%[\w.]+|[-+]?[\d.eE+]+)\)\s*$")
    raw2_coord_re = re.compile(r"^(\s*)(.*?@dx\.op\.rawBuffer(?:Load|Store)\.[A-Za-z0-9.]+\(i32 (?:139|140), %dx\.types\.Handle %[\w.]+, i32 )([^,]+)(, i32 )(?!undef\b)([^,]+)(, .*)$")
    ext_lit_re = re.compile(r"^(\s*)(%[\w.]+) = extractvalue \{ i32(?:, i32)+ \} \{([^}]*)\}, (\d+)\s*$")
    call_re = re.compile(r"^(\s*)(?:%([\w.]+) = )?call ([^@]+?) @xbox\.([\w.]+)\(i32 \d+(?:, (.*?))?\)\s*(?:,\s*![^;]*)?(?:;.*)?$")

    def operand(a):
        """('%x' | literal pair) -> (s0, s1);  'i32 %x' -> '%x'"""
        m = re.match(r"^" + PAIR_RE + r" (.*)$", a)
        if m:
            v = m.group(1).strip()
            if v.startswith("{"):
                lit = re.findall(r"(?:half|i32) ([^,}\s]+)", v)
                return (lit[0], lit[1])
            return pairs[v]
        return a.split(" ", 1)[1]

    for line in text.split("\n"):
        line = sub(line)
        m = raw2_coord_re.match(line)
        if m:
            t = fresh()
            out.append("%s%s = add i32 %s, %s" % (m.group(1), t, m.group(3), m.group(5)))
            line = m.group(1) + m.group(2) + t + ", i32 undef" + m.group(6)
        m = copysign_re.match(line)
        if m:
            x, y, xs, ys, rr = fresh(), fresh(), fresh(), fresh(), fresh()
            emit = lambda s: out.append(m.group(1) + s)
            emit("%s = bitcast float %s to i32" % (x, m.group(3)))
            emit("%s = bitcast float %s to i32" % (y, m.group(4)))
            emit("%s = and i32 %s, 2147483647" % (xs, x))
            emit("%s = and i32 %s, -2147483648" % (ys, y))
            emit("%s = or i32 %s, %s" % (rr, xs, ys))
            emit("%s = bitcast i32 %s to float" % (m.group(2), rr))
            n_lowered += 1
            continue
        m = ins_re.match(line)
        if m:
            dst, src, val, idx = m.groups()
            cur = list(pairs.get(src, ("undef", "undef")))
            cur[int(idx)] = val
            pairs[dst] = tuple(cur)
            continue
        m = ins_any_re.match(line)
        if m:
            dst, src, val, idx = m.groups()
            cur = aggs.get(src)
            if cur is None:
                lit = re.findall(r"i32 ([^,}\s]+)", src) if src.startswith("{") else []
                cur = (lit or ["undef"] * 4) + ["undef"] * (4 - len(lit or []))
            cur = list(cur)
            cur[int(idx)] = val
            aggs[dst] = cur
            continue
        m = ext_re.match(line)
        if m and m.group(2) in agg_comps:
            alias[m.group(1)] = agg_comps[m.group(2)][int(m.group(3))]
            continue
        if m and m.group(2) in pairs:
            alias[m.group(1)] = pairs[m.group(2)][int(m.group(3))]
            continue
        m = ext_lit_re.match(line)
        if m:
            lit = re.findall(r"i32 ([^,}\s]+)", m.group(3))[int(m.group(4))]
            alias[m.group(2)] = lit
            continue
        m = ext_any_re.match(line)
        if m and m.group(3) in aggs:
            alias[m.group(2)] = aggs[m.group(3)][int(m.group(4))]
            continue
        if m and m.group(3) in agg_comps:
            alias[m.group(2)] = agg_comps[m.group(3)][int(m.group(4))]
            continue
        m = resret_re.match(line)
        if m and m.group(3) in bytes_:
            if m.group(4) != "0":
                raise SystemExit("TypedLoad.i32 result component %s is used; only component 0 is lowered" % m.group(4))
            out.append("%s%s = and i32 %s, 255" % (m.group(1), m.group(2), bytes_[m.group(3)]))
            continue
        m = call_re.match(line)
        if not m:
            out.append(line)
            continue
        ind, dst, rty, name, argtext = m.groups()
        dst = "%" + dst if dst else None
        args = split_args(argtext or "")
        emit = lambda s: out.append(ind + s)

        def lane():
            l = fresh()
            emit("%s = call i32 @dx.op.waveGetLaneIndex(i32 111)" % l)
            used.add("@dx.op.waveGetLaneIndex(")
            return l

        def bin_(kind, ty, a, b, op):
            r = fresh()
            emit("%s = call %s @dx.op.binary.%s(i32 %d, %s %s, %s %s)" % (r, ty, kind, op, ty, a, ty, b))
            used.add("@dx.op.binary.%s(" % kind)
            return r

        def popc(v):
            r = fresh()
            emit("%s = call i32 @dx.op.unaryBits.i32(i32 31, i32 %s)" % (r, v))
            used.add("@dx.op.unaryBits.i32(")
            return r

        def i(op, a, b):
            r = fresh()
            emit("%s = %s i32 %s, %s" % (r, op, a, b))
            return r

        TABLE_SLOT, MISS_SLOT = 0, 1

        def addr_lookup(a):
            """64-bit address -> (handle to the owning resource, byte offset). Addresses outside every entry read/write the
            dummy resource in heap slot 1; the caller zeroes the result if it cares. Table entries are 24 bytes: u64 base,
            u64 size, u32 heap slot (the last dword is padding). Sorted by base, 16 entries, sentinel base 2^64-1 size 0."""
            th = fresh()
            emit("%s = call %%dx.types.Handle @dx.op.createHandleFromHeap(i32 218, i32 %d, i1 false, i1 true)" % (th, TABLE_SLOT))
            used.update(("@dx.op.createHandleFromHeap(", "@dx.op.annotateHandle(", "@dx.op.rawBufferLoad.i64(", "@dx.op.rawBufferLoad.i32("))
            tbl = fresh()
            emit("%s = call %%dx.types.Handle @dx.op.annotateHandle(i32 216, %%dx.types.Handle %s, %%dx.types.ResourceProperties { i32 4107, i32 0 })" % (tbl, th))
            idx = fresh()
            emit("%s = add i32 0, 0" % idx)
            for step in (8, 4, 2, 1):
                j = i("add", idx, str(step))
                jb = i("mul", j, "24")
                rb = fresh()
                emit("%s = call %%dx.types.ResRet.i64 @dx.op.rawBufferLoad.i64(i32 139, %%dx.types.Handle %s, i32 %s, i32 undef, i8 3, i32 8)" % (rb, tbl, jb))
                b = fresh()
                emit("%s = extractvalue %%dx.types.ResRet.i64 %s, 0" % (b, rb))
                c = fresh()
                emit("%s = icmp uge i64 %s, %s" % (c, a, b))
                nit = fresh()
                emit("%s = select i1 %s, i32 %s, i32 %s" % (nit, c, j, idx))
                idx = nit
            fb = i("mul", idx, "24")
            rb = fresh()
            emit("%s = call %%dx.types.ResRet.i64 @dx.op.rawBufferLoad.i64(i32 139, %%dx.types.Handle %s, i32 %s, i32 undef, i8 3, i32 8)" % (rb, tbl, fb))
            base, size = fresh(), fresh()
            emit("%s = extractvalue %%dx.types.ResRet.i64 %s, 0" % (base, rb))
            emit("%s = extractvalue %%dx.types.ResRet.i64 %s, 1" % (size, rb))
            d = fresh()
            emit("%s = sub i64 %s, %s" % (d, a, base))
            valid = fresh()
            emit("%s = icmp ult i64 %s, %s" % (valid, d, size))
            hb = i("add", fb, "16")
            rh = fresh()
            emit("%s = call %%dx.types.ResRet.i32 @dx.op.rawBufferLoad.i32(i32 139, %%dx.types.Handle %s, i32 %s, i32 undef, i8 1, i32 4)" % (rh, tbl, hb))
            hp = fresh()
            emit("%s = extractvalue %%dx.types.ResRet.i32 %s, 0" % (hp, rh))
            slot = fresh()
            emit("%s = select i1 %s, i32 %s, i32 %d" % (slot, valid, hp, MISS_SLOT))
            d32 = fresh()
            emit("%s = trunc i64 %s to i32" % (d32, d))
            off = fresh()
            emit("%s = select i1 %s, i32 %s, i32 0" % (off, valid, d32))
            hw = fresh()
            emit("%s = call %%dx.types.Handle @dx.op.createHandleFromHeap(i32 218, i32 %s, i1 false, i1 true)" % (hw, slot))
            h = fresh()
            emit("%s = call %%dx.types.Handle @dx.op.annotateHandle(i32 216, %%dx.types.Handle %s, %%dx.types.ResourceProperties { i32 4107, i32 0 })" % (h, hw))
            return h, off, valid

        def raw_load(a, count):
            """count dwords at the 64-bit address -> list of component SSA values (0 where the address is not covered).
            One handle per access, one single-dword load per component: multi-component raw loads through a heap handle crash
            WARP's shader JIT (0xC0000005 at dispatch)."""
            h, off, valid = addr_lookup(a)
            comps = []
            for k in range(count):
                o = off if k == 0 else i("add", off, str(4 * k))
                r = fresh()
                emit("%s = call %%dx.types.ResRet.i32 @dx.op.rawBufferLoad.i32(i32 139, %%dx.types.Handle %s, i32 %s, i32 undef, i8 1, i32 4)"
                     % (r, h, o))
                w = fresh()
                emit("%s = extractvalue %%dx.types.ResRet.i32 %s, 0" % (w, r))
                c = fresh()
                emit("%s = select i1 %s, i32 %s, i32 0" % (c, valid, w))
                comps.append(c)
            used.add("@dx.op.rawBufferLoad.i32(")
            return comps

        def store_elements(text, count):
            if count == 1:
                return [operand(text)]
            text = text.strip()
            end = text.index("}") + 1
            lit, rest = text[:end], text[end:].strip()
            if rest.startswith("{"):
                return re.findall(r"i32 ([^,}\s]+)", rest)[:count]
            if rest in aggs:
                return aggs[rest][:count]
            if rest:
                vals = []
                for k in range(count):
                    r = fresh()
                    emit("%s = extractvalue %s %s, %d" % (r, lit, rest, k))
                    vals.append(r)
                return vals
            return re.findall(r"i32 ([^,}\s]+)", lit)

        if name == "Med3_F32":
            a, b, c = [operand(x) for x in args]
            lo, hi = bin_("f32", "float", a, b, 36), bin_("f32", "float", a, b, 35)
            t = bin_("f32", "float", hi, c, 36)
            emit("%s = call float @dx.op.binary.f32(i32 35, float %s, float %s)" % (dst, lo, t))
        elif name in ("V_PK_MIN_F16", "V_PK_MAX_F16"):
            a, b = operand(args[0]), operand(args[1])
            op = 36 if name.endswith("MIN_F16") else 35
            pairs[dst] = tuple(bin_("f16", "half", a[k], b[k], op) for k in range(2))
        elif name == "GetLaneID":
            emit("%s = call i32 @dx.op.waveGetLaneIndex(i32 111)" % dst)
            used.add("@dx.op.waveGetLaneIndex(")
        elif name == "MakeUniform.i32":
            emit("%s = call i32 @dx.op.waveReadLaneFirst.i32(i32 118, i32 %s)" % (dst, operand(args[0])))
            used.add("@dx.op.waveReadLaneFirst.i32(")
        elif name == "ReadLane.i32":
            emit("%s = call i32 @dx.op.waveReadLaneAt.i32(i32 117, i32 %s, i32 %s)" % (dst, operand(args[0]), operand(args[1])))
            used.add("@dx.op.waveReadLaneAt.i32(")
        elif name == "LaneSwizzle.i32":
            v, off = operand(args[0]), int(operand(args[1]))
            l = lane()
            if off & 0x8000:                         # quad permute: lane' = (lane & ~3) | ((off >> 2*(lane&3)) & 3)
                q = i("and", l, 3)
                sh = i("shl", q, 1)
                s = i("lshr", str(off & 0xFF), sh)
                p = i("and", s, 3)
                base = i("and", l, -4)
                tgt = i("or", base, p)
            else:                                    # bitmask: ((lane & and) | or) ^ xor, inside a group of 32
                t1 = i("and", l, off & 31)
                t2 = i("or", t1, (off >> 5) & 31)
                t3 = i("xor", t2, (off >> 10) & 31)
                base = i("and", l, -32)
                tgt = i("or", base, t3)
            emit("%s = call i32 @dx.op.waveReadLaneAt.i32(i32 117, i32 %s, i32 %s)" % (dst, v, tgt))
            used.add("@dx.op.waveReadLaneAt.i32(")
        elif name == "Ballot64":
            c = fresh()
            emit("%s = icmp ne i32 %s, 0" % (c, operand(args[0])))
            b = fresh()
            emit("%s = call %%dx.types.fouri32 @dx.op.waveActiveBallot(i32 116, i1 %s)" % (b, c))
            used.add("@dx.op.waveActiveBallot(")
            lo, hi = fresh(), fresh()
            emit("%s = extractvalue %%dx.types.fouri32 %s, 0" % (lo, b))
            emit("%s = extractvalue %%dx.types.fouri32 %s, 1" % (hi, b))
            pairs[dst] = (lo, hi)
        elif name == "V_CNDMASK_B32":
            lo, hi = operand(args[0])
            s0, s1 = operand(args[1])
            l = lane()
            ishi = fresh()
            emit("%s = icmp uge i32 %s, 32" % (ishi, l))
            sh = i("and", l, 31)
            blo, bhi = i("lshr", lo, sh), i("lshr", hi, sh)
            w = fresh()
            emit("%s = select i1 %s, i32 %s, i32 %s" % (w, ishi, bhi, blo))
            bit = i("and", w, 1)
            c = fresh()
            emit("%s = icmp ne i32 %s, 0" % (c, bit))
            emit("%s = select i1 %s, i32 %s, i32 %s" % (dst, c, s1, s0))
        elif name == "MBCNT64":
            lo, hi = operand(args[0])
            l = lane()
            ishi = fresh()
            emit("%s = icmp uge i32 %s, 32" % (ishi, l))
            sh = i("and", l, 31)
            below = i("add", i("shl", "1", sh), -1)                 # (1 << sh) - 1
            lo_m = fresh()
            emit("%s = select i1 %s, i32 -1, i32 %s" % (lo_m, ishi, below))      # lanes >= 32 see all of the low word
            hi_m = fresh()
            emit("%s = select i1 %s, i32 %s, i32 0" % (hi_m, ishi, below))
            a = popc(i("and", lo, lo_m))
            b = popc(i("and", hi, hi_m))
            emit("%s = add i32 %s, %s" % (dst, a, b))
        elif name == "S_BCNT1_U64":
            lo, hi = operand(args[0])
            emit("%s = add i32 %s, %s" % (dst, popc(lo), popc(hi)))
        elif name in ("Min3_F32", "Max3_F32"):
            a, b, c = [operand(x) for x in args]
            op = 36 if name.startswith("Min") else 35
            emit("%s = call float @dx.op.binary.f32(i32 %d, float %s, float %s)" % (dst, op, bin_("f32", "float", a, b, op), c))
            used.add("@dx.op.binary.f32(")
        elif name in ("Min3_U32", "Max3_U32", "Med3_U32"):
            a, b, c = [operand(x) for x in args]
            used.add("@dx.op.binary.i32(")
            def ub(op, x, y, d=None):
                r = d or fresh()
                emit("%s = call i32 @dx.op.binary.i32(i32 %d, i32 %s, i32 %s)" % (r, op, x, y))
                return r
            if name == "Min3_U32":
                ub(40, ub(40, a, b), c, dst)
            elif name == "Max3_U32":
                ub(39, ub(39, a, b), c, dst)
            else:                                    # median: max(min(a,b), min(max(a,b), c))
                lo, hi = ub(40, a, b), ub(39, a, b)
                ub(39, lo, ub(40, hi, c), dst)
        elif name == "BFI":                          # v_bfi_b32: (mask & x) | (~mask & y)
            m, x, y = [operand(a) for a in args]
            nm = i("xor", m, -1)
            emit("%s = or i32 %s, %s" % (dst, i("and", m, x), i("and", nm, y)))
        elif name == "V_ALIGNBYTE_B32":              # ({hi, lo} >> (8 * sh[1:0]))[31:0]
            hi, lo, sh = [operand(a) for a in args]
            sb = i("shl", i("and", sh, 3), 3)
            right = i("lshr", lo, sb)
            nz = fresh()
            emit("%s = icmp ne i32 %s, 0" % (nz, sb))
            left = i("shl", hi, i("and", i("sub", 32, sb), 31))
            emit("%s = select i1 %s, i32 %s, i32 0" % (fresh(), nz, left))
            sel = "%%xl.%d" % counter[0]
            emit("%s = or i32 %s, %s" % (dst, right, sel))
        elif name == "WaveMin_U32":
            emit("%s = call i32 @dx.op.waveActiveOp.i32(i32 119, i32 %s, i8 2, i8 1)" % (dst, operand(args[0])))
            used.add("@dx.op.waveActiveOp.i32(")
        elif name == "MakeUniform.i64":
            emit("%s = call i64 @dx.op.waveReadLaneFirst.i64(i32 118, i64 %s)" % (dst, operand(args[0])))
            used.add("@dx.op.waveReadLaneFirst.i64(")
        elif name == "TypedLoad.i32" and len(args) == 3 and operand(args[2]) == "65":
            h, off = operand(args[0]), operand(args[1])
            r = fresh()
            emit("%s = call %%dx.types.ResRet.i32 @dx.op.rawBufferLoad.i32(i32 139, %%dx.types.Handle %s, i32 %s, i32 undef, i8 1, i32 4)"
                 % (r, h, i("and", off, -4)))
            used.add("@dx.op.rawBufferLoad.i32(")
            w = fresh()
            emit("%s = extractvalue %%dx.types.ResRet.i32 %s, 0" % (w, r))
            bytes_[dst] = i("lshr", w, i("shl", i("and", off, 3), 3))
        elif name.startswith("global_load_dword") and name.endswith("_with_cache"):
            count = {"global_load_dword_with_cache": 1, "global_load_dwordx2_with_cache": 2,
                     "global_load_dwordx3_with_cache": 3, "global_load_dwordx4_with_cache": 4}[name]
            comps = raw_load(operand(args[0]), count)
            if count == 1:
                emit("%s = add i32 %s, 0" % (dst, comps[0]))
            else:
                agg_comps[dst] = comps
        elif name == "s_load_dword_with_cache":
            comps = raw_load(operand(args[0]), 1)
            emit("%s = add i32 %s, 0" % (dst, comps[0]))
        elif name.startswith("global_store_dword"):
            count = {"global_store_dword_with_cache": 1, "global_store_dwordx3_with_cache": 3,
                     "global_store_dwordx4_with_cache": 4}[name]
            h, off, _ = addr_lookup(operand(args[0]))
            for k, v in enumerate(store_elements(args[1], count)):
                o = off if k == 0 else i("add", off, str(4 * k))
                emit("call void @dx.op.rawBufferStore.i32(i32 140, %%dx.types.Handle %s, i32 %s, i32 undef, i32 %s, i32 undef, i32 undef, i32 undef, i8 1, i32 4)"
                     % (h, o, v))
            used.add("@dx.op.rawBufferStore.i32(")
        elif name in ("global_atomic_add", "global_atomic_sub"):
            h, off, valid = addr_lookup(operand(args[0]))
            v = operand(args[1])
            if name.endswith("_sub"):
                v = i("sub", "0", v)
            r = fresh()
            emit("%s = call i32 @dx.op.atomicBinOp.i32(i32 78, %%dx.types.Handle %s, i32 0, i32 %s, i32 0, i32 0, i32 %s)" % (r, h, off, v))
            used.add("@dx.op.atomicBinOp.i32(")
            emit("%s = select i1 %s, i32 %s, i32 0" % (dst, valid, r))
        elif name in ("global_atomic_fmin", "global_atomic_fmax"):
            # no float atomic in DXIL: order-preserving integer key (= bits ^ (bits >> 31 | 0x80000000)) with UMin/UMax.
            # Correct for every non-NaN value (fmin(-0, +0) picks -0); a NaN in memory is replaced rather than kept.
            h, off, valid = addr_lookup(operand(args[0]))
            bits = fresh()
            emit("%s = bitcast float %s to i32" % (bits, operand(args[1])))
            m1 = i("ashr", bits, "31")
            m2 = i("or", m1, "-2147483648")
            key = i("xor", bits, m2)
            old = fresh()
            emit("%s = call i32 @dx.op.atomicBinOp.i32(i32 78, %%dx.types.Handle %s, i32 %d, i32 %s, i32 0, i32 0, i32 %s)"
                 % (old, h, 6 if name.endswith("fmin") else 7, off, key))
            used.add("@dx.op.atomicBinOp.i32(")
            o1 = i("ashr", old, "31")
            o2 = i("or", o1, "-2147483648")
            ob = i("xor", old, o2)
            of = fresh()
            emit("%s = bitcast i32 %s to float" % (of, ob))
            emit("%s = select i1 %s, float %s, float 0.0" % (dst, valid, of))
        else:
            out.append(line)
            continue
        n_lowered += 1
    res = "\n".join(out)
    res = re.sub(r'target triple = "dxil-ms-xb\d*"', 'target triple = "dxil-ms-dx"', res)
    res = re.sub(r"!xbox\.compiler\.options = [^\n]*\n", "", res)
    # drop declarations of intrinsics that are gone; add the standard ones that are now used
    still = set(re.findall(r"call [^@\n]*@(xbox\.[\w.]+)\(", res))
    for nm in set(re.findall(r"^declare [^@\n]*@(xbox\.[\w.]+)\(", res, re.M)):
        if nm not in still:
            res = re.sub(r"(; Function Attrs:[^\n]*\n)?declare [^\n]*@" + re.escape(nm) + r"\([^\n]*\n\n?", "", res)
    if "call float @llvm.copysign.f32(" not in res:
        res = re.sub(r"(; Function Attrs:[^\n]*\n)?declare float @llvm\.copysign\.f32\([^\n]*\n\n?", "", res)
    add = [DECLS[k] for k in sorted(used) if not re.search(r"^declare [^\n]*" + re.escape(k), res, re.M)]
    if add:
        res = insert_decls(res, add)
    if "%dx.types.fouri32" in res and "%dx.types.fouri32 = type" not in res:
        res = res.replace("%dx.types.Handle = type { i8* }\n", "%dx.types.Handle = type { i8* }\n%dx.types.fouri32 = type { i32, i32, i32, i32 }\n", 1)
    for tline in ("%dx.types.ResRet.i32 = type { i32, i32, i32, i32, i32 }",
                  "%dx.types.ResRet.i64 = type { i64, i64, i64, i64, i32 }",
                  "%dx.types.ResourceProperties = type { i32, i32 }"):
        key = tline.split(" = ")[0]
        if key in res and tline not in res:
            res = res.replace("%dx.types.Handle = type { i8* }\n", "%dx.types.Handle = type { i8* }\n" + tline + "\n", 1)
    res = renumber(res)
    left = set(re.findall(r"call [^@\n]*@(xbox\.[\w.]+)\(", res))
    return res, n_lowered, left


if __name__ == "__main__":
    text = open(sys.argv[1], encoding="utf-8", errors="ignore").read()
    res, n, left = lower(text)
    open(sys.argv[2], "w", encoding="utf-8", newline="\n").write(res)
    print("lowered %d call(s); intrinsics still called: %s" % (n, sorted(left) or "none"))
    sys.exit(1 if left else 0)
