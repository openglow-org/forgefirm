# forgetest - the ForgeFIRM release acceptance tool

The daemon behind `http://<machine>:8090/` on the dev image: runs the
acceptance catalog against the machine, keeps the append-only result log,
decides which results still apply to the image that is running, exports
the release artifact `scripts/release.sh` gates on, and serves the bench
diagnostics page. The contract - catalog, campaigns, fingerprints,
inheritance, the gate, the coverage rule - is
[the Acceptance page of the documentation site](https://docs.forgefirm.org/developers/acceptance/).

## Run the host tests

    cd forgetest
    python3 -m unittest discover -s tests -v

## Run the daemon on a workstation (against a mock or a manifest file)

    FORGETEST_DATA=/tmp/ft FORGETEST_MANIFEST=../tree-manifest.json \
    FORGECTRL_URL=http://<machine> python3 -m forgetest --port 8090

`scripts/manifest-from-tree.py` produces `tree-manifest.json` from the recipe
pins; the coverage lint is `python3 -m forgetest.coverage --manifest ...`.

## Environment

| Variable | Default | Purpose |
|---|---|---|
| `FORGETEST_DATA` | `/data/forgetest` | results.jsonl, bench.jsonl, token, export/ |
| `FORGETEST_MANIFEST` | `/etc/forgefirm-manifest.json` | the image manifest |
| `FORGETEST_PORT`, `FORGETEST_HOST` | 8090, 0.0.0.0 | listener |
| `FORGETEST_BENCH_DIR` | `/usr/share/forgetest/bench` | the installed bench scripts |
| `FORGETEST_BENCH_DATA` | `<FORGETEST_DATA>/bench` | passed to bench tools: where they keep their data files (with `GF_HOST=127.0.0.1` and the panel token in `GF_TOKEN`) |
| `FORGETEST_MARKER` | `/run/forgetest.active` | takeover marker |
| `FORGECTRL_URL`, `FORGECTRL_TOKEN_FILE` | `http://127.0.0.1`, `/data/forgefirm/panel.token` | forgectrl client (HTTP; the token authorizes writes from the board) |
| `FORGECTRL_TLS_URL` | `https://127.0.0.1` | forgectrl over HTTPS (self-signed, unverified), for the login test |
| `GF_SYSFS_ROOT` | `/sys/glowforge/` | kernel module sysfs |
| `GRBL_HOST`, `GRBL_PORT` | 127.0.0.1, 23 | Grbl TCP |

## Adding a test

Register it in the subsystem module under `forgetest/suite/` with
`@test(...)`: id `subsystem.name`, kind, hardware, `mode` (the controller
mode the test needs; the runner switches to it first), `covers`,
`requires`, `always`, steps. The body gets a `Context` (`log`, `check`, `fail`,
`prompt`, `confirm`, `instruct`, `sleep`, `evidence`, `forgectrl`, `sysfs`,
`grbl`, `takeover`). Return normally for PASS, raise `runner.Failed` for
FAIL. Then run the unit tests and the coverage lint.
