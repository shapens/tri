# Fan-out disk benchmark

- Created: `2026-11-17T06:07:39Z`
- Platform: `Darwin 25.3.0 arm64`
- Attempts: `2` (`1` at a time)
- Repository: `/Users/bryantle/Documents/bryantleft/bruhs-ai`; install `cargo test -p bruhs-models --no-run --offline -q`; check `cargo test -p bruhs-models --offline -q`; env outputs `['target']` keyed by `['Cargo.lock', 'rust-toolchain.toml']`

| Target | Status | Measure | Peak allocated | End allocated | Per attempt | Wall s | Median attempt s | Checks | Backends |
| --- | --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | --- |
| tri | ok | volume-used | 1.8 GiB | 1.0 GiB | 522.3 MiB | 129.835 | 44.0 | 2 | clone |
| git | ok | volume-used | 2.8 GiB | 2.8 GiB | 1.4 GiB | 189.829 | 127.0 | 2 | worktree |

`volume-used` is the dedicated APFS volume's space-used figure (as `df` reports it), so blocks shared by clones count once. `st_blocks` sums per-file allocation and counts every clone as a full copy, which overstates Tri on clone backends.
