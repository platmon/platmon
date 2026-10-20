# platmon-native candidate 1 (for verification)

Fixed 2026-10-20 (after the ninth review), before the final comparison. Every result judged against this
candidate names it; a change to the product code or its build makes a new candidate with its own hashes. A
change to the measuring tools alone keeps the candidate and is recorded with the tools' hashes and why.
Not a release: nothing here is published, tagged or installed as the service.

## Source

| | |
| --- | --- |
| repository | platmon, branch `exp/rust-runtime` (PR #93, draft) |
| commit | `7f066f0026834e46a6ac259c507938370c4bbeb7`, clean working tree |
| `native/Cargo.lock` | `66da52673d8d0bc8d65d4904bf8ad16c3cc2c1a0bf2d1a6ac123659079ed245b` |
| `native/Cargo.toml` | `c3628703978d9eef0ca4e9d05fdeec2426d547d0f8d6059cbdfed09bdd15cefb` |
| `native/build.sh` | `b8e06f5ee489574b56107bdac8a48385fd6bb69dfd14a338b12ebb7f1f2e6e8e` |
| dependencies | `libc` 0.2.172 only (the `mimalloc` chain is optional and not built) |

## Build

`native/build.sh`, unchanged:

| | |
| --- | --- |
| image | `rust:1.88-slim`, `rust@sha256:38bc5a86d998772d4aec2348656ed21438d20fcdce2795b56ca434cf21430d89` |
| toolchain | rustc 1.88.0 (6b00bc388 2025-06-23), cargo 1.88.0 (873a06493 2025-05-10) |
| command | `cargo build --release --locked --no-default-features --target T`, after `cargo test --release --locked --no-default-features` (28 passed) |
| targets | `x86_64-unknown-linux-musl`, `aarch64-unknown-linux-musl` |
| features | none: the system allocator (musl's malloc) |
| `RUSTFLAGS` | `-C linker=rust-lld -C target-feature=+crt-static` |
| profile | release: `opt-level = 3`, `strip = true`, `panic = "abort"` |

## Binaries

| target | SHA-256 | `file` | allocator mark |
| --- | --- | --- | --- |
| x86_64 | `027e35b1c9251b9d28d1a40a843f286c657fdd0499198cc1bf6ee80d9e50cae4` | ELF 64-bit x86-64, static-pie linked, stripped; no `INTERP` | `platmon-native-allocator=system;` (also `build-info`) |
| aarch64 | `ff97aa9ad4e9dd2881db846d1410940d9826a955ec72ab885f62d7ceb2802975` | ELF 64-bit ARM aarch64, statically linked, stripped; no `INTERP` | `platmon-native-allocator=system;` |

Two builds of the commit gave the same two hashes.

## Running it

| | |
| --- | --- |
| command | `platmon-native serve INI --web TREE/frontends/web --app-version 1.3.0` |
| INI | written by `service_ab.py`: `[core] interval = 1.0`, `[http] bind = 127.0.0.1`, `port = 19797`; everything else the defaults (history 600 s, replay 600 s and 16 MiB, slow groups on) |
| user | the login user (uid 1000) on each host, no root, no service unit: `rain` on the PC and the Pi, `jorin` on the Orin |
| web assets | `frontends/web` of the v1.3.0 tree below |

## The comparison

| | |
| --- | --- |
| baseline | the Python service 1.3.0: `git archive v1.3.0` (commit `e767176a8d5cbeb0c6bf4d04e71ceca9344b65b7`, archive SHA-256 `28a9edc8d4407497bd2d0eba414492bcf5ab72fdab55094ba149ca728786aa2a`), each host's `python3`: 3.14.4 (PC), 3.10.12 (Orin), 3.13.5 (Pi) |
| tools | at the commit above: `service_ab.py` `0e44f17ca588617fea16a0a26379e985aa3251d1cd662e515e670dd44985ff63`, `ab_report.py` `f7f6a7c01ef29334ad1cdb992201c5504d7c10d0fc0d62ccb646ea791460905a`, `walk.py` `d13ee78e2df2176978309d1a076da6691dd7d37d2492c872598ae7fb6b680605`, `worker.py` `5d0e96163e2d08dafeb687216ba283b392707944f2f8659a2814204239a43471` |
| load | `service_ab.py` `PHASES`: warmup 30 s, windows of 120 s (w1 at 30 s, w2 at 1290 s with full buffers, w3 the Hub-like poller), 480 s disconnected, the reconnect backlog fetched 5 times, 3 rounds of 4 pollers fetching one backlog at once, 120 s settle, 120 s after; order A B B A per host |
| hosts | the PC (x86_64), the Jetson Orin Nano, the Raspberry Pi 4, at once |
| judgement | README, "Final comparison, set before it was run": `ab_report.py --pss-budget` with each host's budget |

## Results

The final comparison (2026-10-21): passed on the PC, the Orin and the Pi, every run
([README](README.md#final-comparison-candidate-1)). The deployed form: not run (needs root on a host).
