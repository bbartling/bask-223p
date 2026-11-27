# Architecture

The diagrams are [docs/architecture/test-bench.svg](docs/architecture/test-bench.svg) and [docs/architecture/campus.svg](docs/architecture/campus.svg). Notes on what each Pi runs are in [docs/architecture/README.md](docs/architecture/README.md).

223P validation is `validate_223p` in `src/bask_223p/validate/shacl.py`: OntoEnv 0.6.4 resolves the vendored import closure offline, and Shifty 0.6.0 runs with its defaults at `minimum_severity="Violation"`. A pySHACL cross-check is opt-in and does not set `conforms`. The edge boxes do not run that validation. They load a Turtle file that was already checked.
