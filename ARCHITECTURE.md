# Architecture

The diagrams are [docs/architecture/test-bench.svg](docs/architecture/test-bench.svg), [docs/architecture/campus.svg](docs/architecture/campus.svg), and [docs/architecture/central.svg](docs/architecture/central.svg). The bench picture is an MS/TP FEC, a BASrouter, BACnet/IP to Pi 1 and the Niagara 4 station, baskStream from that station to Pi 2, and a collector pull of `GET /api/export.zip`. Reconcile runs in the collector. Notes on what each Pi runs are in [docs/architecture/README.md](docs/architecture/README.md).

223P validation is `validate_223p` in `src/bask_223p/validate/shacl.py`: OntoEnv 0.6.4 resolves the vendored import closure offline, and Shifty 0.6.0 runs with its defaults at `minimum_severity="Violation"`. A pySHACL cross-check is opt-in and does not set `conforms`. The edge boxes do not run that validation. They load a Turtle file that was already checked.
