#!/usr/bin/env python3
"""
UMA micro-burst simulator
=========================

Models the loop from the revised paper:

    Pulse (synthesise block) -> Check -> Burst (execute) -> Checkpoint -> repeat

Three components are BLACK BOXES with narrow interfaces. Swap them for real
models / RTL / measured numbers without touching the loop:

    Synthesiser   emit(spec_block) -> block        cost(n) -> ns
    Checker       check(block, spec, regs, mem) -> (accepted, reason)   cost(n) -> ns
    Fabric        charge() -> ns   (cost of making a custom op available)

The synthesiser here is an ORACLE + NOISE stand-in: it returns the intended
block, then corrupts it with probability `err`. It says nothing about how a
real learned model would behave; it only lets us study what the *rest* of the
architecture does with a given error rate.

ALL TIMING NUMBERS ARE PLACEHOLDER ASSUMPTIONS, not measurements. The point of
the sweeps is shape (where the knees are), not absolute figures.

The checker is the "mathematical checker" from the paper: it shadow-executes the
candidate block and (with probability `coverage`) compares the result against the
intended transition, plus always applies static safety rules (opcodes, store bounds).

Usage:  python uma_sim.py [--tasks 2000] [--seed 1] [--out results.md]
"""
import argparse
import random
from collections import OrderedDict
from dataclasses import dataclass, replace

MASK = 0xFFFFFFFF
LEGAL = {'ADDI', 'ADD', 'SUB', 'XOR', 'AND', 'OR', 'SLLI', 'SRLI', 'LW', 'SW', 'MAC'}
ALU = ['ADD', 'SUB', 'XOR', 'AND', 'OR']
STORE_LO, STORE_HI = 128, 256   # stores outside this region are safety violations


# ----------------------------------------------------------------- tiny ISA
def run_instr(regs, mem, ins):
    """Execute one instruction in place. Returns None or an incident string."""
    op, rd, a, b = ins
    if op == 'SW':                       # SW src=rd, ptr=a
        addr = regs[a]
        ok = STORE_LO <= addr < STORE_HI
        mem[addr & 0xFF] = regs[rd]
        return None if ok else 'bounds'
    if op == 'ADDI':
        v = (regs[a] + b) & MASK
    elif op == 'ADD':
        v = (regs[a] + regs[b]) & MASK
    elif op == 'SUB':
        v = (regs[a] - regs[b]) & MASK
    elif op == 'XOR':
        v = regs[a] ^ regs[b]
    elif op == 'AND':
        v = regs[a] & regs[b]
    elif op == 'OR':
        v = regs[a] | regs[b]
    elif op == 'SLLI':
        v = (regs[a] << b) & MASK
    elif op == 'SRLI':
        v = regs[a] >> b
    elif op == 'LW':
        v = mem[regs[a] & 127]
    elif op == 'MAC':                    # custom op: rotl3(rd) + rs1, one cycle
        r = regs[rd]
        v = ((((r << 3) | (r >> 29)) & MASK) + regs[a]) & MASK
    else:
        return 'opcode'
    if rd:
        regs[rd] = v
    return None


# ------------------------------------------------------------ workload model
def make_kernels(K, seed, custom):
    """K per-element loop bodies. Kernel 0 is the rotate-and-accumulate op that has
    a 3-instruction custom-extension form (MAC) and a 6-instruction base form."""
    rng = random.Random(seed)
    base0 = [('LW', 3, 2, 0), ('SLLI', 6, 1, 3), ('SRLI', 7, 1, 29),
             ('OR', 6, 6, 7), ('ADD', 1, 6, 3), ('ADDI', 2, 2, 1)]
    cust0 = [('LW', 3, 2, 0), ('MAC', 1, 3, 0), ('ADDI', 2, 2, 1)]
    ks = [cust0 if custom else base0]
    srcs, dsts = [1, 3, 6, 7], [1, 6, 7]
    for _ in range(1, K):
        L = rng.randint(3, 8)
        body = [('LW', 3, 2, 0)]
        has_sw = False      # at most one store pair per body: x4 then advances <= 1 per
        while len(body) < L - 1:   # element, so 100 elements stay inside the store region
            r = rng.random()
            if r < 0.1 and len(body) < L - 2 and not has_sw:
                has_sw = True
                body += [('SW', 3, 4, 0), ('ADDI', 4, 4, 1)]
            elif r < 0.4:
                body.append((rng.choice(['SLLI', 'SRLI']), rng.choice(dsts),
                             rng.choice(srcs), rng.randint(1, 7)))
            else:
                body.append((rng.choice(ALU), rng.choice(dsts),
                             rng.choice(srcs), rng.choice(srcs)))
        body.append(('ADDI', 2, 2, 1))
        ks.append(body)
    return ks


def make_workload(tasks, K, alpha, seed):
    """Tasks pick a kernel by Zipf popularity; each runs n_elem loop iterations."""
    rng = random.Random(seed)
    w = [1.0 / (i + 1) ** alpha for i in range(K)]
    out = []
    for _ in range(tasks):
        kid = rng.choices(range(K), w)[0]
        n_elem = rng.randint(20, 100)
        mem = [rng.getrandbits(32) for _ in range(128)] + [0] * 128
        out.append((kid, n_elem, mem, rng.getrandbits(32)))
    return out


def init_regs(x1):
    return [0, x1, 0, 0, 128, MASK, 0, 0]


def differs(block, spec, regs, mem):
    """True if running `block` from this state gives a different result than `spec`."""
    r1, m1, r2, m2 = regs[:], mem[:], regs[:], mem[:]
    for ins in block:
        run_instr(r1, m1, ins)
    for ins in spec:
        run_instr(r2, m2, ins)
    return r1 != r2 or m1 != m2


# ------------------------------------------------------------- black boxes
class Synthesiser:
    """BLACK BOX. Oracle + noise. Replace emit() with a real model."""

    def __init__(self, err, rng, base_ns, per_instr_ns):
        self.err, self.rng, self.base, self.per = err, rng, base_ns, per_instr_ns

    def cost(self, n):
        return self.base + self.per * n

    def emit(self, spec):
        blk = list(spec)
        if self.rng.random() >= self.err:
            return blk
        i = self.rng.randrange(len(blk))
        kind = self.rng.random()
        op, rd, a, b = blk[i]
        if kind < 0.6:                              # functional error
            if op in ALU:
                blk[i] = (self.rng.choice([o for o in ALU if o != op]), rd, a, b)
            elif op in ('SLLI', 'SRLI'):
                blk[i] = (op, rd, a, b + 1)
            else:
                blk[i] = ('ADDI', rd or 1, a, 1)
        elif kind < 0.8:                            # out-of-bounds store
            blk[i] = ('SW', 3, 2, 0)
        else:                                       # illegal opcode
            blk[i] = ('BAD', 0, 0, 0)
        return blk


class Checker:
    """BLACK BOX. mode: none | bounds | full. `coverage` = chance the equivalence
    (intended-transition) check runs on a given block in full mode."""

    def __init__(self, mode, coverage, rng, base_ns, per_instr_ns):
        self.mode, self.cov, self.rng = mode, coverage, rng
        self.base, self.per = base_ns, per_instr_ns

    def cost(self, n):
        return 0.0 if self.mode == 'none' else self.base + self.per * n

    def check(self, block, spec, regs, mem):
        if self.mode == 'none':
            return True, None
        r, m = regs[:], mem[:]
        for ins in block:
            res = run_instr(r, m, ins)
            if res:
                return False, res
        if self.mode == 'full' and self.rng.random() < self.cov:
            r2, m2 = regs[:], mem[:]
            for ins in spec:
                run_instr(r2, m2, ins)
            if r != r2 or m != m2:
                return False, 'spec'
        return True, None


class Fabric:
    """BLACK BOX. Cost of having the custom op available for a block that uses it."""

    def __init__(self, mode, reconf_ns, switch_ns, promote_after):
        self.mode, self.reconf, self.switch, self.after = mode, reconf_ns, switch_ns, promote_after
        self.uses = 0
        self.resident = False
        self.staged = False
        self.reconfs = 0
        self.ns = 0.0

    def charge(self):
        c = 0.0
        if self.mode == 'rewire':               # fabric dissolves after every burst
            c = self.reconf
            self.reconfs += 1
        elif self.mode == 'prestaged':          # variants staged once, then a mux switch
            c = self.switch
            if not self.staged:
                c += self.reconf
                self.reconfs += 1
                self.staged = True
        elif self.mode == 'promoted':           # reconfigure until promoted, then permanent
            self.uses += 1
            if not self.resident:
                c = self.reconf
                self.reconfs += 1
                if self.uses >= self.after:
                    self.resident = True
        self.ns += c
        return c


# ------------------------------------------------------------------ simulator
@dataclass
class P:
    burst: int = 16
    cache: int = 0                 # trace-cache entries (0 = off)
    speculate: bool = False
    err: float = 0.01              # synthesiser error rate per block
    checker: str = 'full'          # none | bounds | full
    coverage: float = 1.0
    fabric: str = 'none'           # none | rewire | prestaged | promoted
    alpha: float = 1.0             # Zipf popularity of kernels
    kernels: int = 24
    fast_path: bool = False        # fixed interrupt handler, no synthesis
    abort_synth: bool = False      # fast path may abort an in-flight pulse (synth + check)
    max_retries: int = 3
    # placeholder timing assumptions (ns)
    t_instr: float = 1.0
    t_ckpt: float = 2.0
    t_lookup: float = 5.0
    synth_base: float = 2000.0
    synth_per: float = 100.0
    check_base: float = 200.0
    check_per: float = 20.0
    fallback: float = 3000.0       # slow reference path when retries run out
    reconf: float = 50_000.0
    switch: float = 20.0
    promote_after: int = 3
    irq_rate: float = 1 / 20_000   # per ns (one per 20 us)
    fast_ns: float = 10.0


def simulate(p, workload, seed=1):
    rng = random.Random(seed + 1)
    custom = p.fabric != 'none'
    kern = make_kernels(p.kernels, 7, custom)
    kern_base = make_kernels(p.kernels, 7, False)
    synth = Synthesiser(p.err, rng, p.synth_base, p.synth_per)
    chk = Checker(p.checker, p.coverage, rng, p.check_base, p.check_per)
    fab = Fabric(p.fabric, p.reconf, p.switch, p.promote_after)
    cache = OrderedDict()

    def cache_put(key, val):
        if p.cache:
            cache[key] = val
            cache.move_to_end(key)
            while len(cache) > p.cache:
                cache.popitem(last=False)

    t = 0.0
    next_irq = rng.expovariate(p.irq_rate)
    lats = []
    s = dict(hits=0, misses=0, rejects=0, fallbacks=0, bad_exec=0, incidents=0,
             retired=0, conv_instr=0, blocks=0, tasks_ok=0, bad_hit=0, aborts=0)

    for kid, n_elem, mem0, x1 in workload:
        body, body_base = kern[kid], kern_base[kid]
        L, total = len(body), n_elem * len(body)
        s['conv_instr'] += n_elem * len(body_base)
        regs, mem = init_regs(x1), mem0[:]
        er, em = init_regs(x1), mem0[:]
        for i in range(total):
            run_instr(er, em, body[i % L])
        pos, credit = 0, 0.0
        while pos < total:
            # checkpoint: service any interrupts that have arrived
            while next_irq <= t:
                arr = next_irq
                if p.fast_path:
                    lat = (t - arr) + p.fast_ns
                    t += p.fast_ns + 6 * p.t_instr
                else:
                    if p.cache and ('irq',) in cache:
                        cache.move_to_end(('irq',))
                        c = p.t_lookup
                    else:
                        c = synth.cost(6) + chk.cost(6)
                        cache_put(('irq',), True)
                    lat = (t - arr) + c
                    t += c + 6 * p.t_instr
                lats.append(lat)
                next_irq = arr + rng.expovariate(p.irq_rate)
            n = min(p.burst, total - pos)
            spec = [body[(pos + i) % L] for i in range(n)]
            key = (kid, pos % L, n)
            s['blocks'] += 1
            from_cache = False
            if p.cache and key in cache:
                cache.move_to_end(key)
                blk = cache[key]
                t += p.t_lookup
                s['hits'] += 1
                from_cache = True
            else:
                if p.cache:
                    s['misses'] += 1
                attempts = 0
                while True:
                    blk = synth.emit(spec)
                    full = synth.cost(n) + chk.cost(n)
                    c = full
                    if attempts == 0 and p.speculate:
                        c = max(0.0, c - credit)
                    if p.fast_path and p.abort_synth:
                        # An interrupt arriving during the pulse aborts it: partial work is
                        # discarded (no architectural state was touched), the fast path runs
                        # at once, and the pulse restarts from scratch (speculation credit lost).
                        while next_irq < t + c:
                            arr = next_irq
                            t = arr
                            lats.append(p.fast_ns)
                            t += p.fast_ns + 6 * p.t_instr
                            next_irq = arr + rng.expovariate(p.irq_rate)
                            c = full
                            s['aborts'] += 1
                    t += c
                    ok, why = chk.check(blk, spec, regs, mem)
                    if ok:
                        break
                    s['rejects'] += 1
                    attempts += 1
                    if attempts > p.max_retries:
                        blk = spec
                        t += p.fallback
                        s['fallbacks'] += 1
                        break
                cache_put(key, blk)
            # ground-truth metric (not visible to the architecture): does this block's
            # result on the actual state differ from the intended block's result?
            if blk != spec and differs(blk, spec, regs, mem):
                s['bad_exec'] += 1
                if from_cache:
                    s['bad_hit'] += 1
            if any(i[0] == 'MAC' for i in blk):
                t += fab.charge()
            for ins in blk:
                if run_instr(regs, mem, ins):
                    s['incidents'] += 1
            burst = n * p.t_instr + p.t_ckpt
            t += burst
            credit = burst
            pos += n
            s['retired'] += n
        s['tasks_ok'] += (regs == er and mem == em)

    conv = s['conv_instr'] * p.t_instr
    lats.sort()
    pct = lambda q: lats[int(q * (len(lats) - 1))] / 1000 if lats else float('nan')
    tot_lookups = s['hits'] + s['misses']
    return dict(
        total_ms=t / 1e6, slowdown=t / conv,
        hit=(s['hits'] / tot_lookups) if tot_lookups else 0.0,
        rejects=s['rejects'], fallbacks=s['fallbacks'], bad_exec=s['bad_exec'],
        incidents=s['incidents'], ok=s['tasks_ok'] / len(workload),
        irq_p50=pct(0.5), irq_p99=pct(0.99), irqs=len(lats), bad_hit=s['bad_hit'], aborts=s['aborts'], irq_max=pct(1.0),
        reconfs=fab.reconfs, blocks=s['blocks'])


# ---------------------------------------------------------------- experiments
def md(headers, rows):
    out = ['| ' + ' | '.join(headers) + ' |', '|' + '---|' * len(headers)]
    out += ['| ' + ' | '.join(str(c) for c in r) + ' |' for r in rows]
    return '\n'.join(out)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--tasks', type=int, default=2000)
    ap.add_argument('--seed', type=int, default=1)
    ap.add_argument('--out', default='results.md')
    a = ap.parse_args()

    wl_cache = {}

    def run(**kw):
        p = replace(P(), **kw)
        key = p.alpha
        if key not in wl_cache:
            wl_cache[key] = make_workload(a.tasks, p.kernels, p.alpha, a.seed)
        return simulate(p, wl_cache[key], a.seed)

    out = ['# UMA simulator results', '',
           f'{a.tasks} tasks, seed {a.seed}. Timing numbers are placeholder assumptions '
           '(synthesis 2 us + 100 ns/instr, check 200 ns + 20 ns/instr, 1 ns/instr execute, '
           '50 us partial reconfiguration). Read the shapes, not the absolute values.', '']

    # E1 -------------------------------------------------------------------
    rows = []
    for B in (4, 8, 16, 32):
        for C in (0, 64, 1024):
            r = run(burst=B, cache=C, err=0.0)
            rows.append([B, C or 'off', f"{r['slowdown']:.1f}x", f"{r['hit']*100:.0f}%",
                         f"{r['irq_p99']:.2f}"])
    out += ['## E1. Burst size x trace cache (no synthesis errors)',
            'Slowdown = total time / time for a conventional core running the same work at 1 ns/instr.',
            '', md(['burst', 'cache entries', 'slowdown', 'cache hit', 'irq p99 (us)'], rows), '']

    # E2 -------------------------------------------------------------------
    rows = []
    for sc in (1.0, 0.1, 0.01, 0.004):
        kw = dict(cache=0, err=0.0, synth_base=2000 * sc, synth_per=100 * sc,
                  check_base=200 * sc, check_per=20 * sc)
        r0 = run(speculate=False, **kw)
        r1 = run(speculate=True, **kw)
        lat = (2000 + 100 * 16 + 200 + 20 * 16) * sc
        rows.append([f'{lat:,.0f} ns', f"{r0['slowdown']:.2f}x", f"{r1['slowdown']:.2f}x"])
    out += ['## E2. Does speculation help? (cache off, burst 16)',
            'Synthesis and check cost are scaled together. Speculation overlaps the pulse for block N+1 with the burst of block N, so it can hide at most one burst time (18 ns here).',
            '', md(['synth + check latency per 16-instr block', 'no speculation', 'speculation'], rows), '']

    # E3 -------------------------------------------------------------------
    rows = []
    for err in (0.001, 0.01, 0.05):
        for mode, cov, label in (('none', 1.0, 'no checker'), ('bounds', 1.0, 'safety rules only'),
                                 ('full', 0.9, 'full, 90% coverage'), ('full', 1.0, 'full, 100% coverage')):
            r = run(cache=0, err=err, checker=mode, coverage=cov)
            rows.append([f'{err*100:g}%', label, r['bad_exec'], r['incidents'],
                         f"{r['ok']*100:.0f}%", r['fallbacks'], f"{r['slowdown']:.1f}x"])
    out += ['## E3. Synthesiser error rate vs checker strength (cache off)',
            '`wrong blocks run` = executed blocks whose result on the actual state differs from the intended block (ground truth, invisible to the architecture; harmless corruptions such as OR for ADD on disjoint bits do not count). `safety incidents` = executed illegal opcodes or out-of-bounds stores. `tasks correct` = final state equals the intended result.',
            '', md(['error rate', 'checker', 'wrong blocks run', 'safety incidents', 'tasks correct',
                    'fallbacks', 'slowdown'], rows), '']

    # E3b ------------------------------------------------------------------
    rows = []
    for err, cov in ((0.05, 0.9), (0.2, 0.5), (0.3, 0.3)):
        for C in (0, 1024):
            r = run(cache=C, err=err, checker='full', coverage=cov)
            rows.append([f'{err*100:g}%', f'{cov*100:g}%', C or 'off', r['bad_exec'], r['bad_hit'],
                         f"{r['ok']*100:.1f}%", f"{r['slowdown']:.1f}x"])
    out += ['## E3b. A leaky checker plus a cache',
            'The cache means far fewer synthesis events, so fewer chances for an error. But a wrong block that escapes the checker and enters the cache is replayed on every hit (`from cache` counts those replays), so the errors that do occur are persistent.',
            '', md(['error rate', 'checker coverage', 'cache entries', 'wrong blocks run',
                    'of which from cache', 'tasks correct', 'slowdown'], rows), '']

    # E4 -------------------------------------------------------------------
    rows = []
    for mode in ('none', 'rewire', 'prestaged', 'promoted'):
        r = run(cache=1024, err=0.0, fabric=mode, alpha=2.0)
        rows.append([mode, f"{r['total_ms']:.2f}", f"{r['slowdown']:.2f}x", r['reconfs']])
    out += ['## E4. Custom-op fabric strategies (kernel 0 is about 60% of tasks, cache 1024)',
            'The custom MAC op replaces 6 instructions with 3 per element, saving about 3 ns per element. '
            'One 50 us reconfiguration therefore needs roughly 17,000 elements of kernel-0 work to pay back, '
            'so whether staging or promotion wins depends on how much work follows.',
            '', md(['fabric mode', 'total ms', 'slowdown', 'full reconfigurations'], rows), '']

    # E5 -------------------------------------------------------------------
    rows = []
    for label, kw in (('synthesised, no cache', dict(cache=0)),
                      ('synthesised, cached handler', dict(cache=1024)),
                      ('fixed fast path', dict(cache=1024, fast_path=True))):
        for B in (4, 16, 32):
            r = run(err=0.0, burst=B, **kw)
            rows.append([label, B, r['irqs'], f"{r['irq_p50']:.3f}", f"{r['irq_p99']:.3f}"])
    out += ['## E5. Interrupt latency (arrival to handler start)',
            'Interrupts are serviced at checkpoints only, so a pending interrupt also waits out any synthesis already in flight, not just the remaining burst.',
            '', md(['handler path', 'burst', 'interrupts', 'p50 (us)', 'p99 (us)'], rows), '']

    # E6 -------------------------------------------------------------------
    rows = []
    for label, kw in (('synthesised handler, serviced at checkpoint', dict()),
                      ('fast path, serviced at checkpoint', dict(fast_path=True)),
                      ('fast path, aborts in-flight pulse', dict(fast_path=True, abort_synth=True))):
        for B in (4, 16, 32):
            r = run(err=0.0, burst=B, cache=0, **kw)
            rows.append([label, B, r['irqs'], r['aborts'], f"{r['irq_p50']:.3f}",
                         f"{r['irq_p99']:.3f}", f"{r['irq_max']:.3f}", f"{r['slowdown']:.1f}x"])
    out += ['## E6. Can the fast path abort an in-flight pulse? (cache off, every block synthesised)',
            'An aborted pulse discards its partial work and restarts, so the cost is wasted synthesis time. A burst already executing still runs to its end, so the abort removes the synthesis wait but not the burst wait. Cache is off so that synthesis is the dominant exposure; with a cache most interrupts already land in a burst (E5).',
            '', md(['handler path', 'burst', 'interrupts', 'aborts', 'p50 (us)', 'p99 (us)', 'max (us)',
                    'slowdown'], rows), '']

    text = '\n'.join(out)
    print(text)
    with open(a.out, 'w') as f:
        f.write(text + '\n')


if __name__ == '__main__':
    main()
