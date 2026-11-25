# Running bask-223p against a Niagara station (read-only)

Install the signed `baskStream-rt.jar` from [rbhans/bask-stream](https://github.com/rbhans/bask-stream)
(the jar at the repository root is API 1.12) into the Niagara `modules` folder, restart the
station, and add `BASkStreamService` from the `baskStream` palette. Confirm `servletName` is
`stream`.

Set these on the service (all exist in API 1.12):

- `writesEnabled` = false (the default is true)
- `modelEditsEnabled` = false, `securityEditsEnabled` = false
- `allowedPathPatterns` = `slot:/Drivers/*` (the default is `slot:/*`)
- a dedicated station user with read-only rights on Drivers
- optional `requireAuthorizationHeader` = true (then pass `--ws-auth-header` to `export-223p`)

`agentPointWrites`, agent profiles, and `resolve` arrive in API 1.13/1.14. This client gates
those operations on the station's `capabilities` reply, so it also works against a 1.12 station.

Check `https://<station>/stream/health` after login: the JSON should report `apiVersion`. Then:

```powershell
bask-223p export-223p --station https://localhost --insecure `
  --env-file path/to/station.env --base https://example.org/lab `
  --save-snapshot outputs/station/snapshot.json --out outputs/station/site-223p.ttl
```

The env file holds `NIAGARA_USER` and `NIAGARA_PASS`. Values are never printed.

To join the export with a direct BACpypes3 scan:

```powershell
bask-223p reconcile --niagara outputs/station/site-223p.ttl --scan path/to/site-scan.ttl --out outputs/reconcile
```
