#!/usr/bin/env python3
"""Minimal GitHub write integration for the radar-github skill.

Standard library only: no ``gh``, no pip packages, no extra binaries. Every
write follows the same discipline:

  1. **Duplicate gate, bound to the candidate** -- a candidate Issue is never
     created before a dedupe result *for that candidate* authorises it. The
     result is checked to have been computed for the same ``owner/repo`` being
     registered, so a verdict about some other repository can never authorise a
     create. When no result and no explicit override reason are supplied, the
     create is refused.
  2. **Version control** -- ``INVENTARIO.md`` is updated through the contents
     API using the blob ``sha``, so a concurrent edit produces a 409 instead of
     a silent overwrite.
  3. **Read-back verification** -- after every write the remote object is read
     again and compared. A write that demonstrably *landed* but cannot be
     confirmed is reported as ``applied: true, verified: false`` with a reason,
     never as a bare failure: the operation already took effect, and calling it
     a failure would push the operator to retry and duplicate it.

Safety contract
---------------
* The token is read from ``GH_TOKEN`` / ``GITHUB_TOKEN`` only. It is never
  accepted as a command-line argument (that would leak it into the process list
  and the shell history), never printed and never written to disk. Any string
  this module emits passes through :func:`redact`.
* Nothing is *written* unless ``--apply`` is given. In dry-run mode the Issue
  routes send no request at all; ``inventory-append`` performs one read-only GET
  (reported as ``reads_in_dry_run: true``) because the target blob ``sha`` and
  the ``changed`` flag cannot be known without reading the current file.
* :func:`update_inventory` re-applies a *transform* to freshly fetched content
  on every attempt, so a retry after a conflict keeps the other writer's edit
  instead of resurrecting a stale version.
* Exit codes: 0 done, 1 the operation failed, 3 the duplicate gate refused,
  4 the operation landed but could not be verified.
"""

from __future__ import annotations

import argparse
import base64
import json
import sys
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Callable

sys.path.insert(0, str(Path(__file__).resolve().parent))

from radar_github import (  # noqa: E402
    API_BASE,
    DEFAULT_REGISTRY,
    EXIT_BLOCKED,
    USER_AGENT,
    RadarGitHubError,
    dedupe,
    must_not_create,
    read_token,
    redact_text,
    resolve_inventory_path,
)

DEFAULT_INVENTORY_PATH = "INVENTARIO.md"
DEFAULT_MAX_ATTEMPTS = 3

#: The write landed remotely but could not be confirmed by a read-back.
EXIT_UNVERIFIED = 4


class RadarWriteError(RadarGitHubError):
    """A write operation could not be completed safely."""


class TokenMissing(RadarWriteError):
    """No credential is available for an operation that requires one."""


class AuthError(RadarWriteError):
    """The credential was rejected (HTTP 401/403)."""


class NotFoundError(RadarWriteError):
    """The target repository, Issue or file does not exist (HTTP 404)."""


class ConflictError(RadarWriteError):
    """Optimistic-concurrency conflict: someone else wrote first (HTTP 409/412/422)."""


class VerificationError(RadarWriteError):
    """The response to a write was unusable, so the operation is not trustworthy."""


class DuplicateBlocked(RadarWriteError):
    """The duplicate gate refused to create a new candidate card."""


def redact(text: str, token: str | None) -> str:
    """Remove a token value from any text this module emits."""
    return redact_text(text, token)


def _unverified(base: dict[str, Any], detail: str) -> dict[str, Any]:
    """Report a write that took effect but could not be confirmed.

    Deliberately not an exception: the change is already live remotely, so
    reporting it as a failure would invite a retry that duplicates it.
    """
    result = dict(base)
    result["applied"] = True
    result["verified"] = False
    result["verification"] = detail
    return result


def _error_for(code: int, body: bytes, url: str, token: str | None) -> RadarWriteError:
    message = ""
    try:
        payload = json.loads(body.decode("utf-8"))
        if isinstance(payload, dict):
            message = str(payload.get("message") or "")
    except (UnicodeDecodeError, json.JSONDecodeError):
        message = ""
    detail = f"HTTP {code} from {url}" + (f": {message}" if message else "")
    detail = redact(detail, token)
    if code in (401, 403):
        return AuthError(detail)
    if code == 404:
        return NotFoundError(detail)
    if code in (409, 412, 422):
        return ConflictError(detail)
    return RadarWriteError(detail)


def _http_send(
    method: str,
    url: str,
    headers: dict[str, str] | None = None,
    payload: dict[str, Any] | None = None,
    timeout: float = 30.0,
    token: str | None = None,
) -> tuple[int, bytes]:
    """Perform one GitHub API request. Returns ``(status, body_bytes)``."""
    data = None
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=data, method=method)
    for key, value in (headers or {}).items():
        req.add_header(key, value)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, resp.read()
    except urllib.error.HTTPError as exc:
        raise _error_for(exc.code, exc.read(), url, token) from exc
    except ValueError as exc:
        # urllib raises ValueError for a malformed header and quotes the
        # offending value in its message, which would print the credential.
        # The text is deliberately not forwarded.
        raise RadarWriteError(f"invalid request header for {url}") from exc
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise RadarWriteError(redact(f"network error from {url}: {exc}", token)) from exc


def _write_headers(token: str | None) -> dict[str, str]:
    headers = {
        "Accept": "application/vnd.github+json",
        "User-Agent": USER_AGENT,
        "X-GitHub-Api-Version": "2022-11-28",
    }
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return headers


def _require_token(token: str | None) -> str:
    if not token:
        raise TokenMissing(
            "no GitHub credential: set GH_TOKEN or GITHUB_TOKEN in the environment"
        )
    return token


def _request_json(
    method: str,
    url: str,
    token: str | None,
    payload: dict[str, Any] | None = None,
    timeout: float = 30.0,
    expect: tuple[int, ...] = (200, 201),
    _send: Callable | None = None,
) -> Any:
    _send = _send or _http_send
    status, body = _send(
        method, url, _write_headers(token), payload, timeout, token
    )
    if status not in expect:
        raise RadarWriteError(f"unexpected HTTP {status} from {url}")
    if not body:
        return {}
    try:
        return json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise VerificationError(f"non-JSON response from {url}") from exc


def _issues_url(owner: str, repo: str) -> str:
    return f"{API_BASE}/repos/{owner}/{repo}/issues"


def _comment_url(owner: str, repo: str, comment_id: int) -> str:
    return f"{API_BASE}/repos/{owner}/{repo}/issues/comments/{comment_id}"


def _contents_url(owner: str, repo: str, path: str) -> str:
    return f"{API_BASE}/repos/{owner}/{repo}/contents/{path}"


# --------------------------------------------------------------------------- #
# Issues
# --------------------------------------------------------------------------- #


def _gate_state(
    candidate: str | None,
    dedupe_result: dict[str, Any] | None,
    gate_override_reason: str | None,
) -> dict[str, Any]:
    """Check that a dedupe result authorises creating *this* candidate.

    Three things can authorise a create: a dedupe result computed for the same
    ``candidate`` whose verdict is not blocking, or an explicit non-empty
    override reason (which is recorded in the result so the bypass is visible).
    Anything else is refused.
    """
    gate: dict[str, Any] = {
        "candidate": candidate,
        "verdict": (dedupe_result or {}).get("verdict"),
        "bound": False,
        "override_reason": gate_override_reason,
    }

    if dedupe_result is None:
        if not gate_override_reason:
            raise DuplicateBlocked(
                "no dedupe result was supplied: refusing to create a candidate "
                "card without checking for duplicates. Run dedupe for the "
                "candidate first, or pass an explicit override reason."
            )
        return gate

    bound_slug = str(dedupe_result.get("slug") or "")
    if not candidate:
        raise DuplicateBlocked(
            "a dedupe result must be bound to the candidate it was computed for; "
            "pass candidate='owner/repo' so the binding can be checked"
        )
    if bound_slug != candidate:
        raise DuplicateBlocked(
            f"the dedupe result was computed for {bound_slug!r}, not for the "
            f"candidate {candidate!r}: refusing to create. A verdict about a "
            "different repository cannot authorise this card."
        )
    gate["bound"] = True

    if must_not_create(dedupe_result) and not gate_override_reason:
        verdict = dedupe_result.get("verdict", "unknown")
        issues = dedupe_result.get("issues") or {}
        raise DuplicateBlocked(
            f"duplicate gate: verdict={verdict}; "
            f"inventory={len(dedupe_result.get('inventory') or [])} exact, "
            f"{len(dedupe_result.get('inventory_name') or [])} by name, "
            f"issues={len(issues.get('matches') or [])} conclusive, "
            f"{len(issues.get('suggestive') or [])} by title. "
            "No card was created."
        )
    return gate


def create_issue(
    owner: str,
    repo: str,
    title: str,
    body: str,
    token: str | None,
    labels: list[str] | None = None,
    dedupe_result: dict[str, Any] | None = None,
    candidate: str | None = None,
    gate_override_reason: str | None = None,
    dry_run: bool = False,
    timeout: float = 30.0,
    _send: Callable | None = None,
) -> dict[str, Any]:
    """Create a candidate Issue, refusing unless the duplicate gate authorises it.

    ``dedupe_result`` is the output of :func:`radar_github.dedupe` and
    ``candidate`` is the ``owner/repo`` it was computed for. The two are bound
    together: the result is only accepted when its ``slug`` equals ``candidate``.
    Omitting both the result and ``gate_override_reason`` is refused.
    """
    gate = _gate_state(candidate, dedupe_result, gate_override_reason)

    payload: dict[str, Any] = {"title": title, "body": body}
    if labels:
        payload["labels"] = list(labels)

    if dry_run:
        return {
            "dry_run": True,
            "method": "POST",
            "url": _issues_url(owner, repo),
            "payload": payload,
            "gate": gate,
            "applied": False,
        }

    token = _require_token(token)
    created = _request_json(
        "POST", _issues_url(owner, repo), token, payload, timeout, (201,), _send
    )
    number = created.get("number")
    if not isinstance(number, int):
        raise VerificationError("created Issue response carried no number")

    base: dict[str, Any] = {
        "dry_run": False,
        "applied": True,
        "number": number,
        "html_url": created.get("html_url"),
        "state": created.get("state"),
        "gate": gate,
    }

    # Read-back verification. The POST already succeeded, so a problem here
    # means "landed but unconfirmed", not "nothing happened".
    try:
        remote = _request_json(
            "GET", f"{_issues_url(owner, repo)}/{number}", token, None, timeout, (200,), _send
        )
    except RadarWriteError as exc:
        return _unverified(base, f"create landed as Issue #{number} but the read-back failed: {exc}")
    if not isinstance(remote, dict):
        return _unverified(base, f"Issue #{number} read-back was not an object")
    if remote.get("title") != title:
        return _unverified(base, f"Issue #{number} title differs from what was sent")
    if (remote.get("body") or "") != body:
        return _unverified(base, f"Issue #{number} body differs from what was sent")

    base["html_url"] = remote.get("html_url")
    base["state"] = remote.get("state")
    base["verified"] = True
    return base


def comment_issue(
    owner: str,
    repo: str,
    number: int,
    body: str,
    token: str | None,
    dry_run: bool = False,
    timeout: float = 30.0,
    _send: Callable | None = None,
) -> dict[str, Any]:
    """Add a comment to an Issue and verify it exists remotely."""
    url = f"{_issues_url(owner, repo)}/{number}/comments"
    if dry_run:
        return {
            "dry_run": True,
            "method": "POST",
            "url": url,
            "payload": {"body": body},
            "applied": False,
        }

    token = _require_token(token)
    created = _request_json("POST", url, token, {"body": body}, timeout, (201,), _send)
    comment_id = created.get("id") if isinstance(created, dict) else None

    base: dict[str, Any] = {
        "dry_run": False,
        "applied": True,
        "comment_id": comment_id,
        "html_url": created.get("html_url") if isinstance(created, dict) else None,
    }

    # Read the comment back by its exact id when the response gave one: listing
    # comments is paginated, so a created comment can fall off the first page.
    if isinstance(comment_id, int):
        endpoint: str = _comment_url(owner, repo, comment_id)
    else:
        endpoint = f"{url}?per_page=100"

    try:
        remote = _request_json("GET", endpoint, token, None, timeout, (200,), _send)
    except RadarWriteError as exc:
        return _unverified(base, f"comment landed as {comment_id} but the read-back failed: {exc}")

    if isinstance(remote, list):
        remote = next(
            (
                item
                for item in remote
                if isinstance(item, dict) and item.get("id") == comment_id
            ),
            None,
        )
        if remote is None:
            return _unverified(base, f"comment {comment_id} was not on the first page of comments")
    if not isinstance(remote, dict):
        return _unverified(base, f"comment {comment_id} read-back was not an object")
    if remote.get("body") != body:
        return _unverified(base, f"comment {comment_id} body does not match remotely")

    base["html_url"] = remote.get("html_url")
    base["verified"] = True
    return base


def close_issue(
    owner: str,
    repo: str,
    number: int,
    token: str | None,
    comment: str | None = None,
    state_reason: str = "not_planned",
    dry_run: bool = False,
    timeout: float = 30.0,
    _send: Callable | None = None,
) -> dict[str, Any]:
    """Comment on and close an Issue, then verify the remote state.

    Closing preserves the Issue: title, body, author and dates all remain
    readable. Nothing is ever deleted, because the GitHub API cannot delete an
    Issue and the history is the point.
    """
    if state_reason not in ("completed", "not_planned"):
        raise RadarWriteError(f"invalid state_reason: {state_reason!r}")

    url = f"{_issues_url(owner, repo)}/{number}"
    if dry_run:
        plan: dict[str, Any] = {
            "dry_run": True,
            "method": "PATCH",
            "url": url,
            "payload": {"state": "closed", "state_reason": state_reason},
            "applied": False,
        }
        if comment:
            plan["comment_plan"] = {"method": "POST", "url": f"{url}/comments"}
        return plan

    token = _require_token(token)
    comment_result = None
    if comment:
        comment_result = comment_issue(
            owner, repo, number, comment, token, dry_run=False, timeout=timeout, _send=_send
        )

    _request_json(
        "PATCH",
        url,
        token,
        {"state": "closed", "state_reason": state_reason},
        timeout,
        (200,),
        _send,
    )

    base: dict[str, Any] = {
        "dry_run": False,
        "applied": True,
        "number": number,
        "state_reason": state_reason,
        "comment_id": (comment_result or {}).get("comment_id"),
    }
    try:
        remote = _request_json("GET", url, token, None, timeout, (200,), _send)
    except RadarWriteError as exc:
        return _unverified(base, f"close landed for Issue #{number} but the read-back failed: {exc}")
    if not isinstance(remote, dict):
        return _unverified(base, f"Issue #{number} read-back was not an object")
    if remote.get("state") != "closed":
        return _unverified(base, f"Issue #{number} reads back as {remote.get('state')!r}, not closed")

    base["state"] = remote.get("state")
    base["state_reason"] = remote.get("state_reason")
    base["verified"] = True
    return base


# --------------------------------------------------------------------------- #
# INVENTARIO.md (contents API with sha-based version control)
# --------------------------------------------------------------------------- #


def get_inventory(
    owner: str,
    repo: str,
    path: str = DEFAULT_INVENTORY_PATH,
    token: str | None = None,
    ref: str | None = None,
    timeout: float = 30.0,
    _send: Callable | None = None,
) -> dict[str, Any]:
    """Read a file and its blob ``sha`` (the version handle for later writes)."""
    url = _contents_url(owner, repo, path)
    if ref:
        url = f"{url}?ref={ref}"
    payload = _request_json("GET", url, token, None, timeout, (200,), _send)
    if not isinstance(payload, dict):
        raise VerificationError(f"unexpected contents payload for {path}")
    if payload.get("encoding") not in (None, "base64"):
        raise RadarWriteError(
            f"{path} is too large for the contents API (encoding="
            f"{payload.get('encoding')!r}); use the git trees/raw API instead"
        )
    text = ""
    raw = payload.get("content")
    if raw:
        try:
            text = base64.b64decode(raw).decode("utf-8")
        except (ValueError, UnicodeDecodeError) as exc:
            raise VerificationError(f"cannot decode {path}") from exc
    return {"path": payload.get("path", path), "sha": payload.get("sha"), "text": text}


def update_inventory(
    owner: str,
    repo: str,
    transform: Callable[[str], str],
    token: str | None,
    message: str,
    path: str = DEFAULT_INVENTORY_PATH,
    branch: str | None = None,
    base_text: str | None = None,
    require_unchanged_base: bool = False,
    max_attempts: int = DEFAULT_MAX_ATTEMPTS,
    dry_run: bool = False,
    timeout: float = 30.0,
    _send: Callable | None = None,
) -> dict[str, Any]:
    """Update a file with sha-based optimistic concurrency.

    ``transform`` maps the *current* remote text to the desired new text. It is
    re-applied to freshly fetched content after every conflict, so a retry
    preserves whatever another writer added instead of overwriting it.

    With ``require_unchanged_base`` the update aborts (rather than retries) if
    the file changed since ``base_text`` was read: that mode is for callers
    whose intent is a full replacement and who must not clobber anyone.

    In dry-run mode the current file *is* fetched, with the configured
    credential, because the target ``sha`` and the ``changed`` flag cannot
    otherwise be reported; the result carries ``reads_in_dry_run: true``.
    """
    if max_attempts < 1:
        raise RadarWriteError("max_attempts must be >= 1")

    url = _contents_url(owner, repo, path)
    attempts: list[dict[str, Any]] = []

    if dry_run:
        current = get_inventory(owner, repo, path, token, branch, timeout, _send)
        new_text = transform(current["text"])
        return {
            "dry_run": True,
            "method": "PUT",
            "url": url,
            "reads_in_dry_run": True,
            "sha": current["sha"],
            "changed": new_text != current["text"],
            "applied": False,
        }

    token = _require_token(token)

    for attempt in range(1, max_attempts + 1):
        current = get_inventory(owner, repo, path, token, branch, timeout, _send)

        if require_unchanged_base and base_text is not None and current["text"] != base_text:
            raise ConflictError(
                f"{path} changed remotely since it was read; refusing to overwrite it"
            )

        new_text = transform(current["text"])
        if new_text == current["text"]:
            return {
                "dry_run": False,
                "applied": False,
                "changed": False,
                "sha": current["sha"],
                "attempts": attempts,
                "reason": "no change required",
            }

        payload: dict[str, Any] = {
            "message": message,
            "content": base64.b64encode(new_text.encode("utf-8")).decode("ascii"),
            "sha": current["sha"],
        }
        if branch:
            payload["branch"] = branch

        try:
            response = _request_json("PUT", url, token, payload, timeout, (200, 201), _send)
        except ConflictError as exc:
            attempts.append({"attempt": attempt, "result": "conflict", "detail": str(exc)})
            continue

        new_sha = (response.get("content") or {}).get("sha") if isinstance(response, dict) else None
        commit_sha = (response.get("commit") or {}).get("sha") if isinstance(response, dict) else None

        base: dict[str, Any] = {
            "dry_run": False,
            "applied": True,
            "changed": True,
            "previous_sha": current["sha"],
            "commit_sha": commit_sha,
            "attempts": attempts,
            "sha": new_sha,
        }

        # Read-back verification. The PUT succeeded, so a mismatch means the file
        # was already updated -- reporting that as a failure would invite a retry
        # that appends the row twice.
        try:
            remote = get_inventory(owner, repo, path, token, branch, timeout, _send)
        except RadarWriteError as exc:
            return _unverified(base, f"the write landed but the read-back failed: {exc}")
        if remote["text"] != new_text:
            base["sha"] = remote["sha"]
            return _unverified(
                base, "the file changed between the write and the read-back (another writer)"
            )
        if new_sha and remote["sha"] != new_sha:
            base["sha"] = remote["sha"]
            return _unverified(base, "the content matches but the file sha does not")

        base["sha"] = remote["sha"]
        base["verified"] = True
        return base

    raise ConflictError(
        f"{path} could not be updated after {max_attempts} attempts "
        "(concurrent writes kept winning); nothing was written"
    )


def append_inventory_row(
    current_text: str, row: str, anchor: str | None = None
) -> str:
    """Return ``current_text`` with ``row`` appended as a new Markdown table row.

    Appends after the last table row at or below ``anchor``; without an anchor,
    appends after the final table row of the document.

    Two guards: an identical row already present makes the call a no-op, so a
    retry after an inconclusive verification cannot register the candidate
    twice; and an ``anchor`` that matches nothing is an error rather than a
    silent append into whichever table happens to come last.
    """
    lines = current_text.splitlines()
    row = row.strip()
    if not row.startswith("|"):
        row = f"| {row.strip('|').strip()} |"

    if any(line.strip() == row for line in lines):
        # Already registered: this is a retry, not a new candidate.
        return current_text

    insert_at: int | None = None
    start = 0
    if anchor:
        found = next((index for index, line in enumerate(lines) if anchor in line), None)
        if found is None:
            raise RadarWriteError(
                f"anchor {anchor!r} was not found in the inventory; refusing to "
                "guess which table the row belongs to"
            )
        start = found

    for index in range(start, len(lines)):
        if lines[index].strip().startswith("|"):
            insert_at = index + 1
    if insert_at is None:
        return current_text.rstrip("\n") + "\n" + row + "\n"

    lines.insert(insert_at, row)
    return "\n".join(lines) + ("\n" if current_text.endswith("\n") else "")


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #


def _add_global_options(parser: argparse.ArgumentParser, *, suppress: bool) -> None:
    """Register the global flags.

    They are added to the top-level parser *and* to every subcommand. On the
    subcommands the defaults are suppressed, so a flag given before the
    subcommand is not clobbered by the subparser's own default -- argparse
    would otherwise reset it.
    """
    # SUPPRESS keeps a value already parsed by the top-level parser intact; a
    # real subparser default would otherwise overwrite it.
    registry_default = argparse.SUPPRESS if suppress else DEFAULT_REGISTRY
    timeout_default = argparse.SUPPRESS if suppress else 30.0
    apply_default = argparse.SUPPRESS if suppress else False

    parser.add_argument(
        "--registry-repo",
        default=registry_default,
        help="owner/repo of the radar registry",
    )
    parser.add_argument("--timeout", type=float, default=timeout_default)
    parser.add_argument(
        "--apply",
        action="store_true",
        default=apply_default,
        help="actually perform the request; without it, only the plan is printed",
    )


def _add_gate_options(parser: argparse.ArgumentParser) -> None:
    """The duplicate-gate options shared by the two registering commands."""
    parser.add_argument(
        "--candidate",
        required=True,
        metavar="OWNER/REPO",
        help="the candidate being registered; the dedupe gate is bound to it",
    )
    parser.add_argument(
        "--inventory",
        default=None,
        help="local INVENTARIO.md to check (default: $RADAR_INVENTORY, then auto-detect)",
    )
    parser.add_argument(
        "--remote-inventory",
        action="store_true",
        help="check the registry's own INVENTARIO.md instead of a local copy",
    )
    parser.add_argument(
        "--alias",
        action="append",
        default=[],
        help="extra visible name for the candidate (repeatable)",
    )
    parser.add_argument(
        "--override-gate",
        default=None,
        metavar="MOTIVO",
        help="proceed despite a blocked gate; the reason is recorded in the result",
    )


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="radar_github_write.py")
    _add_global_options(parser, suppress=False)

    common = argparse.ArgumentParser(add_help=False)
    _add_global_options(common, suppress=True)

    sub = parser.add_subparsers(dest="command", required=True)

    p_create = sub.add_parser(
        "issue-create", parents=[common], help="create a candidate Issue"
    )
    p_create.add_argument("title")
    p_create.add_argument("--body-file", required=True)
    p_create.add_argument("--label", action="append", default=[])
    _add_gate_options(p_create)

    p_comment = sub.add_parser(
        "issue-comment", parents=[common], help="comment on an Issue"
    )
    p_comment.add_argument("number", type=int)
    p_comment.add_argument("--body-file", required=True)

    p_close = sub.add_parser(
        "issue-close", parents=[common], help="comment on and close an Issue"
    )
    p_close.add_argument("number", type=int)
    p_close.add_argument("--body-file", default=None)
    p_close.add_argument(
        "--state-reason", choices=("completed", "not_planned"), default="not_planned"
    )

    p_get = sub.add_parser(
        "inventory-get", parents=[common], help="read the file and its sha"
    )
    p_get.add_argument("--path", default=DEFAULT_INVENTORY_PATH)
    p_get.add_argument(
        "--full",
        action="store_true",
        help="include the whole file text (omitted by default: shape only)",
    )

    p_append = sub.add_parser(
        "inventory-append", parents=[common], help="append a candidate row"
    )
    p_append.add_argument("--row", required=True)
    p_append.add_argument("--anchor", default=None)
    p_append.add_argument("--path", default=DEFAULT_INVENTORY_PATH)
    p_append.add_argument("--message", default="radar: registrar candidato")
    p_append.add_argument("--max-attempts", type=int, default=DEFAULT_MAX_ATTEMPTS)
    _add_gate_options(p_append)

    return parser


def _read_body(path: str) -> str:
    return Path(path).read_text(encoding="utf-8")


def _split_repo(value: str) -> tuple[str, str]:
    if "/" not in value:
        raise RadarWriteError(f"expected owner/repo, got: {value!r}")
    owner, _, repo = value.partition("/")
    if not owner or not repo:
        raise RadarWriteError(f"expected owner/repo, got: {value!r}")
    return owner, repo


def _run_gate(
    args: argparse.Namespace, owner: str, repo: str, token: str | None
) -> dict[str, Any]:
    """Run dedupe for the candidate named by ``--candidate``.

    The candidate -- not the registry -- is what gets checked, and the registry
    Issues are searched for it.
    """
    cand_owner, cand_repo = _split_repo(args.candidate)
    aliases = tuple(args.alias or [])
    if args.remote_inventory:
        remote = get_inventory(
            owner, repo, DEFAULT_INVENTORY_PATH, token, None, args.timeout
        )
        result = dedupe(
            cand_owner,
            cand_repo,
            inventory_text=remote["text"],
            registry_repo=args.registry_repo,
            aliases=aliases,
            token=token,
            timeout=args.timeout,
        )
        result["inventory_source"] = f"remote:{DEFAULT_INVENTORY_PATH}"
    else:
        inventory = args.inventory or resolve_inventory_path()
        result = dedupe(
            cand_owner,
            cand_repo,
            inventory_path=inventory,
            registry_repo=args.registry_repo,
            aliases=aliases,
            token=token,
            timeout=args.timeout,
        )
        result["inventory_source"] = inventory
    return result


def main(argv: list[str] | None = None) -> int:
    """CLI entry point. Returns the exit code; never raises.

    Exit codes: 0 done, 1 failed, 3 the duplicate gate refused, 4 landed but
    unverified.
    """
    parser = _build_parser()
    args = parser.parse_args(argv)

    token: str | None = None
    dry_run = not args.apply
    try:
        token, token_source = read_token()
        owner, repo = _split_repo(args.registry_repo)
        if not dry_run:
            token = _require_token(token)

        result: dict[str, Any]
        if args.command == "issue-create":
            gate = _run_gate(args, owner, repo, token)
            print(
                json.dumps(
                    {
                        "candidate": args.candidate,
                        "dedupe_verdict": gate.get("verdict"),
                        "blocked": must_not_create(gate),
                        "inventory_source": gate.get("inventory_source"),
                    },
                    ensure_ascii=False,
                ),
                file=sys.stderr,
            )
            result = create_issue(
                owner,
                repo,
                args.title,
                _read_body(args.body_file),
                token,
                labels=args.label,
                dedupe_result=gate,
                candidate=args.candidate,
                gate_override_reason=args.override_gate,
                dry_run=dry_run,
                timeout=args.timeout,
            )
        elif args.command == "issue-comment":
            result = comment_issue(
                owner,
                repo,
                args.number,
                _read_body(args.body_file),
                token,
                dry_run=dry_run,
                timeout=args.timeout,
            )
        elif args.command == "issue-close":
            comment = _read_body(args.body_file) if args.body_file else None
            result = close_issue(
                owner,
                repo,
                args.number,
                token,
                comment=comment,
                state_reason=args.state_reason,
                dry_run=dry_run,
                timeout=args.timeout,
            )
        elif args.command == "inventory-get":
            fetched = get_inventory(owner, repo, args.path, token, None, args.timeout)
            # Shape by default: the whole file need not reach a transcript.
            result = {
                "path": fetched["path"],
                "sha": fetched["sha"],
                "chars": len(fetched["text"]),
                "lines": len(fetched["text"].splitlines()),
            }
            if args.full:
                result["text"] = fetched["text"]
        elif args.command == "inventory-append":
            gate = _run_gate(args, owner, repo, token)
            print(
                json.dumps(
                    {
                        "candidate": args.candidate,
                        "dedupe_verdict": gate.get("verdict"),
                        "blocked": must_not_create(gate),
                        "inventory_source": gate.get("inventory_source"),
                    },
                    ensure_ascii=False,
                ),
                file=sys.stderr,
            )
            # The row is the registration artifact, so it passes the same gate
            # as the Issue: a candidate already registered must not be listed twice.
            if must_not_create(gate) and not args.override_gate:
                raise DuplicateBlocked(
                    f"duplicate gate: verdict={gate.get('verdict')}; the row for "
                    f"{args.candidate} was not appended."
                )
            result = update_inventory(
                owner,
                repo,
                lambda text: append_inventory_row(text, args.row, args.anchor),
                token,
                args.message,
                path=args.path,
                max_attempts=args.max_attempts,
                dry_run=dry_run,
                timeout=args.timeout,
            )
            result["gate"] = {
                "candidate": args.candidate,
                "verdict": gate.get("verdict"),
                "override_reason": args.override_gate,
            }
        else:
            return 2
    except DuplicateBlocked as exc:
        print(f"radar_github_write: {redact(str(exc), token)}", file=sys.stderr)
        return EXIT_BLOCKED
    except RadarGitHubError as exc:
        print(f"radar_github_write: {redact(str(exc), token)}", file=sys.stderr)
        return 1
    except Exception as exc:  # noqa: BLE001 - deliberate: never leak a traceback
        print(
            f"radar_github_write: unexpected {type(exc).__name__}: "
            f"{redact(str(exc), token)}",
            file=sys.stderr,
        )
        return 1

    result["token_source"] = token_source
    result["mode"] = "dry-run" if dry_run else "apply"
    print(json.dumps(result, ensure_ascii=False, indent=2))
    if dry_run:
        print(
            "dry-run: nothing was written. Re-run with --apply to perform the request.",
            file=sys.stderr,
        )
    if result.get("applied") and not result.get("verified"):
        print(
            "warning: the write landed but could NOT be verified by reading it "
            "back; check the remote state before retrying.",
            file=sys.stderr,
        )
        return EXIT_UNVERIFIED
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
