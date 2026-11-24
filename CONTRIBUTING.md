# Contributing to Tri

## Development Setup

Tri uses standard-library Python 3.11+ for the core store, CLI, worker, and tests, and standard-library Go 1.22+ for `native/tri-server`.

```sh
python3 -m pip install -e .
cd native/tri-server && go build -o tri-server . && cd -
```

## Verification Before Opening a Pull Request

Run the unit suite, contract check, Go vet/race tests, and native conformance and leak suites:

```sh
python3 -m unittest discover -s tests -v
python3 -m tri.contracts --check
(cd native/tri-server && go vet ./... && go test -race -count=1 ./...)
TRI_CONFORMANCE_NATIVE="$PWD/native/tri-server/tri-server" \
  python3 -m unittest discover -s tests/conformance -t tests -v
TRI_CONFORMANCE_NATIVE="$PWD/native/tri-server/tri-server" \
  python3 -m unittest discover -s tests -p test_leak_sweep.py -v
```

## Updating Contracts

Whenever a CLI command, schema, route, or diagnostic code changes in `tri/cli.py`, regenerate `contracts/v0/` and include the updated files in your commit:

```sh
python3 -m tri.contracts --write
python3 -m tri.contracts --check
```

Any new read-shaped command must also be covered (or explicitly exempted) in `tests/test_leak_sweep.py`.
