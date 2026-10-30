# ADR 0002: The API contract, not the Python service's answers

> **TL;DR**
> - Accepted 2026-10-30: the server no longer has to answer as the removed Python service did; it keeps
>   the versioning contract (HTTP API, `platmon.ini` keys and syntax, command options).
> - The recorded answers (`native/tools/equivalence/golden/`) stay as regression data: an intended change
>   rewrites them with the harnesses' `--bless`, and the diff is reviewed with the change.

- Date: 2026-10-30
- Status: Accepted
- Scope: platmon-server's answers, its equivalence checks and the comments that cite Python behavior

## Context

The native server was ported from the Python service and checked against it on fake trees, configurations,
the runtime and live HTTP. Before the Python service was removed (after 1.6.0), its answers were recorded
in `native/tools/equivalence/golden/`, and the checks compared the server with those recordings, byte for
byte. Nothing could rewrite them: every intended change to an answer meant editing recorded JSON by hand,
so in practice the Python service's behavior stayed binding after it was gone.

Part of that behavior is not a contract users depend on: HTTP/0.9 requests, error texts built like
Python's `repr`, `/text` closing the connection where Python would have raised, the error messages of a
failed collection. The versioning contract covers the HTTP API, `platmon.ini` keys and command options.

## Decision

The server keeps the versioning contract, not the Python service's answers.

| Kept | Free to change, with a reviewed `--bless` |
| --- | --- |
| The HTTP API's paths, fields, types and meanings ([api.md](../reference/api.md)) | Answers outside the API's documented fields: error pages and their texts, HTTP/0.9 handling |
| `platmon.ini` keys, defaults and syntax (sections, comments, continuation lines, case): users' files are written for it | Configuration error messages, when a clearer one helps |
| Command options of `platmon-server` and `platmon` | `/text` and the CLI's screen, kept equal to each other |

The recorded answers stay in the repository as regression data. `fixture.sh --bless`,
`runtime_fixture.py BINARY --bless` and `config_fixture.py BINARY --bless` run the server alone and
rewrite the files from its answers, only where every step gave what it expects and the server exited
with 0. A bless of an unchanged server reproduces the files; the first one replaced two collection error
messages, which the harness does not compare, with the server's own.

## Consequences

- A change to an answer comes with its bless in the same pull request, so the diff of `golden/` shows
  what changed for a client.
- Comments in `native/src/` that justify behavior by "as Python did" are reasons to revisit, not
  constraints; a change to such behavior is made on its merits and recorded here or in the API reference.
- The JSON text format (`json.dumps` style) stays: clients parse JSON, and changing the spacing buys nothing.
