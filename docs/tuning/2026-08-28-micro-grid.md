# Baseline tuning — the grid sweep that chose `tidewalker` and `corsair`

*Run 2026-08-28 with `tools/tune/grid_search.py` at
`server/cogame_halite/micro.py`'s executor. Checklist item 7's second sentence
("The baseline's parameters were tuned with a grid harness, not guessed") is
this file plus that script; `tests/test_tuning.py` re-runs a small sweep and
asserts the shipped constants are the ones the runoff below selected.*

## Method

Seat 0 plays the candidate directive; seats 1-3 play the shipped baselines
(`corsair`, `tidewalker`, `corsair`), so the incumbent is always in the room.
Every episode is a real 400-turn `HaliteSim` episode driven by the shipped
`micro.compile_turn`, so the harness cannot drift from what the game plays.

No seat rotation is needed and none is applied: `populate_board` mirrors one
11 x 11 quartile into all four quadrants, so the board is exactly 4-fold
symmetric and the four seats are equivalent by construction (design note
§Seats, aliases, quadrants). The variance that is left is the seed.

Two numbers per candidate: **win rate** (seat 0's score is the highest of the
four) and **mean margin** over the best rival's score. The tables are sorted by
mean margin.

**Stage 1 — the grid**, 4 x 4 x 3 = 48 combinations x 6 seeds
(`1, 7, 42, 101, 2718, 31337`), 400 turns:

```sh
python tools/tune/grid_search.py --baseline tidewalker --seeds 6 --out -
python tools/tune/grid_search.py --baseline corsair    --seeds 6 --out -
```

**Stage 2 — the runoff.** Picking the maximum of a 48-cell grid measured on six
episodes is overfitting, so the top five plus the then-shipped combination were
replayed on **16 fresh seeds the grid never saw** (`RUNOFF_SEEDS`):

```sh
python tools/tune/grid_search.py --baseline tidewalker --runoff --seeds 16 \
  --only "mineFloor=200,returnAt=300,spawnUntil=200;mineFloor=200,returnAt=300,spawnUntil=300;mineFloor=200,returnAt=350,spawnUntil=200;mineFloor=100,returnAt=300,spawnUntil=200;mineFloor=150,returnAt=300,spawnUntil=200;mineFloor=100,returnAt=500,spawnUntil=300"
python tools/tune/grid_search.py --baseline corsair --runoff --seeds 16 \
  --only "mineFloor=200,returnAt=300,spawnUntil=340;mineFloor=200,returnAt=300,spawnUntil=300;mineFloor=200,returnAt=500,spawnUntil=300;mineFloor=200,returnAt=350,spawnUntil=300;mineFloor=150,returnAt=300,spawnUntil=300;mineFloor=150,returnAt=350,spawnUntil=340"
```

## Chosen

```
tidewalker  mineFloor=200  returnAt=300  spawnUntil=200
corsair     mineFloor=200  returnAt=300  spawnUntil=300
```

Both were the runoff winner by margin **and** by win rate, and both beat the
constants the design note guessed by a wide margin out of sample: the old
tidewalker (`100/500/300`) won **0 of 16** runoff episodes against 7 of 16 for
the winner, and the old corsair (`150/350/340`) won 5 of 16 against 10 of 16.
`yards` (2) and `stance` are unchanged: `yards` is clamped to [1, 4] by the
directive repair and is what an LLM seat actually steers, and `stance` is the
axis that makes corsair a raider rather than a miner.

The two baselines still play visibly different games — corsair hunts always and
at range 4, and spawns 100 turns longer — which is what
`tests/test_micro.py::test_an_all_corsair_episode_diverges_from_an_all_tidewalker_one`
asserts.

## Stage 2 — the runoff (16 fresh seeds, 400 turns)

*The `shipped:` line under each table names the constants that were shipped
**when the sweep ran** — the ones these results replaced. The opponents in
seats 1-3 were those same incumbents, which is what makes the comparison a
challenge to the sitting baselines rather than a self-match.*

### `tidewalker`

| `mineFloor` | `returnAt` | `spawnUntil` | win rate | mean score | mean margin over the best rival |
|---|---|---|---|---|---|
| 200 | 300 | 200 | 7/16 | 1681 | -909 |
| 200 | 300 | 300 | 2/16 | 1067 | -1693 |
| 200 | 350 | 200 | 3/16 | 1142 | -3142 |
| 100 | 300 | 200 | 2/16 | 719 | -3477 |
| 150 | 300 | 200 | 3/16 | 1209 | -3901 |
| 100 | 500 | 300 | 0/16 | 171 | -5742 |

Best by mean margin: `{'mineFloor': 200, 'returnAt': 300, 'spawnUntil': 200}`; shipped: `{'mineFloor': 100, 'returnAt': 500, 'spawnUntil': 300}`.

### `corsair`

| `mineFloor` | `returnAt` | `spawnUntil` | win rate | mean score | mean margin over the best rival |
|---|---|---|---|---|---|
| 200 | 300 | 300 | 10/16 | 11074 | +7470 |
| 200 | 300 | 340 | 10/16 | 10746 | +7434 |
| 200 | 350 | 300 | 9/16 | 5735 | +2090 |
| 150 | 300 | 300 | 9/16 | 5108 | +1045 |
| 200 | 500 | 300 | 4/16 | 2478 | -3718 |
| 150 | 350 | 340 | 5/16 | 1469 | -4696 |

Best by mean margin: `{'mineFloor': 200, 'returnAt': 300, 'spawnUntil': 300}`; shipped: `{'mineFloor': 150, 'returnAt': 350, 'spawnUntil': 340}`.

## Stage 1 — the grid (6 seeds, 400 turns)

### `tidewalker`

| `mineFloor` | `returnAt` | `spawnUntil` | win rate | mean score | mean margin over the best rival |
|---|---|---|---|---|---|
| 200 | 300 | 200 | 2/6 | 1346 | -966 |
| 200 | 300 | 300 | 2/6 | 664 | -1780 |
| 200 | 300 | 340 | 0/6 | 418 | -2342 |
| 200 | 350 | 200 | 1/6 | 2216 | -2411 |
| 100 | 300 | 200 | 3/6 | 999 | -2743 |
| 150 | 300 | 200 | 1/6 | 1742 | -2908 |
| 150 | 300 | 300 | 1/6 | 929 | -2937 |
| 150 | 300 | 340 | 1/6 | 836 | -3034 |
| 200 | 350 | 300 | 0/6 | 1039 | -3234 |
| 100 | 300 | 300 | 0/6 | 423 | -3397 |
| 100 | 300 | 340 | 0/6 | 304 | -3436 |
| 100 | 350 | 200 | 1/6 | 330 | -3638 |
| 200 | 350 | 340 | 0/6 | 661 | -3898 |
| 100 | 350 | 300 | 1/6 | 368 | -4172 |
| 100 | 350 | 340 | 0/6 | 236 | -4303 |
| 150 | 500 | 300 | 0/6 | 572 | -5240 |
| 150 | 500 | 340 | 0/6 | 336 | -5419 |
| 150 | 700 | 200 | 0/6 | 107 | -5426 |
| 50 | 350 | 200 | 0/6 | 311 | -5795 |
| 150 | 500 | 200 | 0/6 | 627 | -5798 |
| 150 | 700 | 340 | 0/6 | 16 | -5830 |
| 200 | 700 | 200 | 0/6 | 596 | -5878 |
| 200 | 700 | 300 | 0/6 | 387 | -5883 |
| 150 | 700 | 300 | 0/6 | 139 | -5910 |
| 200 | 700 | 340 | 0/6 | 334 | -5934 |
| 100 | 700 | 200 | 1/6 | 515 | -6343 |
| 50 | 350 | 300 | 0/6 | 33 | -6737 |
| 50 | 350 | 340 | 0/6 | 33 | -6737 |
| 100 | 700 | 300 | 0/6 | 124 | -6787 |
| 100 | 700 | 340 | 0/6 | 124 | -6787 |
| 200 | 500 | 300 | 0/6 | 392 | -6914 |
| 200 | 500 | 340 | 0/6 | 269 | -6975 |
| 100 | 500 | 300 | 0/6 | 703 | -7219 |
| 100 | 500 | 340 | 0/6 | 430 | -7246 |
| 100 | 500 | 200 | 0/6 | 752 | -7443 |
| 50 | 700 | 340 | 0/6 | 136 | -7554 |
| 50 | 300 | 300 | 0/6 | -95 | -7667 |
| 50 | 300 | 200 | 0/6 | 250 | -7676 |
| 50 | 300 | 340 | 0/6 | -28 | -7728 |
| 50 | 700 | 300 | 0/6 | -130 | -7773 |
| 50 | 700 | 200 | 0/6 | -147 | -8098 |
| 200 | 500 | 200 | 0/6 | 561 | -8192 |
| 50 | 500 | 200 | 1/6 | 521 | -8674 |
| 150 | 350 | 340 | 0/6 | 252 | -8830 |
| 50 | 500 | 300 | 0/6 | 290 | -8848 |
| 50 | 500 | 340 | 0/6 | 149 | -8969 |
| 150 | 350 | 300 | 0/6 | 334 | -9040 |
| 150 | 350 | 200 | 0/6 | 201 | -9097 |

Best by mean margin: `{'mineFloor': 200, 'returnAt': 300, 'spawnUntil': 200}`; shipped: `{'mineFloor': 100, 'returnAt': 500, 'spawnUntil': 300}`.

### `corsair`

| `mineFloor` | `returnAt` | `spawnUntil` | win rate | mean score | mean margin over the best rival |
|---|---|---|---|---|---|
| 200 | 300 | 340 | 2/6 | 7266 | +2395 |
| 200 | 300 | 300 | 2/6 | 7067 | +1812 |
| 200 | 500 | 300 | 4/6 | 6350 | +1213 |
| 150 | 300 | 300 | 2/6 | 5117 | +610 |
| 200 | 350 | 300 | 3/6 | 6656 | +60 |
| 150 | 300 | 340 | 2/6 | 5136 | +10 |
| 200 | 350 | 340 | 3/6 | 6407 | -35 |
| 150 | 350 | 300 | 2/6 | 4472 | -406 |
| 150 | 300 | 200 | 2/6 | 5791 | -474 |
| 200 | 500 | 340 | 3/6 | 4664 | -613 |
| 150 | 350 | 340 | 2/6 | 3507 | -1004 |
| 200 | 350 | 200 | 2/6 | 6820 | -1347 |
| 200 | 300 | 200 | 2/6 | 5334 | -2037 |
| 100 | 300 | 340 | 2/6 | 1868 | -2490 |
| 100 | 300 | 200 | 2/6 | 3288 | -3264 |
| 150 | 350 | 200 | 1/6 | 3576 | -3702 |
| 100 | 300 | 300 | 2/6 | 1934 | -3788 |
| 200 | 700 | 300 | 2/6 | 4988 | -4199 |
| 200 | 500 | 200 | 2/6 | 4295 | -4211 |
| 50 | 500 | 200 | 0/6 | -111 | -4252 |
| 200 | 700 | 200 | 1/6 | 7072 | -4600 |
| 50 | 500 | 340 | 0/6 | -82 | -5362 |
| 50 | 500 | 300 | 0/6 | -96 | -5389 |
| 100 | 350 | 300 | 0/6 | 506 | -5438 |
| 100 | 350 | 200 | 0/6 | 481 | -5910 |
| 100 | 350 | 340 | 0/6 | 285 | -6045 |
| 200 | 700 | 340 | 1/6 | 3352 | -6124 |
| 150 | 700 | 200 | 1/6 | 2270 | -7354 |
| 50 | 350 | 200 | 0/6 | 227 | -7756 |
| 50 | 350 | 340 | 0/6 | 97 | -7768 |
| 50 | 350 | 300 | 0/6 | 201 | -7890 |
| 150 | 700 | 340 | 1/6 | 850 | -8171 |
| 150 | 700 | 300 | 1/6 | 841 | -8472 |
| 100 | 500 | 200 | 1/6 | 817 | -9053 |
| 50 | 300 | 340 | 0/6 | 248 | -9079 |
| 50 | 300 | 300 | 0/6 | 266 | -9171 |
| 50 | 300 | 200 | 0/6 | 712 | -9217 |
| 100 | 700 | 300 | 0/6 | 74 | -9450 |
| 100 | 700 | 340 | 0/6 | 74 | -9450 |
| 100 | 700 | 200 | 0/6 | -107 | -9980 |
| 100 | 500 | 300 | 0/6 | 24 | -10626 |
| 100 | 500 | 340 | 0/6 | 24 | -10626 |
| 150 | 500 | 340 | 0/6 | 1117 | -11521 |
| 150 | 500 | 300 | 0/6 | 1161 | -11737 |
| 150 | 500 | 200 | 0/6 | 522 | -12706 |
| 50 | 700 | 300 | 0/6 | -96 | -13399 |
| 50 | 700 | 340 | 0/6 | -96 | -13399 |
| 50 | 700 | 200 | 0/6 | -98 | -13586 |

Best by mean margin: `{'mineFloor': 200, 'returnAt': 300, 'spawnUntil': 340}`; shipped: `{'mineFloor': 150, 'returnAt': 350, 'spawnUntil': 340}`.
