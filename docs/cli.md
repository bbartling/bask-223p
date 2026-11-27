# Command line

The `bask_223p` commands write and check 223P Turtle. They do not poll a live network. The Pi services are in [examples/edge-microdot/README.md](../examples/edge-microdot/README.md).

Install: `python -m pip install -e ".[all]"`. Outputs belong in `outputs/`, which is gitignored.

## export-223p

Offline, from the bundled snapshot:

```bash
python -m bask_223p export-223p \
  --snapshot examples/baskstream-ahu-snapshot.json \
  --base https://example.org/lab \
  --manifest outputs/demo-223p/bindings.csv \
  --validate-out outputs/demo-223p/validate \
  --out outputs/demo-223p/site-223p.ttl
```

Live, against a station that is already running baskStream. Credentials stay in the env file and are not written to the output.

```bash
python -m bask_223p export-223p \
  --station https://localhost --insecure \
  --env-file path/to/station.env \
  --base https://example.org/lab \
  --save-snapshot outputs/station/snapshot.json \
  --out outputs/station/site-223p.ttl
```

`--limit` caps nodes per search (default 1000). `--mappings` applies reviewed class assignments. Equipment subclasses are not guessed from names. A BACnet scan CSV or TTL can be passed as `--input`. Niagara-only notes go in a sidecar `.niagara.ttl`. A raw scan graph is kept as `.scan-sidecar.ttl`.

Each bound point is an external reference: `s223:BACnetExternalReference`, `ref:BACnetURI` of the form `bacnet://<device>/<type>,<instance>`, and a QUDT unit local name such as `unit:DEG_F`. The graph imports `http://data.ashrae.org/standard223/1.0/model/all`. It does not invent ducts, connection points, or sensors.

## reconcile

```bash
python -m bask_223p reconcile \
  --niagara outputs/station/site-223p.ttl \
  --scan path/to/site-scan.ttl \
  --out outputs/reconcile \
  --merged-out outputs/reconcile/merged.ttl
```

The scan may be a BACpypes3 site-scan TTL or a CSV (`DeviceID,ObjType,ObjInst,...`). The join key is device instance plus object identifier. `--strict` exits 1 when either side is missing a point. `examples/bench-scan.ttl` is synthetic and is for tests.

## validate

```bash
python -m bask_223p validate \
  --data outputs/site-223p.ttl \
  --shapes path/to/223p.ttl \
  --online \
  --out outputs/validation
```

One function, `validate_223p`: OntoEnv 0.6.4 builds the offline import closure (QUDT 3.4.0, SHACL, SKOS, VAEM), then Shifty 0.6.0 runs with `minimum_severity="Violation"`. The pinned 223P file is the vendored copy of `https://open223.info/223p.ttl`, sha256 `08f80bd6e15052f28cdfd354bdf46ff96e5e1afdba36a4dbed5186fc1ea51c41`. `--online` copies that closure into the output directory. It does not fetch. `--cross-check pyshacl` is optional and does not set `conforms`. A missing engine is an error.

`conforms` means no SHACL Violations. A warning that an `ObservableProperty` has no `s223:Sensor` is expected for this external-reference graph.

## Other commands

```bash
python -m bask_223p export-bacnet-refs --input examples/fec-test-bench-points.csv \
  --base https://example.org/lab --out outputs/bacnet-refs.ttl
python -m bask_223p export-bindings --data outputs/bacnet-refs.ttl --out outputs/point-bindings.csv
python -m bask_223p check-haystack --expected outputs/point-bindings.csv \
  --grid examples/fec-haystack-grid.json --out outputs/haystack-check
```

`audit` and `build` are the older terminology and site-graph tools. They share the same validator.

Exit codes: 0 ok, 1 non-conformance or a strict reconcile mismatch, 2 bad input or setup.

Official 223P CI downloads the pinned file, sets `S223_SHAPES`, and fails if `tests/test_official_223p_shacl.py` is skipped.
