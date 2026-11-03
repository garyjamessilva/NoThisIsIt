# UMA simulator results

2000 tasks, seed 1. Timing numbers are placeholder assumptions (synthesis 2 us + 100 ns/instr, check 200 ns + 20 ns/instr, 1 ns/instr execute, 50 us partial reconfiguration). Read the shapes, not the absolute values.

## E1. Burst size x trace cache (no synthesis errors)
Slowdown = total time / time for a conventional core running the same work at 1 ns/instr.

| burst | cache entries | slowdown | cache hit | irq p99 (us) |
|---|---|---|---|---|
| 4 | off | 788.2x | 0% | 7.88 |
| 4 | 64 | 12.2x | 99% | 2.69 |
| 4 | 1024 | 3.3x | 100% | 2.28 |
| 8 | off | 466.9x | 0% | 8.30 |
| 8 | 64 | 15.0x | 97% | 3.15 |
| 8 | 1024 | 2.8x | 100% | 2.99 |
| 16 | off | 306.4x | 0% | 9.16 |
| 16 | 64 | 21.3x | 92% | 4.07 |
| 16 | 1024 | 3.1x | 99% | 3.75 |
| 32 | off | 226.7x | 0% | 10.65 |
| 32 | 64 | 31.3x | 82% | 6.00 |
| 32 | 1024 | 4.6x | 98% | 5.40 |

## E2. Does speculation help? (cache off, burst 16)
Synthesis and check cost are scaled together. Speculation overlaps the pulse for block N+1 with the burst of block N, so it can hide at most one burst time (18 ns here).

| synth + check latency per 16-instr block | no speculation | speculation |
|---|---|---|
| 4,120 ns | 306.43x | 304.75x |
| 412 ns | 27.57x | 26.47x |
| 41 ns | 3.74x | 2.64x |
| 16 ns | 2.17x | 1.18x |

## E3. Synthesiser error rate vs checker strength (cache off)
`wrong blocks run` = executed blocks whose result on the actual state differs from the intended block (ground truth, invisible to the architecture; harmless corruptions such as OR for ADD on disjoint bits do not count). `safety incidents` = executed illegal opcodes or out-of-bounds stores. `tasks correct` = final state equals the intended result.

| error rate | checker | wrong blocks run | safety incidents | tasks correct | fallbacks | slowdown |
|---|---|---|---|---|---|---|
| 0.1% | no checker | 22 | 13 | 99% | 0 | 262.5x |
| 0.1% | safety rules only | 10 | 0 | 100% | 0 | 306.3x |
| 0.1% | full, 90% coverage | 2 | 0 | 100% | 0 | 306.4x |
| 0.1% | full, 100% coverage | 0 | 0 | 100% | 0 | 306.2x |
| 1% | no checker | 219 | 164 | 92% | 0 | 263.5x |
| 1% | safety rules only | 108 | 0 | 96% | 0 | 307.7x |
| 1% | full, 90% coverage | 15 | 0 | 99% | 0 | 307.8x |
| 1% | full, 100% coverage | 0 | 0 | 100% | 0 | 308.4x |
| 5% | no checker | 1116 | 880 | 68% | 0 | 263.5x |
| 5% | safety rules only | 551 | 0 | 84% | 0 | 312.6x |
| 5% | full, 90% coverage | 58 | 0 | 98% | 0 | 315.9x |
| 5% | full, 100% coverage | 0 | 0 | 100% | 0 | 316.9x |

## E3b. A leaky checker plus a cache
The cache means far fewer synthesis events, so fewer chances for an error. But a wrong block that escapes the checker and enters the cache is replayed on every hit (`from cache` counts those replays), so the errors that do occur are persistent.

| error rate | checker coverage | cache entries | wrong blocks run | of which from cache | tasks correct | slowdown |
|---|---|---|---|---|---|---|
| 5% | 90% | off | 58 | 0 | 97.8% | 315.9x |
| 5% | 90% | 1024 | 0 | 0 | 100.0% | 3.2x |
| 20% | 50% | off | 1220 | 0 | 70.3% | 342.0x |
| 20% | 50% | 1024 | 596 | 589 | 95.4% | 3.3x |
| 30% | 30% | off | 2693 | 0 | 55.4% | 357.4x |
| 30% | 30% | 1024 | 649 | 629 | 92.5% | 3.3x |

## E4. Custom-op fabric strategies (kernel 0 is about 60% of tasks, cache 1024)
The custom MAC op replaces 6 instructions with 3 per element, saving about 3 ns per element. One 50 us reconfiguration therefore needs roughly 17,000 elements of kernel-0 work to pay back, so whether staging or promotion wins depends on how much work follows.

| fabric mode | total ms | slowdown | full reconfigurations |
|---|---|---|---|
| none | 1.76 | 2.66x | 0 |
| rewire | 729.06 | 1103.31x | 14544 |
| prestaged | 1.80 | 2.73x | 1 |
| promoted | 1.61 | 2.44x | 3 |

## E5. Interrupt latency (arrival to handler start)
Interrupts are serviced at checkpoints only, so a pending interrupt also waits out any synthesis already in flight, not just the remaining burst.

| handler path | burst | interrupts | p50 (us) | p99 (us) |
|---|---|---|---|---|
| synthesised, no cache | 4 | 25744 | 4.430 | 7.880 |
| synthesised, no cache | 16 | 10060 | 5.168 | 9.162 |
| synthesised, no cache | 32 | 7557 | 6.175 | 10.651 |
| synthesised, cached handler | 4 | 100 | 0.012 | 2.275 |
| synthesised, cached handler | 16 | 110 | 0.274 | 3.752 |
| synthesised, cached handler | 32 | 162 | 1.665 | 5.396 |
| fixed fast path | 4 | 103 | 0.015 | 1.677 |
| fixed fast path | 16 | 95 | 0.031 | 3.661 |
| fixed fast path | 32 | 166 | 1.812 | 5.865 |

## E6. Can the fast path abort an in-flight pulse? (cache off, every block synthesised)
An aborted pulse discards its partial work and restarts, so the cost is wasted synthesis time. A burst already executing still runs to its end, so the abort removes the synthesis wait but not the burst wait. Cache is off so that synthesis is the dominant exposure; with a cache most interrupts already land in a burst (E5).

| handler path | burst | interrupts | aborts | p50 (us) | p99 (us) | max (us) | slowdown |
|---|---|---|---|---|---|---|---|
| synthesised handler, serviced at checkpoint | 4 | 25744 | 0 | 4.430 | 7.880 | 12.359 | 788.2x |
| synthesised handler, serviced at checkpoint | 16 | 10060 | 0 | 5.168 | 9.162 | 12.327 | 306.4x |
| synthesised handler, serviced at checkpoint | 32 | 7557 | 0 | 6.175 | 10.651 | 15.093 | 226.7x |
| fast path, serviced at checkpoint | 4 | 21883 | 0 | 1.366 | 2.671 | 2.696 | 673.8x |
| fast path, serviced at checkpoint | 16 | 8663 | 0 | 2.074 | 4.101 | 4.148 | 261.7x |
| fast path, serviced at checkpoint | 32 | 6274 | 0 | 2.896 | 6.026 | 6.083 | 193.1x |
| fast path, aborts in-flight pulse | 4 | 23603 | 23563 | 0.010 | 0.010 | 0.016 | 721.3x |
| fast path, aborts in-flight pulse | 16 | 9483 | 9449 | 0.010 | 0.010 | 0.028 | 290.4x |
| fast path, aborts in-flight pulse | 32 | 7574 | 7540 | 0.010 | 0.010 | 0.042 | 225.4x |

