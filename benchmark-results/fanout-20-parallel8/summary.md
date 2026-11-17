# Fan-out disk benchmark

- Created: `2026-11-17T05:59:48Z`
- Platform: `Darwin 25.3.0 arm64`
- Attempts: `20` (`8` at a time); source files: `1000` x `1024` B; dependency files: `2000` x `2048` B

| Target | Status | Measure | Peak allocated | End allocated | Per attempt | Wall s | Checks | Backends |
| --- | --- | --- | ---: | ---: | ---: | ---: | ---: | --- |
| tri | ok | volume-used | 36.0 MiB | 29.1 MiB | 1.5 MiB | 11.246 | 20 | clone |
| tri-copy | ok | volume-used | 59.0 MiB | 23.9 MiB | 1.2 MiB | 13.156 | 20 | copy |
| tri-mount | ok | volume-used | 53.0 MiB | 49.4 MiB | 2.5 MiB | 38.09 | 20 | clone |
| tri-mount-link | ok | volume-used | 41.7 MiB | 37.3 MiB | 1.9 MiB | 30.944 | 20 | clone |
| git | ok | volume-used | 275.0 MiB | 274.1 MiB | 13.7 MiB | 27.364 | 20 | worktree |

`volume-used` is the dedicated APFS volume's space-used figure (as `df` reports it), so blocks shared by clones count once. `st_blocks` sums per-file allocation and counts every clone as a full copy, which overstates Tri on clone backends.
