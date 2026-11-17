# Fan-out disk benchmark

- Created: `2026-11-17T05:14:30Z`
- Platform: `Darwin 25.3.0 arm64`
- Attempts: `10`; source files: `2000` x `1024` B; dependency files: `5000` x `2048` B

| Target | Status | Measure | Peak allocated | End allocated | Per attempt | Wall s | Checks | Backends |
| --- | --- | --- | ---: | ---: | ---: | ---: | ---: | --- |
| tri | ok | volume-used | 63.7 MiB | 60.6 MiB | 6.1 MiB | 64.056 | 10 | clone |
| tri-mount | ok | volume-used | 86.3 MiB | 83.2 MiB | 8.3 MiB | 43.854 | 10 | clone |
| git | ok | volume-used | 326.0 MiB | 325.5 MiB | 32.5 MiB | 25.524 | 10 | worktree |

`volume-used` is the dedicated APFS volume's space-used figure (as `df` reports it), so blocks shared by clones count once. `st_blocks` sums per-file allocation and counts every clone as a full copy, which overstates Tri on clone backends.
