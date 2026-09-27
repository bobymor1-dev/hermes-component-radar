"""Offline suite for the minimal GitHub write integration.

No network, no ``gh``, no token: a stateful fake stands in for the API and
records every call, so the tests can assert *both* the happy path and the
safety properties:

* nothing is sent without a credential; the Issue routes send nothing at all in
  dry-run mode, and ``inventory-append`` makes exactly one read-only GET (it
  cannot report the target ``sha`` without reading the file);
* the duplicate gate is bound to the candidate, refuses to create a card when it
  is blocked *or absent*, and makes no request;
* every write is verified by reading the remote object back, and a write that
  landed but could not be confirmed is reported as ``applied`` + unverified
  rather than as a failure (a retry must not duplicate it);
* a concurrent edit is never clobbered -- a sha conflict retries by re-applying
  the transform to freshly fetched content;
* the token value never appears in any returned structure.
"""

import base64
import contextlib
import hashlib
import io
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))

import radar_github  # noqa: E402
import radar_github_write as w  # noqa: E402
from radar_github_write import (  # noqa: E402
    AuthError,
    ConflictError,
    DuplicateBlocked,
    NotFoundError,
    RadarWriteError,
    TokenMissing,
    VerificationError,
    _build_parser,
    append_inventory_row,
    close_issue,
    comment_issue,
    create_issue,
    get_inventory,
    redact,
    update_inventory,
)


FAKE_TOKEN = "github_pat_FAKE9999999999999999999999999999"
PATH = "INVENTARIO.md"
BASE_TEXT = "# Inventario\n\n| Candidato | Ficha |\n|---|---|\n| ekko-studio | [Issue #1](x) |\n"

#: The candidate the write tests register, and a dedupe result bound to it.
SLUG = "o/r"
CLEAR = {
    "slug": SLUG,
    "verdict": "not_found",
    "inventory": [],
    "inventory_name": [],
    "inventory_ambiguous": False,
    "inventory_checked": True,
    "issues_checked": True,
    "checks_complete": True,
    "review_required": False,
    "issues": {"total_count": 0, "items": []},
}


def clear_gate(**overrides):
    """A dedupe result that authorises creating :data:`SLUG`."""
    result = dict(CLEAR)
    result.update(overrides)
    return result


class FakeGitHub:
    """Stateful stand-in for the GitHub REST API. Records every call."""

    def __init__(self, files=None, issues=None):
        self.files = dict(files if files is not None else {PATH: BASE_TEXT})
        self.issues = dict(issues or {})
        self.comments: dict[int, list[dict]] = {}
        self.calls: list[dict] = []
        self.put_count = 0
        self.next_number = 100
        self.before_put = None
        self.after_put = None
        self.corrupt_after_put = None
        self.drop_comments_on_get = False
        self.reject_close = False
        self.fail = None  # callable(method, url) -> Exception | None

    # -- helpers ---------------------------------------------------------- #
    def _sha(self, path: str) -> str:
        return hashlib.sha1(self.files[path].encode("utf-8")).hexdigest()

    def _json(self, obj, status=200):
        return status, json.dumps(obj).encode("utf-8")

    def put_calls(self):
        return [c for c in self.calls if c["method"] == "PUT"]

    # -- the injected transport ------------------------------------------- #
    def send(self, method, url, headers=None, payload=None, timeout=30.0, token=None):
        self.calls.append({"method": method, "url": url, "payload": payload})
        if self.fail:
            exc = self.fail(method, url)
            if exc:
                raise exc
        if "/comments" in url:
            return self._comments(method, url, payload)
        if "/contents/" in url:
            return self._contents(method, url, payload)
        return self._issue(method, url, payload)

    def _contents(self, method, url, payload):
        path = url.split("/contents/", 1)[1].split("?", 1)[0]
        if method == "GET":
            if path not in self.files:
                raise NotFoundError(f"HTTP 404 from {url}: Not Found")
            return self._json(
                {
                    "path": path,
                    "sha": self._sha(path),
                    "encoding": "base64",
                    "content": base64.b64encode(self.files[path].encode("utf-8")).decode(),
                }
            )
        if method == "PUT":
            self.put_count += 1
            if self.before_put:
                self.before_put(self)
            if payload.get("sha") != self._sha(path):
                raise ConflictError(f"HTTP 409 from {url}: sha does not match")
            self.files[path] = base64.b64decode(payload["content"]).decode("utf-8")
            if self.corrupt_after_put is not None:
                self.files[path] = self.corrupt_after_put
            if self.after_put:
                self.after_put(self)
            return self._json(
                {
                    "content": {"sha": self._sha(path)},
                    "commit": {"sha": "commit" + self._sha(path)[:8]},
                }
            )
        raise AssertionError(f"unexpected method {method}")

    def _issue(self, method, url, payload):
        tail = url.split("/issues", 1)[1].strip("/")
        if method == "POST":
            number = self.next_number
            self.next_number += 1
            self.issues[number] = {
                "number": number,
                "title": payload.get("title"),
                "body": payload.get("body"),
                "state": "open",
                "labels": payload.get("labels", []),
                "html_url": f"https://github.com/o/r/issues/{number}",
            }
            return self._json(dict(self.issues[number]), 201)
        number = int(tail)
        if number not in self.issues:
            raise NotFoundError(f"HTTP 404 from {url}")
        if method == "GET":
            return self._json(dict(self.issues[number]))
        if method == "PATCH":
            issue = self.issues[number]
            if not self.reject_close:
                issue["state"] = payload.get("state", issue["state"])
                issue["state_reason"] = payload.get("state_reason")
            return self._json(dict(issue))
        raise AssertionError(f"unexpected method {method}")

    def _comments(self, method, url, payload):
        base = url.split("?", 1)[0]
        parts = base.split("/issues/", 1)[1].split("/")
        if parts[0] == "comments" and len(parts) > 1:
            # The exact comment endpoint: /issues/comments/{id}
            if self.drop_comments_on_get:
                raise NotFoundError(f"HTTP 404 from {url}")
            cid = int(parts[1])
            for items in self.comments.values():
                for item in items:
                    if item["id"] == cid:
                        return self._json(dict(item))
            raise NotFoundError(f"HTTP 404 from {url}")
        number = int(parts[0])
        if method == "POST":
            cid = self.next_number
            self.next_number += 1
            comment = {
                "id": cid,
                "body": payload.get("body"),
                "html_url": f"https://github.com/o/r/issues/{number}#issuecomment-{cid}",
            }
            self.comments.setdefault(number, []).append(comment)
            return self._json(dict(comment), 201)
        if method == "GET":
            return self._json([] if self.drop_comments_on_get else list(self.comments.get(number, [])))
        raise AssertionError(f"unexpected method {method}")


def _ok(url, headers=None, timeout=30.0):
    return json.dumps({"total_count": 0, "items": []}).encode("utf-8")


def setUpModule():
    """Fail loudly if a test forgets to stub the write transport.

    The real ``_http_send`` would PUT/PATCH against api.github.com; with a
    credential in the environment that is a live write. The module attribute is
    exactly what :func:`_request_json` resolves, so replacing it here makes an
    unmocked call an immediate error instead of a remote mutation.
    """

    def _blocked(*args, **kwargs):
        raise AssertionError("network write attempted during tests")

    original = w._http_send
    w._http_send = _blocked

    def _restore():
        w._http_send = original

    unittest.addModuleCleanup(_restore)


class CreateIssueTests(unittest.TestCase):
    def test_creates_and_verifies(self):
        store = FakeGitHub()
        result = create_issue(
            "o", "r", "Titulo", "Cuerpo", FAKE_TOKEN,
            dedupe_result=clear_gate(), candidate=SLUG, _send=store.send,
        )
        self.assertTrue(result["applied"])
        self.assertTrue(result["verified"])
        self.assertEqual(result["number"], 100)
        self.assertEqual(store.issues[100]["title"], "Titulo")
        methods = [c["method"] for c in store.calls]
        self.assertEqual(methods, ["POST", "GET"], "creation must be read back")
        self.assertEqual(result["gate"]["candidate"], SLUG)
        self.assertTrue(result["gate"]["bound"])

    def test_dry_run_sends_nothing(self):
        store = FakeGitHub()
        result = create_issue(
            "o", "r", "T", "B", FAKE_TOKEN,
            dedupe_result=clear_gate(), candidate=SLUG, dry_run=True, _send=store.send,
        )
        self.assertTrue(result["dry_run"])
        self.assertFalse(result["applied"])
        self.assertEqual(store.calls, [])
        self.assertEqual(result["payload"]["title"], "T")

    def test_missing_token_makes_no_request(self):
        store = FakeGitHub()
        with self.assertRaises(TokenMissing):
            create_issue(
                "o", "r", "T", "B", None,
                dedupe_result=clear_gate(), candidate=SLUG, _send=store.send,
            )
        self.assertEqual(store.calls, [])

    def test_auth_error_propagates(self):
        store = FakeGitHub()
        store.fail = lambda m, u: AuthError("HTTP 401 from x: Requires authentication")
        with self.assertRaises(AuthError):
            create_issue(
                "o", "r", "T", "B", FAKE_TOKEN,
                dedupe_result=clear_gate(), candidate=SLUG, _send=store.send,
            )

    def test_network_error_propagates(self):
        store = FakeGitHub()
        store.fail = lambda m, u: RadarWriteError("network error: simulated outage")
        with self.assertRaises(RadarWriteError):
            create_issue(
                "o", "r", "T", "B", FAKE_TOKEN,
                dedupe_result=clear_gate(), candidate=SLUG, _send=store.send,
            )

    def test_duplicate_gate_blocks_and_makes_no_request(self):
        store = FakeGitHub()
        for verdict in ("duplicate", "review_required", "indeterminate"):
            with self.subTest(verdict=verdict):
                store.calls.clear()
                with self.assertRaises(DuplicateBlocked) as ctx:
                    create_issue(
                        "o", "r", "T", "B", FAKE_TOKEN,
                        dedupe_result=clear_gate(verdict=verdict),
                        candidate=SLUG, _send=store.send,
                    )
                self.assertIn(verdict, str(ctx.exception))
                self.assertEqual(store.calls, [], "a blocked create must not call the API")

    def test_duplicate_gate_allows_a_clean_verdict(self):
        store = FakeGitHub()
        result = create_issue(
            "o", "r", "T", "B", FAKE_TOKEN,
            dedupe_result=clear_gate(), candidate=SLUG, _send=store.send,
        )
        self.assertTrue(result["verified"])

    def test_gate_is_bound_to_the_candidate_not_the_repository(self):
        """A verdict about some other repository must not authorise this card."""
        store = FakeGitHub()
        with self.assertRaises(DuplicateBlocked) as ctx:
            create_issue(
                "o", "r", "T", "B", FAKE_TOKEN,
                dedupe_result=clear_gate(slug="someone/else"), candidate=SLUG,
                _send=store.send,
            )
        self.assertIn("someone/else", str(ctx.exception))
        self.assertEqual(store.calls, [], "must refuse before any request")

    def test_an_unbound_dedupe_result_is_refused(self):
        store = FakeGitHub()
        with self.assertRaises(DuplicateBlocked):
            create_issue(
                "o", "r", "T", "B", FAKE_TOKEN,
                dedupe_result={"verdict": "not_found"}, candidate=None, _send=store.send,
            )
        self.assertEqual(store.calls, [])

    def test_no_gate_at_all_is_refused(self):
        store = FakeGitHub()
        with self.assertRaises(DuplicateBlocked) as ctx:
            create_issue("o", "r", "T", "B", FAKE_TOKEN, _send=store.send)
        self.assertIn("no dedupe result", str(ctx.exception))
        self.assertEqual(store.calls, [], "the gate is mandatory, not optional")

    def test_override_reason_is_the_only_bypass_and_is_recorded(self):
        store = FakeGitHub()
        result = create_issue(
            "o", "r", "T", "B", FAKE_TOKEN,
            dedupe_result=clear_gate(verdict="duplicate"),
            candidate=SLUG,
            gate_override_reason="revision humana: ficha legitima distinta",
            _send=store.send,
        )
        self.assertTrue(result["applied"])
        self.assertEqual(
            result["gate"]["override_reason"],
            "revision humana: ficha legitima distinta",
        )

    def test_landed_but_unconfirmed_is_not_a_failure(self):
        """The write took effect, so reporting a failure would invite a retry."""
        store = FakeGitHub()

        def tamper(method, url):
            if method == "GET":
                store.issues[100]["title"] = "algo distinto"
            return None

        store.fail = tamper
        result = create_issue(
            "o", "r", "T", "B", FAKE_TOKEN,
            dedupe_result=clear_gate(), candidate=SLUG, _send=store.send,
        )
        self.assertTrue(result["applied"])
        self.assertFalse(result["verified"])
        self.assertIn("differ", result["verification"])

    def test_read_back_failure_after_a_landed_create_is_unverified(self):
        store = FakeGitHub()

        def die_on_get(method, url):
            if method == "GET":
                return RadarWriteError("network error: read-back dropped")
            return None

        store.fail = die_on_get
        result = create_issue(
            "o", "r", "T", "B", FAKE_TOKEN,
            dedupe_result=clear_gate(), candidate=SLUG, _send=store.send,
        )
        self.assertTrue(result["applied"])
        self.assertFalse(result["verified"])
        self.assertEqual(result["number"], 100)

    def test_result_never_contains_the_token(self):
        store = FakeGitHub()
        result = create_issue(
            "o", "r", "T", "B", FAKE_TOKEN,
            dedupe_result=clear_gate(), candidate=SLUG, _send=store.send,
        )
        self.assertNotIn(FAKE_TOKEN, json.dumps(result))


class CommentIssueTests(unittest.TestCase):
    def test_comment_is_verified(self):
        store = FakeGitHub(issues={7: {"number": 7, "title": "t", "body": "b", "state": "open"}})
        result = comment_issue("o", "r", 7, "nota", FAKE_TOKEN, _send=store.send)
        self.assertTrue(result["verified"])
        self.assertEqual(store.comments[7][0]["body"], "nota")
        self.assertEqual([c["method"] for c in store.calls], ["POST", "GET"])

    def test_missing_comment_on_read_back_is_unverified_not_a_failure(self):
        """The POST landed, so the comment exists; the read-back just cannot see it."""
        store = FakeGitHub(issues={7: {"number": 7, "state": "open"}})
        store.drop_comments_on_get = True
        result = comment_issue("o", "r", 7, "nota", FAKE_TOKEN, _send=store.send)
        self.assertTrue(result["applied"])
        self.assertFalse(result["verified"])
        self.assertIsNotNone(result["comment_id"])
        self.assertEqual(len(store.comments[7]), 1, "the comment really was created")

    def test_comment_dry_run(self):
        store = FakeGitHub(issues={7: {"number": 7, "state": "open"}})
        result = comment_issue("o", "r", 7, "nota", FAKE_TOKEN, dry_run=True, _send=store.send)
        self.assertTrue(result["dry_run"])
        self.assertEqual(store.calls, [])

    def test_missing_token(self):
        store = FakeGitHub(issues={7: {"number": 7, "state": "open"}})
        with self.assertRaises(TokenMissing):
            comment_issue("o", "r", 7, "nota", None, _send=store.send)


class CloseIssueTests(unittest.TestCase):
    def _store(self):
        return FakeGitHub(issues={8: {"number": 8, "title": "dup", "body": "b", "state": "open"}})

    def test_close_with_traceability_comment(self):
        store = self._store()
        result = close_issue(
            "o", "r", 8, FAKE_TOKEN,
            comment="Duplicado exacto de #7; se conserva por historial.",
            state_reason="not_planned", _send=store.send,
        )
        self.assertTrue(result["verified"])
        self.assertEqual(result["state"], "closed")
        self.assertEqual(result["state_reason"], "not_planned")
        self.assertEqual(len(store.comments[8]), 1)
        self.assertIn("Duplicado exacto", store.comments[8][0]["body"])
        self.assertEqual(
            [c["method"] for c in store.calls],
            ["POST", "GET", "PATCH", "GET"],
            "comment is created, verified, then the close is verified",
        )

    def test_close_without_comment(self):
        store = self._store()
        result = close_issue("o", "r", 8, FAKE_TOKEN, _send=store.send)
        self.assertIsNone(result["comment_id"])
        self.assertEqual([c["method"] for c in store.calls], ["PATCH", "GET"])

    def test_still_open_remotely_is_unverified_not_a_failure(self):
        store = self._store()
        store.reject_close = True
        result = close_issue("o", "r", 8, FAKE_TOKEN, _send=store.send)
        self.assertTrue(result["applied"])
        self.assertFalse(result["verified"])
        self.assertIn("not closed", result["verification"])

    def test_invalid_state_reason_rejected_before_any_request(self):
        store = self._store()
        with self.assertRaises(RadarWriteError):
            close_issue("o", "r", 8, FAKE_TOKEN, state_reason="deleted", _send=store.send)
        self.assertEqual(store.calls, [])

    def test_close_dry_run_plans_without_writing(self):
        store = self._store()
        result = close_issue("o", "r", 8, FAKE_TOKEN, comment="x", dry_run=True, _send=store.send)
        self.assertTrue(result["dry_run"])
        self.assertEqual(store.calls, [])
        self.assertIn("comment_plan", result)


class InventoryReadTests(unittest.TestCase):
    def test_get_returns_text_and_sha(self):
        store = FakeGitHub()
        result = get_inventory("o", "r", PATH, FAKE_TOKEN, _send=store.send)
        self.assertEqual(result["text"], BASE_TEXT)
        self.assertEqual(result["sha"], store._sha(PATH))

    def test_missing_file_is_not_found(self):
        store = FakeGitHub(files={})
        with self.assertRaises(NotFoundError):
            get_inventory("o", "r", PATH, FAKE_TOKEN, _send=store.send)

    def test_non_base64_encoding_is_refused(self):
        class TooLarge(FakeGitHub):
            """Mimics the contents API's "file too large" response."""

            def _contents(self, method, url, payload):
                return self._json(
                    {"path": PATH, "sha": "x", "encoding": "none", "content": ""}
                )

        with self.assertRaises(RadarWriteError):
            get_inventory("o", "r", PATH, FAKE_TOKEN, _send=TooLarge().send)


class UpdateInventoryTests(unittest.TestCase):
    def _append(self, row):
        return lambda text: append_inventory_row(text, row)

    def test_update_applies_and_verifies(self):
        store = FakeGitHub()
        result = update_inventory(
            "o", "r", self._append("| DonSeTch | [Issue #7](x) |"),
            FAKE_TOKEN, "radar: registrar DonSeTch", _send=store.send,
        )
        self.assertTrue(result["applied"])
        self.assertTrue(result["verified"])
        self.assertIn("| DonSeTch | [Issue #7](x) |", store.files[PATH])
        self.assertNotEqual(result["sha"], result["previous_sha"])
        self.assertEqual([c["method"] for c in store.calls], ["GET", "PUT", "GET"])

    def test_no_change_is_a_no_op(self):
        store = FakeGitHub()
        result = update_inventory(
            "o", "r", lambda text: text, FAKE_TOKEN, "no-op", _send=store.send
        )
        self.assertFalse(result["applied"])
        self.assertFalse(result["changed"])
        self.assertEqual(store.put_calls(), [])

    def test_conflict_then_success_preserves_the_other_writer(self):
        """The core anti-clobber property."""
        store = FakeGitHub()

        def concurrent_writer(inner):
            if inner.put_count == 1:
                # Another writer lands between our read and our PUT.
                inner.files[PATH] = BASE_TEXT + "| concurrent | [Issue #99](x) |\n"

        store.before_put = concurrent_writer
        result = update_inventory(
            "o", "r", self._append("| nuevo | [Issue #7](x) |"),
            FAKE_TOKEN, "radar: registrar", _send=store.send,
        )
        self.assertTrue(result["applied"])
        self.assertEqual(len(result["attempts"]), 1, "one conflict then a success")
        self.assertEqual(result["attempts"][0]["result"], "conflict")
        final = store.files[PATH]
        self.assertIn("| concurrent |", final, "the other writer's row must survive")
        self.assertIn("| nuevo |", final, "our row must be applied to fresh content")
        self.assertEqual(store.put_count, 2)

    def test_conflict_exhaustion_writes_nothing(self):
        store = FakeGitHub()

        def always_conflict(inner):
            inner.files[PATH] = inner.files[PATH] + "| moving target |\n"

        store.before_put = always_conflict
        with self.assertRaises(ConflictError):
            update_inventory(
                "o", "r", self._append("| nuevo |"), FAKE_TOKEN, "msg",
                max_attempts=3, _send=store.send,
            )
        self.assertNotIn("| nuevo |", store.files[PATH])
        self.assertEqual(store.put_count, 3)

    def test_require_unchanged_base_aborts_instead_of_overwriting(self):
        store = FakeGitHub()
        store.files[PATH] = BASE_TEXT + "| someone else |\n"
        with self.assertRaises(ConflictError):
            update_inventory(
                "o", "r", lambda text: "FULL REPLACEMENT", FAKE_TOKEN, "msg",
                base_text=BASE_TEXT, require_unchanged_base=True, _send=store.send,
            )
        self.assertEqual(store.put_calls(), [], "must abort before writing")
        self.assertNotIn("FULL REPLACEMENT", store.files[PATH])

    def test_read_back_mismatch_is_unverified_not_a_lost_write(self):
        """Another writer landing after our PUT must not look like a failure.

        The file *was* updated, so raising here would push the operator to retry
        and append the row a second time.
        """
        store = FakeGitHub()

        def another_writer(inner):
            # Someone appends after our PUT: our row is there, the text differs.
            inner.files[PATH] = inner.files[PATH] + "| otro | [Issue #99](x) |\n"

        store.after_put = another_writer
        result = update_inventory(
            "o", "r", self._append("| nuevo |"), FAKE_TOKEN, "msg", _send=store.send
        )
        self.assertTrue(result["applied"])
        self.assertFalse(result["verified"])
        self.assertIn("another writer", result["verification"])
        self.assertIn("| nuevo |", store.files[PATH], "our write did land")

    def test_read_back_get_failure_is_unverified(self):
        store = FakeGitHub()

        original_contents = store._contents

        calls = {"n": 0}

        def counting_contents(method, url, payload):
            if method == "GET":
                calls["n"] += 1
                if calls["n"] >= 2:
                    raise RadarWriteError("network error: read-back dropped")
            return original_contents(method, url, payload)

        store._contents = counting_contents
        result = update_inventory(
            "o", "r", self._append("| nuevo |"), FAKE_TOKEN, "msg", _send=store.send
        )
        self.assertTrue(result["applied"])
        self.assertFalse(result["verified"])
        self.assertIn("read-back failed", result["verification"])

    def test_missing_token_makes_no_request(self):
        store = FakeGitHub()
        with self.assertRaises(TokenMissing):
            update_inventory("o", "r", self._append("| x |"), None, "msg", _send=store.send)
        self.assertEqual(store.calls, [])

    def test_dry_run_plans_without_writing(self):
        store = FakeGitHub()
        result = update_inventory(
            "o", "r", self._append("| x |"), FAKE_TOKEN, "msg", dry_run=True, _send=store.send
        )
        self.assertTrue(result["dry_run"])
        self.assertTrue(result["changed"])
        self.assertEqual(store.put_calls(), [])
        self.assertNotIn("| x |", store.files[PATH])

    def test_invalid_max_attempts(self):
        store = FakeGitHub()
        with self.assertRaises(RadarWriteError):
            update_inventory(
                "o", "r", self._append("| x |"), FAKE_TOKEN, "msg",
                max_attempts=0, _send=store.send,
            )


class AppendRowTests(unittest.TestCase):
    def test_appends_after_the_last_table_row(self):
        text = "# T\n\n| a | b |\n|---|---|\n| 1 | 2 |\n\ntexto final\n"
        out = append_inventory_row(text, "| nuevo | [Issue #7](x) |")
        lines = out.splitlines()
        self.assertEqual(lines[4], "| 1 | 2 |")
        self.assertEqual(lines[5], "| nuevo | [Issue #7](x) |")
        self.assertTrue(out.endswith("\n"))

    def test_anchor_places_the_row_after_the_anchored_table(self):
        text = "# T\n\n| a |\n|---|\n| 1 |\n\n## Otra\n\n| c |\n|---|\n| 2 |\n"
        out = append_inventory_row(text, "nuevo", anchor="## Otra")
        self.assertEqual(out.splitlines()[11], "| nuevo |")

    def test_row_is_wrapped_when_not_a_table_row(self):
        out = append_inventory_row("| a |\n|---|\n| 1 |\n", "solo texto")
        self.assertIn("| solo texto |", out)

    def test_no_table_appends_at_end(self):
        out = append_inventory_row("solo texto\n", "| x |")
        self.assertIn("| x |", out)

    def test_an_unknown_anchor_is_an_error_not_a_silent_last_table(self):
        """A typo in the anchor must not drop the row into an unrelated table."""
        text = (
            "# T\n\n| a |\n|---|\n| 1 |\n\n"
            "## Candidatos registrados\n\n| c |\n|---|\n| 2 |\n"
        )
        with self.assertRaises(RadarWriteError) as ctx:
            append_inventory_row(text, "| nuevo |", anchor="## Tabla inexistente")
        self.assertIn("not found", str(ctx.exception))

    def test_an_anchor_that_is_a_prefix_still_anchors(self):
        """The anchor is a substring match, so a longer heading still works."""
        text = (
            "# T\n\n| a |\n|---|\n| 1 |\n\n"
            "## Candidatos registrados\n\n| c |\n|---|\n| 2 |\n"
        )
        out = append_inventory_row(text, "| nuevo |", anchor="## Candidatos")
        self.assertEqual(out.splitlines()[-1], "| nuevo |")

    def test_an_identical_row_is_not_appended_twice(self):
        """A retry after an inconclusive verification must be a no-op."""
        text = "| a |\n|---|\n| nuevo | [Issue #7](x) |\n"
        out = append_inventory_row(text, "| nuevo | [Issue #7](x) |")
        self.assertEqual(out, text)


class RedactionTests(unittest.TestCase):
    def test_redact_removes_the_value(self):
        self.assertNotIn(FAKE_TOKEN, redact(f"token={FAKE_TOKEN} failed", FAKE_TOKEN))

    def test_redact_without_token_is_a_no_op(self):
        self.assertEqual(redact("texto", None), "texto")

    def test_no_token_in_any_returned_structure(self):
        store = FakeGitHub(issues={8: {"number": 8, "state": "open"}})
        outputs = [
            create_issue(
                "o", "r", "T", "B", FAKE_TOKEN,
                dedupe_result=clear_gate(), candidate=SLUG, _send=store.send,
            ),
            comment_issue("o", "r", 8, "c", FAKE_TOKEN, _send=store.send),
            close_issue("o", "r", 8, FAKE_TOKEN, _send=store.send),
            get_inventory("o", "r", PATH, FAKE_TOKEN, _send=store.send),
            update_inventory(
                "o", "r", lambda t: append_inventory_row(t, "| x |"),
                FAKE_TOKEN, "m", _send=store.send,
            ),
        ]
        blob = json.dumps(outputs)
        self.assertNotIn(FAKE_TOKEN, blob)
        self.assertNotIn("Bearer", blob)


class CliParserTests(unittest.TestCase):
    def test_apply_accepted_before_the_subcommand(self):
        args = _build_parser().parse_args(["--apply", "issue-close", "8"])
        self.assertTrue(args.apply)

    def test_apply_accepted_after_the_subcommand(self):
        args = _build_parser().parse_args(["issue-close", "8", "--apply"])
        self.assertTrue(args.apply)

    def test_apply_defaults_to_dry_run(self):
        args = _build_parser().parse_args(["issue-close", "8"])
        self.assertFalse(args.apply)

    def test_registry_repo_is_not_clobbered_by_the_subparser(self):
        args = _build_parser().parse_args(["--registry-repo", "a/b", "inventory-get"])
        self.assertEqual(args.registry_repo, "a/b")

    def test_registry_repo_and_timeout_defaults(self):
        args = _build_parser().parse_args(
            ["inventory-append", "--row", "| x |", "--candidate", "o/r"]
        )
        self.assertEqual(args.registry_repo, w.DEFAULT_REGISTRY)
        self.assertEqual(args.timeout, 30.0)
        self.assertEqual(args.max_attempts, w.DEFAULT_MAX_ATTEMPTS)

    def test_registering_commands_require_a_candidate(self):
        """Without --candidate the gate cannot be bound, so both must refuse."""
        for argv in (
            ["issue-create", "T", "--body-file", "b.md"],
            ["inventory-append", "--row", "| x |"],
        ):
            with self.subTest(argv=argv):
                with self.assertRaises(SystemExit):
                    with contextlib.redirect_stderr(io.StringIO()):
                        _build_parser().parse_args(argv)

    def test_the_bypass_flag_is_gone(self):
        args = _build_parser().parse_args(
            ["issue-create", "T", "--body-file", "b.md", "--candidate", "o/r",
             "--override-gate", "motivo escrito"]
        )
        self.assertFalse(hasattr(args, "skip_dedupe_check"))
        self.assertEqual(args.override_gate, "motivo escrito")


class CliBehaviourTests(unittest.TestCase):
    def _no_network(self, calls):
        def guard(*args, **kwargs):
            calls.append(args)
            raise AssertionError("the CLI must not send anything here")

        return guard

    def test_dry_run_never_touches_the_network(self):
        calls = []
        with mock.patch.object(w, "_http_send", self._no_network(calls)):
            stdout, stderr = io.StringIO(), io.StringIO()
            with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
                code = w.main(["issue-close", "8", "--state-reason", "not_planned"])
        self.assertEqual(code, 0)
        self.assertEqual(calls, [])
        payload = json.loads(stdout.getvalue())
        self.assertTrue(payload["dry_run"])
        self.assertFalse(payload["applied"])
        self.assertEqual(payload["payload"]["state_reason"], "not_planned")

    def test_apply_without_a_credential_fails_cleanly_and_sends_nothing(self):
        calls = []
        env = {k: v for k, v in os.environ.items() if k not in ("GH_TOKEN", "GITHUB_TOKEN")}
        stdout, stderr = io.StringIO(), io.StringIO()
        with mock.patch.dict(os.environ, env, clear=True):
            with mock.patch.object(w, "_http_send", self._no_network(calls)):
                with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
                    code = w.main(["issue-close", "8", "--apply"])
        self.assertEqual(code, 1)
        self.assertEqual(calls, [])
        self.assertIn("GH_TOKEN", stderr.getvalue())

    def test_token_source_is_reported_but_never_the_value(self):
        fake = "ghp_FAKE00000000000000000000000000000000"
        seen = []

        def fake_send(method, url, headers=None, payload=None, timeout=30.0, token=None):
            seen.append((headers or {}).get("Authorization"))
            return 200, json.dumps(
                {
                    "path": "INVENTARIO.md",
                    "sha": "abc123",
                    "encoding": "base64",
                    "content": base64.b64encode(b"# inventario\n").decode(),
                }
            ).encode("utf-8")

        stdout, stderr = io.StringIO(), io.StringIO()
        with mock.patch.dict(os.environ, {"GH_TOKEN": fake}, clear=False):
            with mock.patch.object(w, "_http_send", fake_send):
                with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
                    code = w.main(["inventory-get", "--path", "INVENTARIO.md"])
        self.assertEqual(code, 0)
        self.assertEqual(
            seen, [f"Bearer {fake}"], "the CLI must authenticate with the env token (DE-02)"
        )
        out = stdout.getvalue()
        self.assertIn("GH_TOKEN", out)
        self.assertNotIn(fake, out + stderr.getvalue())

    def test_invalid_state_reason_is_rejected_by_the_parser(self):
        with self.assertRaises(SystemExit):
            with contextlib.redirect_stderr(io.StringIO()):
                w.main(["issue-close", "8", "--state-reason", "deleted"])


class CliGateTests(unittest.TestCase):
    """The gate must be about the *candidate*, and must run on both write paths.

    The inventory below mimics the real registry, whose Ficha column links to
    the registry's own Issues. A gate that deduped the registry instead of the
    candidate therefore answered "duplicate" for everything.
    """

    REGISTRY = "bobymor1-dev/hermes-component-radar"

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.inv = root / "INVENTARIO.md"
        self.inv.write_text(
            "# Inventario\n\n"
            "| Candidato | Ficha |\n|---|---|\n"
            f"| DonSeTch | [Issue #7](https://github.com/{self.REGISTRY}/issues/7) | PROBAR. |\n",
            encoding="utf-8",
        )
        self.body = root / "body.md"
        self.body.write_text("cuerpo de la ficha", encoding="utf-8")

    def _run(self, argv, search_payload, env=None):
        net = []

        def fake_get(url, headers=None, timeout=30.0):
            net.append(("search", url))
            return json.dumps(search_payload).encode("utf-8")

        def fake_send(method, url, headers=None, payload=None, timeout=30.0, token=None):
            net.append((method, url))
            if method == "GET" and "/contents/" in url:
                text = self.inv.read_text(encoding="utf-8")
                return 200, json.dumps(
                    {
                        "path": "INVENTARIO.md",
                        "sha": hashlib.sha1(text.encode()).hexdigest(),
                        "encoding": "base64",
                        "content": base64.b64encode(text.encode()).decode(),
                    }
                ).encode("utf-8")
            raise AssertionError(f"unexpected write attempted: {method} {url}")

        environment = {"GH_TOKEN": FAKE_TOKEN, "RADAR_INVENTORY": str(self.inv)}
        if env:
            environment.update(env)

        stdout, stderr = io.StringIO(), io.StringIO()
        with mock.patch.object(radar_github, "_http_get", fake_get):
            with mock.patch.object(w, "_http_send", fake_send):
                with mock.patch.dict(os.environ, environment, clear=False):
                    with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
                        code = w.main(["--registry-repo", self.REGISTRY] + argv)
        return code, stdout.getvalue(), stderr.getvalue(), net

    @staticmethod
    def _verdict(stderr):
        return json.loads(stderr.strip().splitlines()[0])

    def test_the_gate_judges_the_candidate_not_the_registry(self):
        """The regression: dedupe used to run on the registry repository."""
        code, out, err, net = self._run(
            [
                "issue-create", "T", "--body-file", str(self.body),
                "--candidate", "nuevo/candidato",
            ],
            {"total_count": 0, "items": []},
        )
        reported = self._verdict(err)
        self.assertEqual(reported["candidate"], "nuevo/candidato")
        self.assertEqual(
            reported["dedupe_verdict"],
            "not_found",
            "the registry's own rows must not make every candidate a duplicate",
        )
        self.assertFalse(reported["blocked"])
        self.assertEqual(code, 0, "a clear gate proceeds (dry run here)")

    def test_a_registered_candidate_is_refused_before_any_write(self):
        code, out, err, net = self._run(
            [
                "issue-create", "T", "--body-file", str(self.body),
                "--candidate", "dondai44423/donsetch", "--apply",
            ],
            {"total_count": 0, "items": []},
        )
        self.assertEqual(code, 3, "a blocking verdict must be an exit refusal")
        self.assertEqual([n[0] for n in net], ["search"], "no write may be attempted")
        self.assertTrue(self._verdict(err)["blocked"])

    def test_inventory_append_passes_the_same_gate(self):
        code, out, err, net = self._run(
            [
                "inventory-append", "--row", "| DonSeTch | [Issue #8](x) |",
                "--candidate", "dondai44423/donsetch", "--apply",
            ],
            {"total_count": 0, "items": []},
        )
        self.assertEqual(code, 3, "the row is a registration too: it must be gated")
        self.assertEqual([n[0] for n in net], ["search"], "must refuse before writing")

    def test_inventory_append_is_idempotent_when_the_row_is_present(self):
        self.inv.write_text(
            "# Inventario\n\n| Candidato | Ficha |\n|---|---|\n"
            "| nuevo/candidato | [Issue #9](x) | PROBAR. |\n",
            encoding="utf-8",
        )
        code, out, err, net = self._run(
            [
                "inventory-append", "--row", "| nuevo/candidato | [Issue #9](x) | PROBAR. |",
                "--candidate", "nuevo/candidato",
                "--override-gate", "prueba de idempotencia",
                "--apply",
            ],
            {"total_count": 0, "items": []},
        )
        payload = json.loads(out)
        self.assertEqual(code, 0)
        self.assertFalse(payload["applied"], "a retry must not append a second row")
        self.assertFalse(payload["changed"])
        self.assertNotIn("PUT", [n[0] for n in net])
        self.assertEqual(
            payload["gate"]["override_reason"], "prueba de idempotencia",
            "the bypass must be recorded",
        )

    def test_the_search_query_names_the_candidate_and_scopes_the_registry(self):
        net = self._run(
            ["inventory-append", "--row", "| x |", "--candidate", "nuevo/candidato"],
            {"total_count": 0, "items": []},
        )[3]
        # The dry run only reads: the search plus the contents GET it needs to
        # report the target sha. No write of any kind.
        self.assertEqual([n[0] for n in net], ["search", "GET"])
        _, url = net[0]
        self.assertIn("nuevo/candidato", url, "the candidate must be searched for")
        self.assertIn(self.REGISTRY, url, "scoped to the registry's Issues")

    def test_inventory_get_reports_shape_only_by_default(self):
        code, out, err, net = self._run(["inventory-get"], {"total_count": 0, "items": []})
        payload = json.loads(out)
        self.assertEqual(code, 0)
        self.assertNotIn("text", payload, "the file must not be echoed by default")
        self.assertIn("sha", payload)
        self.assertGreater(payload["chars"], 0)

    def test_inventory_get_full_opts_in(self):
        code, out, err, net = self._run(
            ["inventory-get", "--full"], {"total_count": 0, "items": []}
        )
        self.assertIn("DonSeTch", json.loads(out)["text"])

    def test_a_malformed_credential_is_refused_without_leaking_it(self):
        bad = "ghp_" + "A" * 20 + "\nINJECTED"
        code, out, err, net = self._run(
            ["inventory-get"], {"total_count": 0, "items": []}, env={"GH_TOKEN": bad}
        )
        self.assertEqual(code, 1)
        self.assertEqual(net, [], "a refused credential must not reach the network")
        self.assertNotIn(bad, out + err)
        self.assertNotIn("INJECTED", out + err)


if __name__ == "__main__":
    unittest.main()
