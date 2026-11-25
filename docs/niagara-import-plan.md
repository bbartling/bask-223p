# Niagara import path (plan only)

This is a concrete interchange plan, not a Niagara module and not a station-config change. Tridium does not currently support native Turtle or ASHRAE 223P import. Keep the reference graph in Turtle; hand Niagara a CSV/JSON point-binding table.

The intended desktop demo loop:

1. In Niagara (later, by the station user): create BACnet proxy points for a local test-bench device under the existing Niagara 4.15 lab station (HTTPS). The tool does not write to the station.
2. This tool builds a Turtle graph from a bench BACnet inventory or a BACpypes3-style site-scan TTL. Each point gets a Brick `ref:BACnetReference` (`bacnet:object-identifier`, `bacnet:object-name`, `bacnet:objectOf` a device, plus `ref:BACnetURI` in the Clause Q.8 `bacnet://device/type,instance` form) and the 223P `BACnetExternalReference` (`device-identifier`, `object-identifier`, `property-identifier`). Device/object IRIs match the BACpypes3 BACnet-to-RDF scan layout so a scan graph and this export can be joined.
3. From that Turtle, export a point-binding table (CSV and JSON) with device instance, object identifier, optional Brick class (only when a reviewer approved one), and Haystack tags. A Niagara BACnet Point Manager import, a tag dictionary, or a small station module can consume that table to tag and confirm bindings. It is not a TTL/223P import.
4. Read back through nHaystack (`GET /haystack/about` and `GET /haystack/read?filter=point`, Zinc or JSON). The `check-haystack` command verifies every required point is present, carries the expected tags (at least `point`), and is bound to the right BACnet object. If the live servlet is not reachable, run the same checker against an exported or fixture grid (`examples/fec-haystack-grid.json`). Use pyhaystack or the same REST calls for a manual double-check. Read-only only; no point writes.

## Lab station probe (read-only)

If the nHaystack servlet is enabled, unauthenticated `GET https://<station>/haystack/about` may still redirect to the station form login (HTTP 302, no `WWW-Authenticate: Basic`). The live `--url` path does not send credentials; if a login is required, the command reports that and exits.

Do not run a BACnet scan from the same host as a Niagara BACnet server that already owns UDP 47808. Use an existing scan TTL/CSV and nHaystack read-back instead.

For a live `check-haystack --url`, a read-only station user must be able to reach `/haystack/about` without a form-login redirect (nHaystack historically expected HTTP Basic and Zinc). Until that is available, run the checker against an exported or fixture grid (`examples/fec-haystack-grid.json`).

## Commands

```powershell
python -m bask_223p export-bacnet-refs --input examples/fec-test-bench-points.csv --base https://example.org/lab --out outputs/bacnet-refs.ttl --manifest outputs/point-bindings.csv
python -m bask_223p export-bindings --data outputs/bacnet-refs.ttl --out outputs/point-bindings.csv
python -m bask_223p check-haystack --expected outputs/point-bindings.csv --grid examples/fec-haystack-grid.json --out outputs/haystack-check
# later, after the servlet accepts unauthenticated or you run a manual login yourself:
# python -m bask_223p check-haystack --expected outputs/point-bindings.csv --url https://localhost/haystack --out outputs/haystack-check
```

The live `--url` path does not send credentials. If the station asks for a login, the command reports that and exits.
