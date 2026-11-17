Use Tri's secret lease workflow without leaking the secret value.

The verifier expects a local secret reference, a workspace-scoped lease, a check run that receives the secret through an environment variable, redacted log output, an audit record, and policy denial when a revoked lease is reused.
