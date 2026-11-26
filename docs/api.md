# Edge HTTP API

## Points file

The BACnet Compose service does not pass `--points`. When that flag is omitted, the CLI loads `EDGE_POINTS_FILE`. The default is `/app/examples/edge-microdot/var/points.csv`, and Compose forwards the variable with that default. `./var` on the host is mounted at that directory.

`--points` wins over `EDGE_POINTS_FILE`. A missing fallback file is not a startup error. The process logs `no points are configured` and, with no rows loaded, the BACnet profile polls nothing.

### CSV format

Header and columns, in order:

```csv
device,object,property,units,label
10001,"analog-input,1",present-value,degC,Zone temperature
```

| Column | Required | Meaning |
| --- | --- | --- |
| `device` | yes | BACnet device instance |
| `object` | yes | `type,instance`, quoted because the value contains a comma |
| `property` | no | BACnet property. Empty means `present-value` |
| `units` | no | Unit string stored with the point |
| `label` | no | Display name |

Rows missing `device` or `object`, or whose `object` has no comma, are skipped.
