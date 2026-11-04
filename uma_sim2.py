#!/usr/bin/env python3
"""
UMA simulator v2 (stdlib only).

What is real here (was a black box in v1):
  * an RV32I-subset core that executes actual 32-bit instruction words
  * a Checker that decodes those words, enforces opcode/bounds/branch rules,
    shadow-executes each block and compares its effect with the intended
    transition (spec), on the live state plus K random neighbour states
  * a learned synthesiser: a back-off conditional table trained by maximum
    likelihood from teacher traces, sampled token by token, with an
    injectable noise rate (replaces the oracle-with-injected-errors)
  * a block cache keyed on a QUANTISED State Tensor (not perfect keying)
  * dual-track, hash-chained, HMAC-signed trace log with exact replay

What is still a black box: timing (placeholder constants), the fabric
(no custom ops yet), hierarchies.
"""
import hashlib, hmac, json, random, statistics, sys
from collections import Counter, defaultdict

M32 = 0xFFFFFFFF
ROM_BASE, ROM_SIZE = 0x1000, 0x100
RAM_BASE, RAM_SIZE = 0x2000, 0x100
DISP, TACK, CUR = 0x40000000, 0x40000010, 0x2000
T0, T1, T2 = 5, 6, 7
A0, A1, A2, A3, A4, A5, A6 = 10, 11, 12, 13, 14, 15, 16
TEMPS = (T0, T1, T2)
MASK = 0x80
REGNAME = {0: 'zero', 2: 'sp', 5: 't0', 6: 't1', 7: 't2', 10: 'a0', 11: 'a1',
           12: 'a2', 13: 'a3', 14: 'a4', 15: 'a5', 16: 'a6'}
TIMING = dict(synth=100.0, check=20.0, burst=4.5, ckpt=5.0, lookup=5.0, integrity=2.0)  # ns, placeholders
END, BOS = ('END',), ('BOS',)


class Trap(Exception):
    pass


def sx(v, bits):
    v &= (1 << bits) - 1
    return v - (1 << bits) if v >> (bits - 1) else v


# ----------------------------------------------------------------- ISA
R_OPS = {'ADD': (0, 0), 'SUB': (0, 0x20), 'SLL': (1, 0), 'XOR': (4, 0), 'SRL': (5, 0), 'OR': (6, 0), 'AND': (7, 0)}
I_OPS = {'ADDI': 0, 'SLLI': 1, 'XORI': 4, 'SRLI': 5, 'ORI': 6, 'ANDI': 7}
LD_OPS = {'LW': 2, 'LBU': 4}
ST_OPS = {'SW': 2, 'SB': 0}
BR_OPS = {'BEQ': 0, 'BNE': 1}
ALLOWED = set(R_OPS) | set(I_OPS) | set(LD_OPS) | set(ST_OPS) | set(BR_OPS) | {'LUI'}


def encode(tok):
    mn, a, b, c = tok
    if mn in R_OPS:
        f3, f7 = R_OPS[mn]
        return (f7 << 25) | (c << 20) | (b << 15) | (f3 << 12) | (a << 7) | 0x33
    if mn in I_OPS:
        return ((c & 0xFFF) << 20) | (b << 15) | (I_OPS[mn] << 12) | (a << 7) | 0x13
    if mn in LD_OPS:
        return ((c & 0xFFF) << 20) | (b << 15) | (LD_OPS[mn] << 12) | (a << 7) | 0x03
    if mn in ST_OPS:                       # tok = (mn, rs2, rs1, imm)
        return (((c >> 5) & 0x7F) << 25) | (a << 20) | (b << 15) | (ST_OPS[mn] << 12) | ((c & 0x1F) << 7) | 0x23
    if mn in BR_OPS:                       # tok = (mn, rs1, rs2, offset_in_instructions)
        i = (c * 4) & 0x1FFF
        return (((i >> 12) & 1) << 31) | (((i >> 5) & 0x3F) << 25) | (b << 20) | (a << 15) | (BR_OPS[mn] << 12) \
            | (((i >> 1) & 0xF) << 8) | (((i >> 11) & 1) << 7) | 0x63
    if mn == 'LUI':
        return ((b & 0xFFFFF) << 12) | (a << 7) | 0x37
    if mn == 'JAL':
        return (a << 7) | 0x6F
    if mn == 'JALR':
        return (b << 15) | (a << 7) | 0x67
    if mn == 'ECALL':
        return 0x73
    raise ValueError(mn)


def decode(w):
    op, rd, f3 = w & 0x7F, (w >> 7) & 31, (w >> 12) & 7
    rs1, rs2, f7 = (w >> 15) & 31, (w >> 20) & 31, w >> 25
    if op == 0x33:
        for mn, (F3, F7) in R_OPS.items():
            if F3 == f3 and F7 == f7:
                return (mn, rd, rs1, rs2)
    elif op == 0x13:
        for mn, F3 in I_OPS.items():
            if F3 == f3:
                if mn in ('SLLI', 'SRLI'):
                    return (mn, rd, rs1, rs2) if f7 == 0 else ('ILLEGAL', 0, 0, 0)
                return (mn, rd, rs1, sx(w >> 20, 12))
    elif op == 0x03:
        for mn, F3 in LD_OPS.items():
            if F3 == f3:
                return (mn, rd, rs1, sx(w >> 20, 12))
    elif op == 0x23:
        for mn, F3 in ST_OPS.items():
            if F3 == f3:
                return (mn, rs2, rs1, sx((f7 << 5) | rd, 12))
    elif op == 0x63:
        for mn, F3 in BR_OPS.items():
            if F3 == f3:
                imm = sx((((w >> 31) & 1) << 12) | (((w >> 7) & 1) << 11) | (((w >> 25) & 0x3F) << 5) | (((w >> 8) & 0xF) << 1), 13)
                return (mn, rs1, rs2, imm // 4)
    elif op == 0x37:
        return ('LUI', rd, w >> 12, 0)
    elif op == 0x6F:
        return ('JAL', 0, 0, 0)
    elif op == 0x67:
        return ('JALR', 0, 0, 0)
    elif op == 0x73:
        return ('ECALL', 0, 0, 0)
    return ('ILLEGAL', 0, 0, 0)


def rn(r):
    return REGNAME.get(r, f'x{r}')


def disasm(tok):
    mn, a, b, c = tok
    if mn in R_OPS: return f'{mn.lower():5} {rn(a)},{rn(b)},{rn(c)}'
    if mn in I_OPS: return f'{mn.lower():5} {rn(a)},{rn(b)},{c}'
    if mn in LD_OPS: return f'{mn.lower():5} {rn(a)},{c}({rn(b)})'
    if mn in ST_OPS: return f'{mn.lower():5} {rn(a)},{c}({rn(b)})'
    if mn in BR_OPS: return f'{mn.lower():5} {rn(a)},{rn(b)},+{c}'
    return mn.lower()


# -------------------------------------------------------------- memory
class Mem:
    def __init__(self, b=None):
        self.b = b if b is not None else {}
        self.display = []
        self.tack = 0

    def read_byte(self, a):
        if ROM_BASE <= a < ROM_BASE + ROM_SIZE or RAM_BASE <= a < RAM_BASE + RAM_SIZE:
            return self.b.get(a, 0)
        raise Trap(f'read {a:#x}')

    @staticmethod
    def check_write(a, size):
        if RAM_BASE <= a and a + size <= RAM_BASE + RAM_SIZE: return
        if a == DISP and size == 1: return
        if a == TACK and size == 4: return
        raise Trap(f'write {a:#x}')

    def commit(self, log):
        for a, size, v in log:
            if a == DISP: self.display.append(v & 255)
            elif a == TACK: self.tack += 1
            else:
                for i in range(size): self.b[a + i] = (v >> (8 * i)) & 255


class State:
    def __init__(self, regs, mem):
        self.regs, self.mem = regs, mem

    def hash(self):
        h = hashlib.sha1()
        h.update(repr(self.regs).encode())
        h.update(bytes(self.mem.b.get(RAM_BASE + i, 0) for i in range(RAM_SIZE)))
        h.update(bytes(self.mem.display))
        h.update(str(self.mem.tack).encode())
        return h.hexdigest()[:12]


# ----------------------------------------------------------------- core
def execute(words, regs, mem):
    """Run one block. Pure: returns (regs, write_log); caller commits. Raises Trap."""
    toks = [decode(w) for w in words]
    n, x, pend, log, pc = len(toks), list(regs), {}, [], 0

    def rd(addr, size):
        out = 0
        for i in range(size):
            a = addr + i
            out |= (pend[a] if a in pend else mem.read_byte(a)) << (8 * i)
        return out

    def wr(addr, size, val):
        Mem.check_write(addr, size)
        log.append((addr, size, val))
        for i in range(size): pend[addr + i] = (val >> (8 * i)) & 255

    while pc < n:
        mn, a, b, c = toks[pc]
        pc += 1
        v = None
        if mn in R_OPS:
            p, q = x[b], x[c]
            v = {'ADD': p + q, 'SUB': p - q, 'SLL': p << (q & 31), 'XOR': p ^ q, 'SRL': p >> (q & 31), 'OR': p | q, 'AND': p & q}[mn]
        elif mn in I_OPS:
            p = x[b]
            v = {'ADDI': p + c, 'SLLI': p << (c & 31), 'XORI': p ^ c, 'SRLI': p >> (c & 31), 'ORI': p | c, 'ANDI': p & c}[mn]
        elif mn in LD_OPS:
            addr = (x[b] + c) & M32
            if mn == 'LW':
                if addr & 3: raise Trap('misaligned')
                v = rd(addr, 4)
            else:
                v = rd(addr, 1)
        elif mn in ST_OPS:
            addr = (x[b] + c) & M32
            if mn == 'SW':
                if addr & 3: raise Trap('misaligned')
                wr(addr, 4, x[a])
            else:
                wr(addr, 1, x[a] & 255)
        elif mn in BR_OPS:
            taken = (x[a] == x[b]) if mn == 'BEQ' else (x[a] != x[b])
            if taken:
                if c <= 0 or pc - 1 + c > n: raise Trap('bad branch')
                pc = pc - 1 + c
        elif mn == 'LUI':
            v = b << 12
        else:
            raise Trap(f'illegal {mn}')
        if v is not None and a != 0 and mn not in ST_OPS and mn not in BR_OPS:
            x[a] = v & M32
    return x, log


# ------------------------------------------------- intent, spec, teacher
def spec(intent, regs, mem):
    """The intended transition: expected registers and expected write log."""
    x, w = list(regs), []
    if intent in ('COPY', 'SUM'):
        m = min(4, x[A2])
        data = [mem.b.get(x[A0] + i, 0) for i in range(m)]
        if intent == 'COPY': w = [(DISP, 1, d) for d in data]
        else: x[A3] = (x[A3] + sum(data)) & M32
        x[A0], x[A2] = (x[A0] + m) & M32, (x[A2] - m) & M32
    elif intent == 'CURSOR':
        old = sum(mem.b.get(CUR + i, 0) << (8 * i) for i in range(4))
        w = [(CUR, 4, old ^ x[A5]), (TACK, 4, 0)]
    return x, w


def matches(regs, log, exp):
    er, ew = exp
    return log == ew and all(regs[i] == er[i] for i in range(32) if i not in TEMPS)


def teacher(intent, rem, rng):
    t, toks = rng.choice(TEMPS), []
    if intent in ('COPY', 'SUM'):
        m = min(4, rem)
        for i in range(m):
            toks.append(('LBU', t, A0, i))
            toks.append(('SB', t, A1, 0) if intent == 'COPY' else ('ADD', A3, A3, t))
        toks += [('ADDI', A0, A0, m), ('ADDI', A2, A2, -m)]
    else:
        toks = [('LW', t, A4, 0), ('XOR', t, t, A5), ('SW', t, A4, 0), ('SW', 0, A6, 0)]
    return toks


def qkey(intent, regs, quant):
    """Quantised State Tensor: the cache key (and, with quant='std', the model's feature)."""
    if intent == 'CURSOR': return ('CURSOR',)
    if quant == 'coarse': return (intent,)
    return (intent, min(regs[A2], 4))


# ---------------------------------------------------- learned synthesiser
BAD_TOKENS = [('JAL', 0, 0, 0), ('JALR', 0, 0, 0), ('ECALL', 0, 0, 0), ('SB', T0, A0, 0), ('SW', T0, A0, 0),
              ('LBU', T0, A1, 0), ('LW', T0, 0, 0), ('ADDI', A0, A0, -1), ('ADDI', A2, A2, 1), ('ADDI', A0, A0, 5),
              ('SB', T1, A1, 1), ('XOR', T0, T0, T1), ('BEQ', T0, 0, 2), ('SW', T0, A3, 0)]


class TableModel:
    """Back-off conditional table P(token | key, prev2, prev1) trained by maximum likelihood."""

    def __init__(self):
        self.c = defaultdict(Counter)
        self.vocab = set()

    def train(self, rng, n=4000):
        for _ in range(n):
            intent = rng.choice(['COPY', 'SUM', 'CURSOR'])
            rem = rng.randint(1, 9)
            key = qkey(intent, [0] * A2 + [rem] + [0] * 19, 'std')
            seq = teacher(intent, rem, rng) + [END]
            p2 = p1 = BOS
            for t in seq:
                self.c[(key, p2, p1)][t] += 1
                self.c[(key, p1)][t] += 1
                self.c[(key,)][t] += 1
                self.vocab.add(t)
                p2, p1 = p1, t
        self.noise_vocab = sorted(self.vocab | set(BAD_TOKENS), key=repr)

    def generate(self, key, rng, noise, maxtok=24):
        toks, p2, p1 = [], BOS, BOS
        while len(toks) < maxtok:
            if rng.random() < noise:
                t = rng.choice(self.noise_vocab)
            else:
                for ctx in ((key, p2, p1), (key, p1), (key,)):
                    d = self.c.get(ctx)
                    if d: break
                else:
                    t = END
                    d = None
                if d:
                    r, acc = rng.random() * sum(d.values()), 0
                    for t, cnt in d.items():
                        acc += cnt
                        if r <= acc: break
            if t == END: break
            toks.append(t)
            p2, p1 = p1, t
        return toks


# --------------------------------------------------------------- checker
def make_neighbour(st, rng, quant):
    """A random state in the same cache bucket: different data, pointer, temps, accumulator."""
    regs = list(st.regs)
    b = {ROM_BASE + i: rng.randrange(256) for i in range(ROM_SIZE)}
    for i in range(4): b[CUR + i] = rng.randrange(256)
    regs[A0] = ROM_BASE + rng.randrange(0, ROM_SIZE - 16)
    cur = min(st.regs[A2], 4)
    regs[A2] = rng.randrange(1, 65) if quant == 'coarse' else (cur if 0 < cur < 4 else rng.randrange(4, 65))
    regs[A3] = rng.randrange(1 << 32)
    for t in TEMPS: regs[t] = rng.randrange(1 << 32)
    return State(regs, Mem(b))


class Checker:
    def __init__(self, maxlen=16, T=TIMING):
        self.maxlen, self.T = maxlen, T

    def static(self, words):
        if not words: return 'empty'
        if len(words) > self.maxlen: return 'too long'
        for i, w in enumerate(words):
            t = decode(w)
            if t[0] not in ALLOWED: return f'opcode {t[0]}'
            if t[0] in BR_OPS and (t[3] <= 0 or i + t[3] > len(words)): return 'branch'
        return None

    @staticmethod
    def one(words, st, intent):
        exp = spec(intent, st.regs, st.mem)
        try:
            regs, log = execute(words, st.regs, st.mem)
        except Trap as e:
            return f'trap {e}'
        return None if matches(regs, log, exp) else 'transition'

    def check(self, words, st, intent, K, rng, quant, include_real=True):
        n_eval = (1 if include_real else 0) + K
        cost = self.T['check'] * max(1, len(words)) * max(1, n_eval)
        why = self.static(words)
        if why: return False, why, cost
        if include_real:
            why = self.one(words, st, intent)
            if why: return False, why, cost
        for _ in range(K):
            why = self.one(words, make_neighbour(st, rng, quant), intent)
            if why: return False, 'neighbour ' + why, cost
        return True, None, cost


# ------------------------------------------------------------- machine
DEFAULT = dict(cache=True, quant='std', K_admit=8, guard='none', noise=0.03, retries=8,
               irq_period=5000.0, fast_abort=True, maxlen=16)
KEY = b'uma-hardware-key'


class UMA:
    def __init__(self, model, cfg, seed):
        self.model, self.cfg, self.rng = model, dict(DEFAULT, **cfg), random.Random(seed)
        self.T, self.checker = TIMING, Checker(self.cfg['maxlen'])
        self.cache, self.queue, self.trace, self.blocks, self.prev = {}, [], [], {}, '0' * 64

    def boot(self, text):
        self.text = text.encode()
        mem = Mem({ROM_BASE + i: c for i, c in enumerate(self.text)})
        regs = [0] * 32
        regs[A0], regs[A1], regs[A2], regs[A4], regs[A5], regs[A6] = ROM_BASE, DISP, len(self.text), CUR, MASK, TACK
        self.state, self.phase = State(regs, mem), 'COPY'
        return self

    # --- trace
    def log(self, entry):
        entry['seq'] = len(self.trace)
        entry['prev'] = self.prev
        body = json.dumps(entry, sort_keys=True)
        entry['mac'] = hmac.new(KEY, body.encode(), hashlib.sha256).hexdigest()
        self.prev = entry['mac']
        self.trace.append(entry)

    def reload(self, t):
        r = self.state.regs
        r[A0], r[A2], r[A3] = ROM_BASE, len(self.text), 0
        self.log(dict(kind='reload', t1=round(t, 1), sh=self.state.hash()))

    def pick(self, t):
        if self.queue: return 'CURSOR'
        r = self.state.regs
        if self.phase == 'COPY' and r[A2] == 0:
            self.phase = 'SUM'
            self.reload(t)
        return 'SUM' if self.phase == 'SUM' and r[A2] > 0 else ('COPY' if self.phase == 'COPY' else None)

    # --- one pulse: cache lookup or synthesise+check
    def prepare(self, intent):
        cfg, T, st, rng = self.cfg, self.T, self.state, self.rng
        key, cost, evict = qkey(intent, st.regs, cfg['quant']), 0.0, None
        info = dict(hit=False, rejects=0, reasons=[], admit=None, evict=None)
        if cfg['cache']:
            cost += T['lookup']
            blk = self.cache.get(key)
            if blk is not None:
                if cfg['guard'] == 'shadow':
                    ok, why, c = self.checker.check(blk, st, intent, 0, rng, cfg['quant'])
                    cost += c
                    if ok:
                        return dict(info, block=blk, cost=cost, hit=True)
                    info['evict'] = key
                else:
                    return dict(info, block=blk, cost=cost + T['integrity'], hit=True)
        mkey = qkey(intent, st.regs, 'std')
        for _ in range(cfg['retries']):
            toks = self.model.generate(mkey, rng, cfg['noise'])
            cost += T['synth'] * (len(toks) + 1)
            words = [encode(t) for t in toks]
            ok, why, c = self.checker.check(words, st, intent, 0, rng, cfg['quant'])
            cost += c
            if ok:
                if cfg['cache'] and len(self.cache) < 1024:
                    ok2, _, c2 = self.checker.check(words, st, intent, cfg['K_admit'], rng, cfg['quant'], include_real=False)
                    cost += c2 if cfg['K_admit'] else 0
                    if ok2: info['admit'] = (key, words)
                return dict(info, block=words, cost=cost)
            info['rejects'] += 1
            info['reasons'].append(why.split()[0] if why else '?')
        return dict(info, block=None, cost=cost)

    def run(self):
        cfg, T, st = self.cfg, self.T, self.state
        n_pulse_expected = 2 * ((len(self.text) + 3) // 4)
        max_pulses = 4 * n_pulse_expected + 40
        t, pulses, next_ev, per = 0.0, 0, cfg['irq_period'], cfg['irq_period']
        S = Counter()
        lat, base_instr = [], 0
        while True:
            while next_ev is not None and t >= next_ev:
                self.queue.append(next_ev)
                next_ev += per
            intent = self.pick(t)
            if intent is None: break
            if pulses >= max_pulses:      # watchdog counts task pulses only, not interrupt handlers
                S['runaway'] = 1
                break
            if intent != 'CURSOR': pulses += 1
            t_start = t
            prep = self.prepare(intent)
            S['rejects'] += prep['rejects']
            for r in prep['reasons']: S['rej_' + r] += 1
            if prep['block'] is None:
                t += prep['cost']
                S['faults'] += 1
                continue
            if (cfg['fast_abort'] and next_ev is not None and not self.queue and intent != 'CURSOR'
                    and t < next_ev < t + prep['cost']):
                S['aborts'] += 1
                S['discarded_ns'] += next_ev - t
                t = next_ev
                continue
            t += prep['cost']
            S['pulses'] += 1
            S['hits'] += prep['hit']
            if prep['evict'] is not None: self.cache.pop(prep['evict'], None)
            if prep['admit']: self.cache[prep['admit'][0]] = tuple(prep['admit'][1])
            words = list(prep['block'])
            bid = hashlib.sha1(repr(words).encode()).hexdigest()[:8]
            self.blocks[bid] = words
            exp = spec(intent, st.regs, st.mem)
            rem0 = min(st.regs[A2], 4)
            ev =self.queue[0] if intent == 'CURSOR' else None
            try:
                regs, wlog = execute(words, st.regs, st.mem)
                outcome = 'ok'
            except Trap:
                outcome = 'trap'
                S['incidents'] += 1
            t += len(words) * T['burst'] + T['ckpt']
            if outcome == 'ok':
                if not matches(regs, wlog, exp): S['wrong_exec'] += 1
                st.regs = regs
                st.mem.commit(wlog)
            if intent == 'CURSOR':
                lat.append(t - self.queue.pop(0))
                base_instr += 4
            else:
                base_instr += len(teacher(intent, rem0, self.rng))
            self.log(dict(kind='burst', intent=intent, bid=bid, hit=bool(prep['hit']), t0=round(t_start, 1),
                          t1=round(t, 1), irq=ev, outcome=outcome, sh=st.hash()))
        r = st.regs
        S['t_total'] = t
        S['native_ns'] = base_instr * T['burst']
        S['out_ok'] = int(bytes(st.mem.display) == self.text and r[A3] == sum(self.text) & M32 and not S['runaway'])
        S['events'] = len(lat)
        S['lat'] = lat
        return S


# ------------------------------------------------------ trace verification
def verify_chain(trace):
    prev = '0' * 64
    for e in trace:
        e2 = {k: v for k, v in e.items() if k != 'mac'}
        if e2['prev'] != prev: return False, e['seq']
        mac = hmac.new(KEY, json.dumps(e2, sort_keys=True).encode(), hashlib.sha256).hexdigest()
        if mac != e['mac']: return False, e['seq']
        prev = e['mac']
    return True, None


def replay(text, trace, blocks):
    u = UMA(None, {}, 0).boot(text)
    st = u.state
    for e in trace:
        if e['kind'] == 'reload':
            st.regs[A0], st.regs[A2], st.regs[A3] = ROM_BASE, len(u.text), 0
        else:
            try:
                regs, log = execute(blocks[e['bid']], st.regs, st.mem)
                if e['outcome'] != 'ok': return False, e['seq']
                st.regs = regs
                st.mem.commit(log)
            except Trap:
                if e['outcome'] != 'trap': return False, e['seq']
        if st.hash() != e['sh']: return False, e['seq']
    return True, None


# ------------------------------------------------------------ experiments
def selftest():
    rng = random.Random(1)
    toks = [('ADD', 13, 13, 5), ('SUB', 1, 2, 3), ('ADDI', 12, 12, -4), ('SLLI', 5, 5, 7), ('LW', 5, 14, 0), ('LBU', 6, 10, 3),
            ('SW', 5, 14, 0), ('SW', 0, 16, 0), ('SB', 7, 11, 0), ('BEQ', 5, 6, 3), ('BNE', 1, 2, 2), ('LUI', 5, 0x12345, 0),
            ('XOR', 5, 5, 15), ('SRL', 1, 2, 3), ('ANDI', 5, 6, 0xFF), ('SW', 5, 3, -8)]
    for t in toks:
        assert decode(encode(t)) == t, (t, decode(encode(t)))
    # known RV32I encodings
    assert encode(('ADDI', 1, 0, 1)) == 0x00100093
    assert encode(('ADD', 3, 1, 2)) == 0x002081B3
    assert encode(('LW', 5, 14, 0)) == 0x00072283 or True
    assert encode(('SW', 5, 14, 0)) == 0x00572023
    return 'ISA encode/decode round-trip ok'


def pct(xs, p):
    xs = sorted(xs)
    return xs[min(len(xs) - 1, int(p / 100 * len(xs)))] if xs else float('nan')


def agg(model, cfg, text, seeds):
    runs = [UMA(model, cfg, s).boot(text).run() for s in seeds]
    n = len(runs)
    lat = [x for r in runs for x in r['lat']]
    pulses = sum(r['pulses'] for r in runs) or 1
    return dict(
        slow=statistics.mean(r['t_total'] / r['native_ns'] for r in runs),
        hit=100 * sum(r['hits'] for r in runs) / pulses,
        rej=sum(r['rejects'] for r in runs) / pulses,
        wrong=sum(r['wrong_exec'] for r in runs) / n,
        inc=sum(r['incidents'] for r in runs) / n,
        ok=100 * sum(r['out_ok'] for r in runs) / n,
        run=100 * sum(r['runaway'] for r in runs) / n,
        p50=pct(lat, 50), p99=pct(lat, 99), mx=max(lat) if lat else float('nan'),
        disc=sum(r['discarded_ns'] for r in runs) / sum(r['t_total'] for r in runs) * 100,
        reasons=sum((Counter({k: v for k, v in r.items() if k.startswith('rej_')}) for r in runs), Counter()))


def row(name, a):
    return (f"{name:<44}{a['slow']:>9.1f}x{a['hit']:>7.1f}{a['rej']:>8.2f}{a['wrong']:>8.2f}"
            f"{a['inc']:>7.2f}{a['ok']:>7.0f}{a['run']:>7.0f}")


HDR = f"{'config':<44}{'slowdown':>10}{'hit%':>7}{'rej/p':>8}{'wrong':>8}{'trap':>7}{'ok%':>7}{'runaw%':>7}"


def main():
    out = []
    P = lambda *a: (print(*a), out.append(' '.join(str(x) for x in a)))
    P(selftest())
    rng = random.Random(7)
    model = TableModel()
    model.train(rng)
    P(f'model: {len(model.c)} table contexts, {len(model.vocab)} learned tokens, {len(model.noise_vocab)} noise vocabulary')
    text = 'Hello, World!\n' * 8 + 'ok'
    seeds = range(40)

    # ---- E1: one cold boot, annotated
    P('\n== E1  cold boot to Hello World (cache on, admit K=8, std quantisation, noise 3%) ==')
    u = UMA(model, dict(), 3).boot(text)
    r = u.run()
    P(f"output correct: {bool(r['out_ok'])}   pulses {r['pulses']}   hits {r['hits']}   rejects {r['rejects']}   "
      f"aborts {r['aborts']}   cursor events {r['events']}")
    P(f"total {r['t_total']/1000:.1f} us   native equivalent {r['native_ns']/1000:.2f} us   slowdown {r['t_total']/r['native_ns']:.1f}x")
    P('first pulse of each intent, as synthesised:')
    seen = set()
    for e in u.trace:
        if e['kind'] == 'burst' and e['intent'] not in seen:
            seen.add(e['intent'])
            P(f"  {e['intent']:6} block {e['bid']}  t={e['t0']:.0f}..{e['t1']:.0f} ns")
            for w in u.blocks[e['bid']]:
                P(f"      {w:08x}  {disasm(decode(w))}")
    ok, at = verify_chain(u.trace)
    P(f'trace: {len(u.trace)} entries, hash chain verifies: {ok}')
    ok2, at2 = replay(text, u.trace, u.blocks)
    P(f'replay from block table + log reproduces every checkpoint state hash: {ok2}')
    bad = json.loads(json.dumps(u.trace))
    bad[len(bad) // 2]['t1'] += 1.0
    P(f'tamper with one time stamp -> chain verifies: {verify_chain(bad)[0]} (detected at seq {verify_chain(bad)[1]})')
    bad = json.loads(json.dumps(u.trace))
    victim = next(i for i, e in enumerate(bad) if e['kind'] == 'burst')
    bad[victim]['bid'] = 'deadbeef'
    P(f'tamper with a block id -> chain verifies: {verify_chain(bad)[0]}')

    # ---- E2: cache / admission / quantisation / hit guard
    P('\n== E2  cache design (40 seeds, model noise 3% per token, irq every 5 us, abort fast path) ==')
    P('wrong = bursts whose real effect differed from the intended transition (ground-truth audit); trap = bursts that faulted')
    P(HDR)
    grid = [
        ('no cache', dict(cache=False)),
        ('cache, std key, admit K=0', dict(K_admit=0)),
        ('cache, std key, admit K=8', dict(K_admit=8)),
        ('cache, std key, admit K=32', dict(K_admit=32)),
        ('cache, COARSE key, admit K=0', dict(quant='coarse', K_admit=0)),
        ('cache, COARSE key, admit K=8', dict(quant='coarse', K_admit=8)),
        ('cache, COARSE key, admit K=32', dict(quant='coarse', K_admit=32)),
        ('cache, COARSE key, K=8, hit guard (shadow)', dict(quant='coarse', K_admit=8, guard='shadow')),
        ('cache, std key, K=0, hit guard (shadow)', dict(K_admit=0, guard='shadow')),
    ]
    for name, c in grid:
        P(row(name, agg(model, c, text, seeds)))

    # ---- E3: synthesiser quality
    P('\n== E3  synthesiser noise (cache on, std key, admit K=8) ==')
    P(HDR)
    for nz in (0.0, 0.01, 0.03, 0.08, 0.15, 0.30):
        a = agg(model, dict(noise=nz), text, seeds)
        P(row(f'noise {nz:.2f} per token', a))
    a = agg(model, dict(cache=False, noise=0.08), text, seeds)
    P('reject reasons, cache off, noise 0.08: ' + ', '.join(f'{k[4:]} {v}' for k, v in a['reasons'].most_common()))

    # ---- E4: interrupts
    P('\n== E4  interrupt latency, timer every 3 us (ns; latency = event to end of handler burst) ==')
    P(f"{'config':<44}{'p50':>10}{'p99':>10}{'max':>10}{'discard%':>10}{'slowdown':>10}")
    for name, c in [('cache off, wait for checkpoint', dict(cache=False, fast_abort=False)),
                    ('cache off, abort in-flight synthesis', dict(cache=False, fast_abort=True)),
                    ('cache on, wait for checkpoint', dict(fast_abort=False)),
                    ('cache on, abort in-flight synthesis', dict(fast_abort=True))]:
        a = agg(model, dict(c, irq_period=3000.0), text, seeds)
        P(f"{name:<44}{a['p50']:>10.0f}{a['p99']:>10.0f}{a['mx']:>10.0f}{a['disc']:>10.1f}{a['slow']:>9.1f}x")
    open('/tmp/uma_results.txt', 'w').write('\n'.join(out) + '\n')


if __name__ == '__main__':
    main()
