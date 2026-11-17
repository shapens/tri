# Fan-out disk benchmark

- Created: `2026-11-17T05:19:30Z`
- Platform: `Darwin 25.3.0 arm64`
- Attempts: `50`; source files: `1000` x `1024` B; dependency files: `2000` x `2048` B

| Target | Status | Measure | Peak allocated | End allocated | Per attempt | Wall s | Checks | Backends |
| --- | --- | --- | ---: | ---: | ---: | ---: | ---: | --- |
| tri | ok | volume-used | 32.0 MiB | 30.4 MiB | 622.6 KiB | 52.341 | 50 | clone |
| tri-mount | ok | volume-used | 80.7 MiB | 79.1 MiB | 1.6 MiB | 76.523 | 50 | clone |
| git | ok | volume-used | 673.0 MiB | 672.6 MiB | 13.5 MiB | 51.927 | 50 | worktree |

`volume-used` is the dedicated APFS volume's space-used figure (as `df` reports it), so blocks shared by clones count once. `st_blocks` sums per-file allocation and counts every clone as a full copy, which overstates Tri on clone backends.
