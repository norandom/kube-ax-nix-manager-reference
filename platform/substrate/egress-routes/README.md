# Egress routes (tools/egress-watcher)

Each new actor in atespace `default` gets the policy of the **longest matching name prefix**.
`"policy": null` (or no match) means no policy, so **no egress**: only loopback works.

| File | When | Effect |
|---|---|---|
| `scoped.json` | **live since 2026-10-06** (Substrate main `715f2daa`, host names enforced) | per-prefix host lists agreed with OpenUltraSAST on 2026-10-05 |
| `open.json` | fallback | public HTTPS for all; verifiers no egress |

`scoped.json` must **not** be used on v0.3.0: without #2015 a host-name rule lets any host through,
so verifiers would go from "no egress" to "any host". `scripts/platform.sh` picks the file from
`EGRESS_ROUTES` (default `scoped`).

Prefixes (from OpenUltraSAST, 2026-10-05):

| Prefix | Hosts (443) |
|---|---|
| `ousast-engine-search-exec-` | any public host |
| `ousast-engine-search-verify-` | files.because-security.com |
| `ousast-engine-search-probe-` | files.because-security.com |
| `ousast-engine-pass-` | files.because-security.com |
| `ousast-engine-replay-` | files.because-security.com, github.com |
| any other `ousast-engine-` name | **no egress** (scoped.json; OpenUltraSAST, 2026-10-06: a name outside the list is their bug) |
| `egtest-files-`, `egtest-replay-` | test-only (scripts/egress-test.py): as verify / replay |
| everything else | any public host |

The NetworkPolicy `atenet-egress-public-https-only` caps every route to public IPv4 on TCP 443.
