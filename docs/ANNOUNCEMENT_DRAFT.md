# Announcement draft

Do not send this. It is a draft for the group, after the cleanup pull request is merged.

---

open-hvac-edge is in shape to try.

It is the edge side of an HVAC data path. ASHRAE 223P is the model. Haystack is only an overlay. Two drivers write the same `bacnet://` identity: BACpypes3 for BACnet/IP, and a read-only baskStream client for Niagara. Writes are off. A live system is polled only for the points you list. History is DuckDB on the edge, with Parquet on disk. There is no in-browser database.

The test bench is two Pis. Pi 1 talks BACnet to a JCI FEC (read, read-multiple, priority array, scan). Pi 2 talks to a Niagara station over an authenticated WebSocket and does not write. Both export a 223P Turtle file, and `reconcile` compares them. The pictures are in the README:

https://github.com/bbartling/open-hvac-edge/blob/main/docs/architecture/test-bench.svg

https://github.com/bbartling/open-hvac-edge/blob/main/docs/architecture/campus.svg

To try it: clone the repo, `python -m pip install -e ".[all]"`, then `python -m pytest -q`. The Pi quick start is `examples/edge-microdot` (`--profile bacnet` or `--profile niagara`) with a points file. The command-line export and reconcile steps are in `docs/cli.md`.

Open-FDD 5.0.22 is not tagged yet. When it is, this repo will pin `roles/openfdd-roles.json` (1.1.0-draft.1, QUDT wire units) and the rules and UI bundles by version and sha256. Until that tag exists, those files are not vendored and the edge keeps its current role table.
