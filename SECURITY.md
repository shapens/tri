# Security Policy

## Reporting a Vulnerability

Please report security vulnerabilities privately through [GitHub Security Advisories](https://github.com/shapens/tri/security/advisories/new) rather than opening a public issue. Include reproduction steps, the platform and sandbox backend used, and the expected versus observed boundary.

## Security Model and Known Boundaries

Tri isolates untrusted workspace commands, pins authenticated HTTP callers to per-actor bearer tokens, and prevents hidden paths, private packages, and leased secrets from leaking through reads, diffs, search, logs, or exports. Every read route is checked against `tests/test_leak_sweep.py`.

Important boundaries to understand before running untrusted code:

| Area | Boundary and Caveat |
| :--- | :--- |
| Non-loopback serving | `tri serve` and `tri-server` refuse to bind beyond loopback without `--auth-file` (unless `--allow-unauthenticated` is explicitly passed) and default `--run-sandbox` to `required`. |
| Linux / macOS local sandboxes | `bubblewrap` (Linux) and Seatbelt (macOS) restrict writes to the run directory, block reads of `TRI_HOME` and common user credential paths, scrub the environment, and disable network access by default. They share the host kernel; for multi-tenant or adversarial workloads, move execution onto remote runners using `TRI_RUN_SANDBOX_BACKEND=container` with gVisor (`runsc`) or Kata/Firecracker (`kata-fc`). |
| Windows local sandbox | Runs commands at low integrity inside a Job Object with process, CPU, and memory limits. Without administrator rights it cannot enforce network denial (`network_enforced: false` is recorded on every run) and does not hide arbitrary user-profile files outside `TRI_HOME`. |
| Container secret leases | When using `--with-lease` with the OCI container backend, leased values live in the container's environment while running; anyone with access to the container engine socket can inspect container configuration until removal. |
