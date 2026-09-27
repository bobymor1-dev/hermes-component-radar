"""DE-02 regression suite: the CLI must honour GH_TOKEN / GITHUB_TOKEN.

The defect: ``main()`` hard-coded ``token = None``, so a configured credential
was silently ignored while the docstring claimed otherwise. These tests use
obviously fake credentials and additionally assert that no fake value ever
reaches the emitted output.
"""

import contextlib
import io
import json
import os
import sys
import unittest
from pathlib import Path
from unittest import mock


SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))

import radar_github  # noqa: E402
from radar_github import (  # noqa: E402
    TOKEN_ENV_VARS,
    RadarGitHubError,
    main,
    read_token,
)


FAKE_GH = "ghp_FAKE00000000000000000000000000000000"
FAKE_GITHUB = "github_pat_FAKE1111111111111111111111111111"


def setUpModule():
    """Fail loudly if a test forgets to stub the transport.

    Without this guard a missing mock would perform a real unauthenticated API
    call -- slow, rate-limited, and non-deterministic. The guard is installed on
    the module attribute, which is exactly what ``dedupe``/``fetch_repo``
    resolve at call time.
    """

    def _blocked(url, headers=None, timeout=30.0):
        raise RadarGitHubError(f"network access attempted during tests: {url}")

    original = radar_github._http_get
    radar_github._http_get = _blocked

    def _restore():
        radar_github._http_get = original

    unittest.addModuleCleanup(_restore)


class ReadTokenTests(unittest.TestCase):
    def test_no_credential(self):
        self.assertEqual(read_token({}), (None, None))

    def test_gh_token(self):
        token, source = read_token({"GH_TOKEN": FAKE_GH})
        self.assertEqual(token, FAKE_GH)
        self.assertEqual(source, "GH_TOKEN")

    def test_github_token(self):
        token, source = read_token({"GITHUB_TOKEN": FAKE_GITHUB})
        self.assertEqual(token, FAKE_GITHUB)
        self.assertEqual(source, "GITHUB_TOKEN")

    def test_gh_token_takes_precedence(self):
        token, source = read_token({"GH_TOKEN": FAKE_GH, "GITHUB_TOKEN": FAKE_GITHUB})
        self.assertEqual(token, FAKE_GH)
        self.assertEqual(source, "GH_TOKEN")

    def test_blank_and_whitespace_count_as_absent(self):
        self.assertEqual(read_token({"GH_TOKEN": "", "GITHUB_TOKEN": "   "}), (None, None))
        self.assertEqual(read_token({"GH_TOKEN": "  ", "GITHUB_TOKEN": FAKE_GITHUB}),
                         (FAKE_GITHUB, "GITHUB_TOKEN"))

    def test_value_is_stripped(self):
        self.assertEqual(read_token({"GH_TOKEN": f"  {FAKE_GH}\n"}), (FAKE_GH, "GH_TOKEN"))

    def test_documented_variables_are_the_ones_read(self):
        self.assertEqual(TOKEN_ENV_VARS, ("GH_TOKEN", "GITHUB_TOKEN"))

    def test_reads_process_environment_by_default(self):
        with mock.patch.dict(os.environ, {"GH_TOKEN": FAKE_GH}, clear=False):
            self.assertEqual(read_token(), (FAKE_GH, "GH_TOKEN"))


class CliTokenTests(unittest.TestCase):
    """The CLI path must actually use the credential (the DE-02 defect)."""

    def _run(self, argv, env):
        stdout, stderr = io.StringIO(), io.StringIO()
        with mock.patch.dict(os.environ, env, clear=False):
            for name in TOKEN_ENV_VARS:
                if name not in env:
                    os.environ.pop(name, None)
            with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
                code = main(argv)
        return code, stdout.getvalue(), stderr.getvalue()

    def _fake_fetch(self, seen):
        def fetch(owner, repo, token=None, timeout=30.0, _get=None):
            seen.append(token)
            return {"slug": f"{owner}/{repo}", "html_url": "x"}

        return fetch

    def test_inspect_uses_env_token(self):
        seen = []
        with mock.patch.object(radar_github, "fetch_repo", self._fake_fetch(seen)):
            code, out, _ = self._run(["inspect", "o", "r"], {"GH_TOKEN": FAKE_GH})
        self.assertEqual(code, 0)
        self.assertEqual(seen, [FAKE_GH], "the credential must reach the request builder")
        self.assertIn("GH_TOKEN", out, "the source name is reported")
        self.assertNotIn(FAKE_GH, out, "the value must never be printed")

    def test_inspect_without_token_still_works(self):
        seen = []
        with mock.patch.object(radar_github, "fetch_repo", self._fake_fetch(seen)):
            code, out, _ = self._run(["inspect", "o", "r"], {})
        self.assertEqual(code, 0)
        self.assertEqual(seen, [None])
        self.assertIn('"token_source": null', out)

    def test_inspect_falls_back_to_github_token(self):
        seen = []
        with mock.patch.object(radar_github, "fetch_repo", self._fake_fetch(seen)):
            code, out, _ = self._run(["inspect", "o", "r"], {"GITHUB_TOKEN": FAKE_GITHUB})
        self.assertEqual(code, 0)
        self.assertEqual(seen, [FAKE_GITHUB])
        self.assertNotIn(FAKE_GITHUB, out)

    def test_dedupe_uses_env_token_and_reports_blocked(self):
        seen = []

        def fake_get(url, headers=None, timeout=30.0):
            seen.append((headers or {}).get("Authorization"))
            return json.dumps({"total_count": 0, "items": []}).encode("utf-8")

        with mock.patch.object(radar_github, "_http_get", fake_get):
            code, out, _ = self._run(
                ["dedupe", "nobody", "nothing-here", "--inventory", "/nope/INVENTARIO.md"],
                {"GH_TOKEN": FAKE_GH},
            )
        # The missing inventory raises a clean error, not a traceback.
        self.assertEqual(code, 1)
        self.assertNotIn(FAKE_GH, out)

    def test_dedupe_output_never_contains_the_token(self):
        fixture = Path(__file__).resolve().parent / "fixtures" / "INVENTARIO.md"

        def fake_get(url, headers=None, timeout=30.0):
            return json.dumps({"total_count": 0, "items": []}).encode("utf-8")

        with mock.patch.object(radar_github, "_http_get", fake_get):
            code, out, _ = self._run(
                ["dedupe", "dondai44423", "donsetch", "--inventory", str(fixture)],
                {"GH_TOKEN": FAKE_GH},
            )
        self.assertEqual(code, 3, "a blocked verdict is a refusal, not a success")
        payload = json.loads(out)
        self.assertEqual(payload["verdict"], "review_required")
        self.assertTrue(payload["blocked"])
        self.assertEqual(payload["token_source"], "GH_TOKEN")
        self.assertNotIn(FAKE_GH, out)

    def test_error_path_does_not_leak_the_token(self):
        def boom(url, headers=None, timeout=30.0):
            raise RadarGitHubError(f"HTTP 401 from {url}")

        fixture = Path(__file__).resolve().parent / "fixtures" / "INVENTARIO.md"
        with mock.patch.object(radar_github, "_http_get", boom):
            code, out, err = self._run(
                ["dedupe", "x", "y", "--inventory", str(fixture)], {"GH_TOKEN": FAKE_GH}
            )
        self.assertEqual(code, 3, "an indeterminate verdict is also a refusal")
        self.assertNotIn(FAKE_GH, out + err)
        self.assertEqual(json.loads(out)["verdict"], "indeterminate")

    def test_no_token_value_in_any_cli_output(self):
        for command in (["inspect", "o", "r"],):
            for env in ({"GH_TOKEN": FAKE_GH}, {"GITHUB_TOKEN": FAKE_GITHUB}):
                with self.subTest(command=command, env=list(env)):
                    seen = []
                    with mock.patch.object(radar_github, "fetch_repo", self._fake_fetch(seen)):
                        _, out, err = self._run(command, env)
                    self.assertNotIn(FAKE_GH, out + err)
                    self.assertNotIn(FAKE_GITHUB, out + err)


if __name__ == "__main__":
    unittest.main()
