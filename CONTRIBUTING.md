# Contributing

> **TL;DR**
> - The most useful contribution is a **hardware report**: what platmon shows, and misses, on your board.
> - For a feature, open an issue first. platmon stays read-only, generic and free of runtime
>   dependencies; a change that needs otherwise is unlikely to be merged.
> - Building needs Linux, Docker and Python 3.9+. Run `native/build.sh` and `pytest` before a pull request.
> - Security problems go to [private reporting](SECURITY.md), not to an issue.

## Ways to help

| Contribution | Where |
| --- | --- |
| Tell us how platmon behaves on a board (also when everything works) | a *Hardware report* issue |
| Report wrong, missing or stale readings, a failed install or upgrade | a *Bug report* issue |
| Ask a question or propose an idea | Discussions |
| Fix a bug or add a reader for a device | a pull request, after an issue for anything beyond a small fix |

platmon is validated on few devices (the README lists them). A report with the output of
`/usr/lib/platmon/platmon-server collect` from a board that is not on the list is worth more than most
patches.

## What fits the project

- **Read-only.** platmon reads the device and serves what it read. It does not control fans, power
  modes or services, and the HTTP API has no write path.
- **Generic.** No code, configuration or names for one company, product or customer. Vendor names only
  describe compatibility.
- **No runtime dependencies.** The server depends on `libc` (and `windows-sys` on Windows); the `platmon`
  command and the web page use the Python standard library and plain JavaScript. A new dependency needs
  a reason no existing code can meet.
- **Licenses.** Dependencies under MIT, BSD-2-Clause, BSD-3-Clause or Apache-2.0 only. Do not copy code
  from GPL, LGPL or AGPL projects (jetson-stats and Glances among them), also not as a reference
  implementation to translate.
- **Sources.** Read `/proc`, `/sys` and what the kernel or the board's own tools expose. A reading that is
  not there is reported as unavailable with a reason, never estimated.
- **Cost.** A feature that is on by default must fit the
  [performance budget](docs/performance/budget.md); an expensive one is off by default or runs as a slow
  group on its own thread.

## Building and testing

You need a Linux host with Docker (the server is built in a throwaway Rust container, so no Rust toolchain
on the host) and Python 3.9 or later.

```sh
native/build.sh                                         # Rust unit tests, then static x86_64 and aarch64 builds
python3 -m pip install -r tests/requirements.txt        # pytest only
pytest                                                  # the terminal client, the web page, packaging
git diff --check
```

Run the server from the checkout (use the `aarch64-unknown-linux-musl` path on an Arm host):

```sh
native/target/x86_64-unknown-linux-musl/release/platmon-server serve platmon.ini --web frontends/web
python3 frontends/cli.py localhost
```

The [architecture guide](docs/development/architecture.md) describes the source layout and which module
may depend on which.

## Changes that touch the API or the screen

The HTTP API's paths and fields, the `platmon.ini` keys and defaults, and the command options are a
contract from 1.0.0 ([packaging and releases](docs/reference/packaging.md)). Adding a field or a key is a
minor change; removing or renaming one is a major change and needs an issue first.

The server's answers are compared with recorded ones (`native/tools/equivalence/golden/`), and CI also
checks that the released platmon-hub still reads the server:

```sh
B=native/target/x86_64-unknown-linux-musl/release/platmon-server
native/tools/equivalence/fixture.sh
python3 native/tools/equivalence/runtime_fixture.py $B
python3 native/tools/equivalence/config_fixture.py $B
python3 native/tools/equivalence/live_http.py $B
```

When a change is meant to alter an answer, rewrite the recorded ones with `--bless` on the first three
commands and include the diff of `golden/` in the pull request: reviewers read that diff as the change's
effect on clients ([ADR 0002](docs/development/adr/0002-api-contract-not-python-answers.md)).

The terminal client and the web page share their views, levels and colors through
`frontends/ui-spec.json`. Start a design change there; `tests/frontends/test_ui_spec.py` checks both
clients against it. `/text` (`native/src/render.rs`) is a port of the terminal client's plain screen, so a
change to that screen is made in both.

## Pull requests

- One logical change per pull request, with tests for a behavior change (say why when there are none).
- Commit messages follow [Conventional Commits](https://www.conventionalcommits.org/):
  `<type>(<scope>): <description>`, lowercase, imperative, no trailing period, for example
  `fix(http): limit the whole request, not each read, to the request timeout`.
- Update the documentation the change makes wrong, and add a line to `CHANGELOG.md` under `## Unreleased`
  for anything a user can notice.
- Say what you ran. The pull request template has the list.
- Do not bump `VERSION`, tag or edit release files; releases are made by the maintainer.

## AI-assisted work

This repository is developed with AI coding agents; `CLAUDE.md` and `.claude/` hold the rules they work
under. Every change, whoever wrote it, passes the same CI and is read by the maintainer before it is
merged. You may use such tools too. You are responsible for what you submit: that you have the right to
contribute it, that it follows the license rules above, and that you ran what you say you ran.

## License

By contributing you agree that your contribution is licensed under the
[Apache License 2.0](LICENSE), as section 5 of that license describes.
