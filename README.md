# humus

A comment on each GitHub pull request that records every version of the
force-pushed branch, newest first:

## Revision history

| Version | Changes from previous version | Changes from branch base |
| --- | --- | --- |
| 3 (current) | `ef0152a3`..`7b0def91` | `zzzzzzzz`..`7b0def91` |
| 2 | `c0ffee12`..`ef0152a3` | `zzzzzzzz`..`ef0152a3` |
| 1 | Initial version | `zzzzzzzz`..`c0ffee12` |

Run it after a force-push and it keeps one comment up to date on the pull
request for the branch you pushed. It does not care what pushed: `git push
--force`, jj, anything that moves the branch.

## Install

Needs Python 3.10+ and a [GitHub token](#authentication).

```
uv tool install .
```

Or run it from a checkout without installing:

```
python3 -m humus --help
```

## Use

Name the push branch or the pull request number:

```
humus --branch feature/user-repairs
humus --pr 1347
```

The repo comes from `git remote get-url origin`; pass `--repo OWNER/NAME` to
override it. Run humus again after the next force-push; it rewrites the same
comment in place.

A bare `humus` is inert: it prints help and exits. Apart from the `jj-push`
subcommand, humus never pushes anything and never writes a comment until you
name a target.

| flag | meaning |
| --- | --- |
| `--pr N` | annotate pull request N |
| `--branch NAME` | annotate the open pull request for this branch |
| `--repo OWNER/NAME` | override the repo from the origin remote |
| `--dry-run`, `-n` | print the comment body to stdout, do not write it |

Preview without touching GitHub:

```
humus --branch feature/user-repairs --dry-run
```

## Use with jj

The `jj-push` subcommand connects `jj push` to humus: it runs `jj git push`,
reads the bookmark on the working-copy commit, and updates the comment for
that branch. The branch is named explicitly, so a bare `humus` still does
nothing on its own.

Point a jj alias at the subcommand:

```console
jj config set --user aliases.push '["util", "exec", "--", "humus", "jj-push"]'
```

`jj push` then pushes as usual and updates the revision history comment for
the pushed branch. Anything after `jj-push` on the command line is forwarded
to `jj git push`. The subcommand skips the comment when the working-copy
commit has no bookmark. It assumes the pushed change carries one bookmark,
which is the common case; use `--pr` manually when it does not.

## The comment

GitHub treats the first push to a branch as version 1 and every force-push
after it as the next version. humus rebuilds those version numbers from the
PR's timeline, marks the newest one `(current)`, and links each row to the
compare view between the two commits and to the submitted commit. It shows
the newest 20 versions.

GitHub records a force-push a moment after it lands. humus compares the
branch's current head to the newest recorded event and adds a row when they
differ, so the version you just pushed appears immediately. A later run never
adds a duplicate for a version it already wrote down.

## Authentication

humus reads `GITHUB_TOKEN` or `GH_TOKEN`, then falls back to the token of
`gh` (the GitHub CLI) if you are logged in with it.

## History

humus is an extraction of the revision-history comment from
[jj-stack](https://github.com/bos/jj-stack) (Apache-2.0). It keeps the same
comment marker, so it adopts and rewrites any such comment jj-stack already
left behind.

## Development

```
python3 -m unittest discover -s tests
```
