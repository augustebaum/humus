"""Maintain a PR comment listing the force-pushed versions of a patch.

Extracted from jj-stack's revision-history comments (Apache-2.0):
https://github.com/bos/jj-stack/blob/main/src/jj_stack/commands/submit/revision_comments.py

Run it after a force-push. Give it the branch or PR number; it finds the open
pull request for that branch on GitHub, reads the PR's force-push timeline,
renders a version table, and creates or updates one managed comment on the PR.
The jj-push subcommand runs `jj git push` and then does this for the pushed
branch. Bare invocation only prints help; nothing is pushed or written
without a target or the jj-push subcommand.
"""

from __future__ import annotations

import argparse
import functools
import json
import os
import re
import ssl
import subprocess
import sys
import urllib.error
import urllib.request
from collections.abc import Sequence
from dataclasses import dataclass, replace
from typing import Any

VERSION_LIMIT = 20
MARKER = "<!-- jj-stack-revision-history -->"
SHORT_COMMIT_LENGTH = 8
USER_AGENT = "humus"
REST_BASE = os.environ.get("GITHUB_API_URL", "https://api.github.com").rstrip("/")
GRAPHQL_URL = f"{REST_BASE}/graphql"
_MAX_COMMENT_PAGES = 20

_SYSTEM_CA_BUNDLES = (
    "/etc/ssl/certs/ca-certificates.crt",
    "/etc/pki/tls/certs/ca-bundle.crt",
    "/etc/pki/ca-trust/extracted/pem/tls-ca-bundle.crt",
    "/etc/ssl/ca-bundle.pem",
    "/etc/ssl/cert.pem",
    "/usr/local/etc/openssl/cert.pem",
    "/etc/pki/tls/cacert.pem",
)


@functools.lru_cache(maxsize=1)
def _https_context() -> ssl.SSLContext:
    """A context that can verify TLS against the host's trust store.

    uv-managed pythons compile in a cafile path that usually does not exist on
    the host, so otherwise-fine HTTPS fails with CERTIFICATE_VERIFY_FAILED.
    Honor SSL_CERT_FILE, then pick the first existing cafile from the OpenSSL
    defaults and known distro locations; fall back to the default context.
    """
    default = ssl.get_default_verify_paths()
    candidates = (
        os.environ.get("SSL_CERT_FILE"),
        default.cafile,
        default.openssl_cafile,
        *_SYSTEM_CA_BUNDLES,
    )
    for cafile in candidates:
        if cafile and os.path.isfile(cafile):
            return ssl.create_default_context(cafile=cafile)
    env_capath = os.environ.get("SSL_CERT_DIR")
    if env_capath and os.path.isdir(env_capath):
        return ssl.create_default_context(capath=env_capath)
    return ssl.create_default_context()


class ToolError(Exception):
    """A user-facing failure with a fixed message."""


@dataclass(frozen=True)
class Revision:
    """One observed force-push of a PR branch, with its GitHub version number."""

    before_commit_id: str
    commit_id: str
    is_current: bool
    version: int


@dataclass(frozen=True)
class Comment:
    id: int
    body: str


def short_commit_id(commit_id: str) -> str:
    return commit_id[:SHORT_COMMIT_LENGTH]


def _run(cmd: Sequence[str]) -> str:
    try:
        completed = subprocess.run(list(cmd), capture_output=True, text=True)
    except FileNotFoundError:
        return ""
    return completed.stdout.strip() if completed.returncode == 0 else ""


def github_token() -> str | None:
    for name in ("GITHUB_TOKEN", "GH_TOKEN"):
        if token := os.environ.get(name):
            return token
    return _run(("gh", "auth", "token")) or None


_REPO_RE = re.compile(
    r"(?:github\.com[:/])(?P<owner>[A-Za-z0-9_.-]+)/(?P<repo>[A-Za-z0-9_.-]+?)(?:\.git)?$"
)


def parse_github_repo(remote: str) -> tuple[str, str] | None:
    """Extract owner and repo from a github.com remote URL, SSH or HTTPS."""
    match = _REPO_RE.search(remote)
    if match is None:
        return None
    return match.group("owner"), match.group("repo").removesuffix(".git")


def detect_repo() -> tuple[str, str]:
    parsed = parse_github_repo(_run(("git", "remote", "get-url", "origin")))
    if parsed is None:
        raise ToolError(
            "Could not determine the GitHub repo from `git remote get-url origin`; "
            "pass --repo OWNER/NAME"
        )
    return parsed


def _request(url: str, *, method: str, token: str, payload: Any = None) -> Any:
    headers = {
        "Accept": "application/vnd.github+json",
        "Authorization": f"Bearer {token}",
        "User-Agent": USER_AGENT,
        "X-GitHub-Api-Version": "2022-11-28",
    }
    body = json.dumps(payload).encode() if payload is not None else None
    request = urllib.request.Request(url, data=body, headers=headers, method=method)
    try:
        with urllib.request.urlopen(request, context=_https_context()) as response:
            raw = response.read()
    except urllib.error.HTTPError as error:
        raise ToolError(
            f"GitHub API error {error.code} for {method} {url}: "
            f"{error.read().decode(errors='replace')}"
        ) from error
    return json.loads(raw) if raw else None


def graphql(query: str, variables: dict[str, Any], *, token: str) -> dict[str, Any]:
    response = _request(
        GRAPHQL_URL,
        method="POST",
        token=token,
        payload={"query": query, "variables": variables},
    )
    if not isinstance(response, dict):
        raise ToolError("GitHub GraphQL returned an invalid response")
    if errors := response.get("errors"):
        raise ToolError(
            "GitHub GraphQL query failed: "
            + "; ".join(str(error.get("message")) for error in errors)
        )
    return response


def rest(token: str, method: str, path: str, body: Any = None) -> Any:
    return _request(f"{REST_BASE}{path}", method=method, token=token, payload=body)


def _pull_request(data: dict[str, Any]) -> dict[str, Any]:
    pr = data["data"]["repository"]["pullRequest"]
    if pr is None:
        raise ToolError("Could not find a matching pull request")
    return pr


PR_BY_BRANCH_QUERY = """
query PullRequestForBranch($owner: String!, $repo: String!, $branch: String!) {
  repository(owner: $owner, name: $repo) {
    nameWithOwner
    pullRequests(headRefName: $branch, states: OPEN, first: 10) {
      nodes { number url baseRefOid headRefOid }
    }
  }
}
"""


def find_pr_by_branch(
    *, token: str, owner: str, repo: str, branch: str
) -> dict[str, Any]:
    data = graphql(
        PR_BY_BRANCH_QUERY,
        {"owner": owner, "repo": repo, "branch": branch},
        token=token,
    )
    request = data["data"]["repository"]["pullRequests"]
    nodes = request["nodes"] or []
    if not nodes:
        raise ToolError(
            f"No open pull request has head branch {branch!r}; pass --pr NUMBER"
        )
    if len(nodes) > 1:
        print(
            f"Multiple open pull requests have head branch {branch!r}; "
            f"using #{nodes[0]['number']}. Pass --pr to choose one.",
            file=sys.stderr,
        )
    return nodes[0]


PR_BY_NUMBER_QUERY = """
query PullRequest($owner: String!, $repo: String!, $number: Int!) {
  repository(owner: $owner, name: $repo) {
    nameWithOwner
    pullRequest(number: $number) { number url baseRefOid headRefOid state }
  }
}
"""


def find_pr_by_number(
    *, token: str, owner: str, repo: str, number: int
) -> dict[str, Any]:
    data = graphql(
        PR_BY_NUMBER_QUERY,
        {"owner": owner, "repo": repo, "number": number},
        token=token,
    )
    pr = data["data"]["repository"]["pullRequest"]
    if pr is None:
        raise ToolError(f"No pull request #{number} found in {owner}/{repo}")
    return pr


def _revisions_from_timeline(timeline: dict[str, Any] | None) -> list[Revision]:
    nodes = timeline.get("nodes") or ()
    filtered_count = timeline.get("filteredCount") or 0
    revisions: list[Revision] = []
    for index, event in enumerate(nodes):
        if event is None:
            continue
        before = (event.get("beforeCommit") or {}).get("oid")
        after = (event.get("afterCommit") or {}).get("oid")
        if not before or not after:
            continue
        # In a window shorter than the full history, the oldest visible event is
        # version (filteredCount - len(nodes) + 2); n+1 for every step forward.
        version = filtered_count - len(nodes) + index + 2
        revisions.append(
            Revision(
                before_commit_id=before,
                commit_id=after,
                is_current=index == len(nodes) - 1,
                version=version,
            )
        )
    return revisions


def include_pushed_revision(
    revisions: list[Revision], branch_target: str | None
) -> list[Revision]:
    """Append the branch's current commit when GitHub's timeline lags a push.

    GitHub records HEAD_REF_FORCE_PUSHED_EVENT events asynchronously, so the
    head commit reported for the branch can be newer than the newest recorded
    event. Treat the gap as one more revision.
    """
    if (
        branch_target is None
        or not revisions
        or revisions[-1].commit_id == branch_target
    ):
        return revisions
    last = revisions[-1]
    prior = [replace(revision, is_current=False) for revision in revisions]
    return [
        *prior,
        Revision(
            before_commit_id=last.commit_id,
            commit_id=branch_target,
            is_current=True,
            version=last.version + 1,
        ),
    ]


def _markdown_link(*, text: str, url: str) -> str:
    return f"[`{text}`]({url})"


def revision_history_body(
    *, revisions: list[Revision], repo_full_name: str, base_ref_oid: str | None = None
) -> str:
    """Render the managed comment, newest version first."""
    revisions = revisions[-VERSION_LIMIT:]
    repo_url = f"https://github.com/{repo_full_name}"

    def _changes_from_base(commit_id: str) -> str:
        """Link a version to the whole PR diff, not the head commit alone."""
        return (
            _markdown_link(
                text=(f"{short_commit_id(base_ref_oid)}..{short_commit_id(commit_id)}"),
                url=f"{repo_url}/compare/{base_ref_oid}..{commit_id}",
            )
            if base_ref_oid is not None
            else _markdown_link(
                text=short_commit_id(commit_id),
                url=f"{repo_url}/commit/{commit_id}",
            )
        )

    rows = [
        MARKER,
        "## Revision history",
        "",
        "| Version | Changes from previous version | Changes from branch base |",
        "| --- | --- | --- |",
    ]
    for revision in reversed(revisions):
        version = f"{revision.version}{' (current)' if revision.is_current else ''}"
        changes_from_previous = _markdown_link(
            text=(
                f"{short_commit_id(revision.before_commit_id)}.."
                f"{short_commit_id(revision.commit_id)}"
            ),
            url=f"{repo_url}/compare/{revision.before_commit_id}..{revision.commit_id}",
        )
        changes_from_base = _changes_from_base(revision.commit_id)
        rows.append(f"| {version} | {changes_from_previous} | {changes_from_base} |")
    if revisions[0].version == 2 and len(revisions) < VERSION_LIMIT:
        initial_commit = revisions[0].before_commit_id
        rows.append(f"| 1 | Initial version | {_changes_from_base(initial_commit)} |")
    history_note = (
        f"Showing the {VERSION_LIMIT} most recent available versions. "
        if revisions[0].version > 2 or len(revisions) == VERSION_LIMIT
        else ""
    )
    rows.extend(
        (
            "",
            f"<sub>{history_note}Maintained by humus from this "
            "pull request's force-push history.</sub>",
        )
    )
    return "\n".join(rows)


def load_history(
    *, token: str, owner: str, repo: str, pr_number: int
) -> tuple[tuple[Comment, ...], list[Revision]]:
    """Fetch a PR's comments (looking for the managed one) and force pushes."""
    query = f"""
    query PullRequestHistory($owner: String!, $repo: String!, $number: Int!, $after: String) {{
      repository(owner: $owner, name: $repo) {{
        pullRequest(number: $number) {{
          comments(first: 100, after: $after) {{
            nodes {{ databaseId body }}
            pageInfo {{ endCursor hasNextPage }}
          }}
          timelineItems(last: {VERSION_LIMIT}, itemTypes: [HEAD_REF_FORCE_PUSHED_EVENT]) {{
            filteredCount
            nodes {{
              ... on HeadRefForcePushedEvent {{
                beforeCommit {{ oid }}
                afterCommit {{ oid }}
              }}
            }}
          }}
        }}
      }}
    }}
    """
    comments: list[Comment] = []
    timeline: dict[str, Any] | None = None
    cursor = None
    for _ in range(_MAX_COMMENT_PAGES):
        data = graphql(
            query,
            {"owner": owner, "repo": repo, "number": pr_number, "after": cursor},
            token=token,
        )
        pr = _pull_request(data)
        page = pr["comments"]
        comments.extend(
            Comment(id=node["databaseId"], body=node["body"])
            for node in (page["nodes"] or ())
            if node is not None
        )
        timeline = pr["timelineItems"]
        page_info = page["pageInfo"]
        if not page_info["hasNextPage"]:
            break
        cursor = page_info["endCursor"]
    return tuple(comments), _revisions_from_timeline(timeline)


def sync_comment(
    *,
    token: str,
    owner: str,
    repo: str,
    pr_number: int,
    comments: tuple[Comment, ...],
    body: str,
) -> str:
    """Create or replace the managed comment; report what happened."""
    managed: Comment | None = None
    for comment in comments:
        if MARKER in comment.body:
            managed = comment
    if managed is not None and managed.body == body:
        return "unchanged"
    if managed is None:
        rest(
            token,
            "POST",
            f"/repos/{owner}/{repo}/issues/{pr_number}/comments",
            {"body": body},
        )
        return "created"
    rest(
        token,
        "PATCH",
        f"/repos/{owner}/{repo}/issues/comments/{managed.id}",
        {"body": body},
    )
    return "updated"


def _parse_repo(repo: str) -> tuple[str, str]:
    parts = repo.split("/")
    if len(parts) != 2 or not all(parts):
        raise ToolError(f"Expected --repo as OWNER/NAME, got {repo!r}")
    return parts[0], parts[1]


def _require_token() -> str:
    token = github_token()
    if token is None:
        raise ToolError(
            "No GitHub token found. Set GITHUB_TOKEN or GH_TOKEN, or run `gh auth login`."
        )
    return token


def annotate(
    *,
    token: str,
    owner: str,
    repo: str,
    pr: int | None,
    branch: str | None,
    dry_run: bool,
) -> None:
    """Render the revision-history comment for a branch's PR and sync it.

    Exactly one of pr or branch is given; a branch is resolved to its open PR
    first.
    """
    if pr is not None:
        pull = find_pr_by_number(token=token, owner=owner, repo=repo, number=pr)
    else:
        pull = find_pr_by_branch(token=token, owner=owner, repo=repo, branch=branch)
    pr_number = pull["number"]
    comments, revisions = load_history(
        token=token, owner=owner, repo=repo, pr_number=pr_number
    )
    revisions = include_pushed_revision(revisions, pull.get("headRefOid"))
    if not revisions:
        print(f"No force-push history recorded for pull request #{pr_number} yet.")
        return
    body = revision_history_body(
        revisions=revisions,
        repo_full_name=pull.get("nameWithOwner", f"{owner}/{repo}"),
        base_ref_oid=pull.get("baseRefOid"),
    )
    if dry_run:
        print(body)
        return
    status = sync_comment(
        token=token,
        owner=owner,
        repo=repo,
        pr_number=pr_number,
        comments=comments,
        body=body,
    )
    print(f"Revision history comment for pull request #{pr_number}: {status}.")


def _working_copy_bookmark() -> str:
    """Name of the bookmark on @, or "" when @ has none."""
    output = _run(("jj", "bookmark", "list", "-r", "@", "-T", "name"))
    return output.splitlines()[0].strip() if output else ""


def jj_push(
    args: Sequence[str], *, repo: str | None = None, dry_run: bool = False
) -> int:
    """Push with jj, then keep the comment current for the pushed branch.

    Runs `jj git push` first; the branch comes from the bookmark on the
    working-copy commit, so bare `humus` stays inert on its own.
    """
    try:
        push = subprocess.run(["jj", "git", "push", *args])
    except FileNotFoundError:
        raise ToolError("Could not find `jj` on PATH")
    if push.returncode != 0:
        return push.returncode
    branch = _working_copy_bookmark()
    if not branch:
        print(
            "humus: the working-copy commit has no bookmark; skipping the comment",
            file=sys.stderr,
        )
        return 0
    owner, repo_name = _parse_repo(repo) if repo else detect_repo()
    annotate(
        token=_require_token(),
        owner=owner,
        repo=repo_name,
        pr=None,
        branch=branch,
        dry_run=dry_run,
    )
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="humus",
        description=(
            "Maintain a PR comment listing the force-pushed versions of a patch. "
            "The repo comes from the origin remote; the branch comes from --branch, "
            "or the PR from --pr."
        ),
    )
    parser.add_argument(
        "--pr", type=int, help="Pull request number (when branch lookup is not enough)"
    )
    parser.add_argument(
        "--repo", help="GitHub repo as OWNER/NAME, overriding the origin remote"
    )
    parser.add_argument("--branch", help="Head branch owning the PR to annotate")
    parser.add_argument(
        "-n",
        "--dry-run",
        action="store_true",
        help="Print the rendered comment body to stdout without writing it",
    )
    parser.add_argument(
        "command",
        nargs=argparse.REMAINDER,
        metavar="COMMAND",
        help=(
            "`jj-push` runs `jj git push` (with anything after it passed "
            "through) and then updates the comment for the pushed branch"
        ),
    )
    args = parser.parse_args(list(argv) if argv is not None else None)

    try:
        if args.command and args.command[0] == "jj-push":
            return jj_push(args.command[1:], repo=args.repo, dry_run=args.dry_run)
        if args.command:
            parser.error(f"unrecognized arguments: {' '.join(args.command)}")
        if args.pr is None and args.branch is None:
            parser.print_help()
            return 0
        owner, repo = _parse_repo(args.repo) if args.repo else detect_repo()
        annotate(
            token=_require_token(),
            owner=owner,
            repo=repo,
            pr=args.pr,
            branch=args.branch,
            dry_run=args.dry_run,
        )
    except ToolError as error:
        print(f"humus: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
