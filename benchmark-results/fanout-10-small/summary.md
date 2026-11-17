# Fan-out disk benchmark

- Created: `2026-11-17T05:20:26Z`
- Platform: `Darwin 25.3.0 arm64`
- Attempts: `10`; source files: `1000` x `1024` B; dependency files: `2000` x `2048` B

| Target | Status | Measure | Peak allocated | End allocated | Per attempt | Wall s | Checks | Backends |
| --- | --- | --- | ---: | ---: | ---: | ---: | ---: | --- |
| tri | ok | volume-used | 30.2 MiB | 28.7 MiB | 2.9 MiB | 13.932 | 10 | clone |
| tri-mount | ok | volume-used | 40.0 MiB | 38.4 MiB | 3.8 MiB | 13.447 | 10 | clone |
| git | ok | volume-used | 142.2 MiB | 141.8 MiB | 14.2 MiB | 10.384 | 10 | worktree |

`volume-used` is the dedicated APFS volume's space-used figure (as `df` reports it), so blocks shared by clones count once. `st_blocks` sums per-file allocation and counts every clone as a full copy, which overstates Tri on clone backends.
