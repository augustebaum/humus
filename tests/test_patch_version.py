import contextlib
import io
import sys
import unittest
import unittest.mock as mock
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from humus import cli
from humus.cli import (
    Revision,
    include_pushed_revision,
    main,
    parse_github_repo,
    revision_history_body,
    short_commit_id,
    _https_context,
    _revisions_from_timeline,
    _SYSTEM_CA_BUNDLES,
)


class ParseRepoTest(unittest.TestCase):
    def test_https(self) -> None:
        self.assertEqual(
            parse_github_repo("https://github.com/octocat/Hello-World.git"),
            ("octocat", "Hello-World"),
        )

    def test_ssh(self) -> None:
        self.assertEqual(
            parse_github_repo("git@github.com:octocat/Hello-World.git"),
            ("octocat", "Hello-World"),
        )

    def test_no_slash_repo(self) -> None:
        self.assertIsNone(parse_github_repo("https://github.com/octocat"))

    def test_other_host(self) -> None:
        self.assertIsNone(parse_github_repo("git@gitlab.com:octocat/Hello-World.git"))


class RevisionsFromTimelineTest(unittest.TestCase):
    def test_full_window_reconstruction(self) -> None:
        timeline = {
            "filteredCount": 2,
            "nodes": [
                {
                    "beforeCommit": {"oid": "a" * 40},
                    "afterCommit": {"oid": "b" * 40},
                },
                {
                    "beforeCommit": {"oid": "b" * 40},
                    "afterCommit": {"oid": "c" * 40},
                },
            ],
        }
        revisions = _revisions_from_timeline(timeline)
        self.assertEqual(len(revisions), 2)
        self.assertEqual(revisions[0].version, 2)
        self.assertFalse(revisions[0].is_current)
        self.assertEqual(revisions[1].version, 3)
        self.assertTrue(revisions[1].is_current)

    def test_window_shorter_than_history(self) -> None:
        timeline = {
            "filteredCount": 42,
            "nodes": [
                {"beforeCommit": {"oid": "a" * 40}, "afterCommit": {"oid": "b" * 40}},
                {"beforeCommit": {"oid": "b" * 40}, "afterCommit": {"oid": "c" * 40}},
            ],
        }
        revisions = _revisions_from_timeline(timeline)
        self.assertEqual(revisions[0].version, 42)
        self.assertEqual(revisions[1].version, 43)

    def test_events_without_commit_are_skipped(self) -> None:
        timeline = {
            "filteredCount": 2,
            "nodes": [
                {"beforeCommit": None, "afterCommit": {"oid": "b" * 40}},
                {"beforeCommit": {"oid": "b" * 40}, "afterCommit": {"oid": "c" * 40}},
            ],
        }
        revisions = _revisions_from_timeline(timeline)
        self.assertEqual(len(revisions), 1)
        self.assertEqual(revisions[0].version, 3)


class IncludePushedRevisionTest(unittest.TestCase):
    revisions = (
        Revision(before_commit_id="a" * 40, commit_id="b" * 40, is_current=False, version=2),
        Revision(before_commit_id="b" * 40, commit_id="c" * 40, is_current=True, version=3),
    )

    def test_no_append_when_timeline_current(self) -> None:
        out = include_pushed_revision(self.revisions, "c" * 40)
        self.assertEqual(out, self.revisions)

    def test_append_when_branch_ahead(self) -> None:
        out = include_pushed_revision(self.revisions, "d" * 40)
        self.assertEqual(len(out), 3)
        self.assertFalse(out[0].is_current)
        self.assertFalse(out[1].is_current)
        self.assertEqual(out[2].before_commit_id, "c" * 40)
        self.assertEqual(out[2].commit_id, "d" * 40)
        self.assertEqual(out[2].version, 4)
        self.assertTrue(out[2].is_current)

    def test_no_append_without_branch_target(self) -> None:
        out = include_pushed_revision(self.revisions, None)
        self.assertEqual(out, self.revisions)

    def test_no_append_without_revisions(self) -> None:
        self.assertEqual(include_pushed_revision((), "d" * 40), ())


class RenderBodyTest(unittest.TestCase):
    def test_table(self) -> None:
        revisions = (
            Revision(before_commit_id="a" * 40, commit_id="b" * 40, is_current=False, version=2),
            Revision(before_commit_id="b" * 40, commit_id="c" * 40, is_current=True, version=3),
        )
        body = revision_history_body(revisions=revisions, repo_full_name="octocat/Hello-World")
        self.assertIn("<!-- jj-stack-revision-history -->", body)
        self.assertIn("| Version | Changes from previous version | Submitted commit |", body)
        self.assertIn("| 3 (current) |", body)
        self.assertIn(
            f"[`{'b' * 8}..{'c' * 8}`](https://github.com/octocat/Hello-World/"
            f"compare/{'b' * 40}..{'c' * 40})",
            body,
        )
        self.assertIn(f"`{short_commit_id('c' * 40)}`", body)
        self.assertIn("| 1 | Initial version |", body)
        self.assertIn("Maintained by humus", body)

    def test_version_cap_note(self) -> None:
        revisions = (
            Revision(before_commit_id="a" * 40, commit_id="b" * 40, is_current=False, version=42),
            Revision(before_commit_id="b" * 40, commit_id="c" * 40, is_current=True, version=43),
        )
        body = revision_history_body(revisions=revisions, repo_full_name="octocat/Hello-World")
        self.assertIn("Showing the 20 most recent available versions.", body)

    def test_current_commit_linked(self) -> None:
        revisions = (
            Revision(before_commit_id="a" * 40, commit_id="b" * 40, is_current=True, version=2),
        )
        body = revision_history_body(revisions=revisions, repo_full_name="octocat/Hello-World")
        self.assertIn(f"compare/{'a' * 40}..{'b' * 40}", body)
        self.assertIn(f"commit/{'b' * 40}", body)


class ShortCommitIdTest(unittest.TestCase):
    def test_prefix(self) -> None:
        self.assertEqual(short_commit_id("0123456789abcdef"), "01234567")


class HttpsContextTest(unittest.TestCase):
    def setUp(self) -> None:
        _https_context.cache_clear()
        self.addCleanup(_https_context.cache_clear)

    def test_env_ssl_cert_file_wins(self) -> None:
        default = mock.Mock(cafile="/missing.pem", openssl_cafile="/missing.pem")
        with mock.patch.object(cli.ssl, "get_default_verify_paths", return_value=default), \
                mock.patch.object(cli.os.path, "isfile", return_value=True), \
                mock.patch.object(cli.ssl, "create_default_context") as create, \
                mock.patch.dict(cli.os.environ, {"SSL_CERT_FILE": "/env.pem"}, clear=True):
            _https_context()
        create.assert_called_once_with(cafile="/env.pem")

    def test_default_cafile_is_used_when_present(self) -> None:
        default = mock.Mock(cafile="/distro/ca.pem", openssl_cafile="/distro/ca.pem")
        with mock.patch.object(cli.ssl, "get_default_verify_paths", return_value=default), \
                mock.patch.object(cli.os.path, "isfile", side_effect=lambda p: p == "/distro/ca.pem"), \
                mock.patch.object(cli.ssl, "create_default_context") as create, \
                mock.patch.dict(cli.os.environ, {}, clear=True):
            _https_context()
        create.assert_called_once_with(cafile="/distro/ca.pem")

    def test_system_bundle_is_found_when_default_is_missing(self) -> None:
        default = mock.Mock(cafile=None, openssl_cafile="/etc/ssl/cert.pem")
        bundle = _SYSTEM_CA_BUNDLES[0]
        with mock.patch.object(cli.ssl, "get_default_verify_paths", return_value=default), \
                mock.patch.object(cli.os.path, "isfile", side_effect=lambda p: p == bundle), \
                mock.patch.object(cli.ssl, "create_default_context") as create, \
                mock.patch.dict(cli.os.environ, {}, clear=True):
            _https_context()
        create.assert_called_once_with(cafile=bundle)

    def test_no_bundle_falls_back_to_default_context(self) -> None:
        default = mock.Mock(cafile=None, openssl_cafile=None)
        with mock.patch.object(cli.ssl, "get_default_verify_paths", return_value=default), \
                mock.patch.object(cli.os.path, "isfile", return_value=False), \
                mock.patch.object(cli.ssl, "create_default_context") as create, \
                mock.patch.dict(cli.os.environ, {}, clear=True):
            _https_context()
        create.assert_called_once_with()


class BareInvocationTest(unittest.TestCase):
    def test_no_arguments_is_inert(self) -> None:
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            self.assertEqual(main([]), 0)
        self.assertIn("usage: humus", buffer.getvalue())

    def test_short_and_long_help_are_synonyms(self) -> None:
        outputs = []
        for flag in ("-h", "--help"):
            buffer = io.StringIO()
            with contextlib.redirect_stdout(buffer):
                with self.assertRaises(SystemExit) as caught:
                    main([flag])
            self.assertEqual(caught.exception.code, 0)
            outputs.append(buffer.getvalue())
        self.assertEqual(outputs[0], outputs[1])


class JJPushTest(unittest.TestCase):
    def test_pushes_then_annotates_bookmarked_branch(self) -> None:
        process = mock.Mock(returncode=0)
        with mock.patch.object(cli.subprocess, "run", return_value=process), \
                mock.patch.object(cli, "_working_copy_bookmark", return_value="feature/x"), \
                mock.patch.object(cli, "detect_repo", return_value=("octocat", "Hello-World")), \
                mock.patch.object(cli, "_require_token", return_value="token"), \
                mock.patch.object(cli, "annotate") as annotate:
            rc = main(["jj-push"])
        self.assertEqual(rc, 0)
        self.assertEqual(
            annotate.call_args.kwargs,
            {
                "token": "token",
                "owner": "octocat",
                "repo": "Hello-World",
                "pr": None,
                "branch": "feature/x",
                "dry_run": False,
            },
        )

    def test_forwards_args_to_jj_push(self) -> None:
        process = mock.Mock(returncode=0)
        with mock.patch.object(cli.subprocess, "run", return_value=process) as run, \
                mock.patch.object(cli, "_working_copy_bookmark", return_value="main"), \
                mock.patch.object(cli, "detect_repo", return_value=("o", "r")), \
                mock.patch.object(cli, "_require_token", return_value="t"), \
                mock.patch.object(cli, "annotate"):
            rc = main(["jj-push", "--to", "origin", "-r", "@"])
        self.assertEqual(rc, 0)
        run.assert_called_once_with(["jj", "git", "push", "--to", "origin", "-r", "@"])

    def test_repo_flag_overrides_origin_remote(self) -> None:
        process = mock.Mock(returncode=0)
        with mock.patch.object(cli.subprocess, "run", return_value=process), \
                mock.patch.object(cli, "_working_copy_bookmark", return_value="main"), \
                mock.patch.object(cli, "detect_repo") as detect, \
                mock.patch.object(cli, "_require_token", return_value="t"), \
                mock.patch.object(cli, "annotate") as annotate:
            rc = main(["--repo", "o/r", "jj-push"])
        self.assertEqual(rc, 0)
        detect.assert_not_called()
        self.assertEqual(annotate.call_args.kwargs["repo"], "r")

    def test_dry_run_flag_is_forwarded(self) -> None:
        process = mock.Mock(returncode=0)
        with mock.patch.object(cli.subprocess, "run", return_value=process), \
                mock.patch.object(cli, "_working_copy_bookmark", return_value="main"), \
                mock.patch.object(cli, "detect_repo", return_value=("o", "r")), \
                mock.patch.object(cli, "_require_token", return_value="t"), \
                mock.patch.object(cli, "annotate") as annotate:
            rc = main(["--dry-run", "jj-push"])
        self.assertEqual(rc, 0)
        self.assertTrue(annotate.call_args.kwargs["dry_run"])

    def test_no_bookmark_skips_comment(self) -> None:
        process = mock.Mock(returncode=0)
        stderr = io.StringIO()
        with mock.patch.object(cli.subprocess, "run", return_value=process), \
                mock.patch.object(cli, "_working_copy_bookmark", return_value=""), \
                mock.patch.object(cli, "annotate") as annotate, \
                contextlib.redirect_stderr(stderr):
            rc = main(["jj-push"])
        self.assertEqual(rc, 0)
        annotate.assert_not_called()
        self.assertIn("no bookmark", stderr.getvalue())

    def test_push_failure_exit_code_propagates(self) -> None:
        failed = mock.Mock(returncode=3)
        with mock.patch.object(cli.subprocess, "run", return_value=failed), \
                mock.patch.object(cli, "annotate") as annotate:
            rc = main(["jj-push"])
        self.assertEqual(rc, 3)
        annotate.assert_not_called()

    def test_missing_jj_is_a_user_error(self) -> None:
        stderr = io.StringIO()
        with mock.patch.object(cli.subprocess, "run", side_effect=FileNotFoundError), \
                contextlib.redirect_stderr(stderr):
            rc = main(["jj-push"])
        self.assertEqual(rc, 1)
        self.assertIn("jj", stderr.getvalue())

    def test_unknown_command_errors(self) -> None:
        stderr = io.StringIO()
        with mock.patch.object(cli.subprocess, "run") as run, \
                contextlib.redirect_stderr(stderr):
            with self.assertRaises(SystemExit) as caught:
                main(["bogus"])
        self.assertEqual(caught.exception.code, 2)
        run.assert_not_called()
        self.assertIn("unrecognized arguments", stderr.getvalue())


if __name__ == "__main__":
    unittest.main()
