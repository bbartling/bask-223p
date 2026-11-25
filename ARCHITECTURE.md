# Architecture

## Primary path

BACpypes3 scan *code* is not copied into this repository. The scan side is a document
(TTL or CSV) that already uses `bacnet://<device>/<type>,<instance>` identity, typically
written by a BACpypes3 BACnet-to-RDF run. The Niagara side is a read-only baskStream
session. `reconcile` compares the two by that URI.

```mermaid
flowchart LR
  NS[Niagara station] -->|baskStream WSS, read-only, SCRAM| Snap[discovery snapshot]
  Snap --> Exp[export-223p]
  Exp --> TTL[223P + Brick Turtle]
  Scan["BACpypes3 bacnet-rdf TTL or CSV\n(external; not vendored)"] --> Rec[reconcile]
  TTL --> Rec
  Rec --> Out[matched / missing in Niagara / missing in scan]
  TTL --> Shacl[official 223P SHACL]
```

Both sides go through one reference builder, so the reference node for a given
(device, object type, object instance) is identical whether it came from Niagara or
from a scan. Niagara display names stay labels; only BACnet-sourced names become
`bacnet:object-name`.

The client is read-only: write operations are refused locally, the SCRAM server
signature is verified, and credentials are never printed or written to outputs.

The emitted graph is an *external-reference* model: `s223:Equipment` (not a guessed
subclass), `s223:Property` subclasses with their superclass chain so official NodeShapes
apply without a reasoner, `s223:hasProperty`, QUDT unit and quantity kind, enumeration
kind, and `s223:BACnetExternalReference` / Brick `ref:BACnetReference`. It is not a
complete 223 topology (no connection points or sensors).

## Validating the TTL against official 223P

1. [OntoEnv](https://github.com/gtfierro/ontoenv-rs) manages ontology imports and versions.
   Add the generated TTL (it `owl:imports` the 223P ontology IRI) and the public 223P file
   at `https://open223.info/223p.ttl`, then take the imports closure (QUDT and other
   dependencies come along). `copy_closure` returns a mutable `rdflib.Graph` on stable
   releases.
2. [Shifty](https://github.com/gtfierro/shifty) (`pyshifty`) validates the model against
   that closure (SHACL and SHACL-AF) and can run inference separately.
3. If those packages are not installed, `export-223p --shapes 223p.ttl` runs the same
   shapes with pyshacl. `conforms` means no Violations; Warnings such as a missing
   `s223:Sensor` that `observes` a property are expected for this graph style.

## Secondary tools

```
FQPN / point inventory CSV  ----------------------->  lexical audit (suggestions only)
           |                                            |
           v                                            v
    local RDF topology  <------------------------- human approval
           |
           +----> reviewed BACnet addresses --> SPARQL / binding tables
           |
           +----> authored 223/Brick templates (optional, not bundled)
                             |
                             +----> ontology imports closure
                                           |
                             +-------------+---> SHACL validation
```

Approved class assignments are opt-in. A passing SHACL run is not proof of HVAC
engineering correctness or write safety.
