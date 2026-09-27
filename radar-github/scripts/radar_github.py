#!/usr/bin/env python3
"""Radar GitHub helper.

Small, dependency-free utility for the radar-github skill:
  * parse   - normalize a GitHub link into owner/repo + canonical html_url
  * inspect - fetch a read-only summary from the public GitHub API
  * dedupe  - detect an already-registered candidate (INVENTARIO.md + Issues)
  * card    - render a candidate card (ficha) as Markdown

It uses only the Python standard library. Public read-only API access works
without a token (60 req/hour). Set GH_TOKEN (preferred) or GITHUB_TOKEN to raise
the limit; the write helpers in ``radar_github_write.py`` authenticate with the
same variables. Token values are never printed, logged or persisted.

Dedupe safety rules (DE-01):
  * an exact slug/URL match in INVENTARIO.md is a confirmed duplicate;
  * a match on the candidate's *visible name* alone is never auto-confirmed: it
    yields ``review_required`` so a human decides before anything is written;
  * when the Issues lookup fails the verdict is ``indeterminate``: absence of
    results is never reported as absence of duplicates.

DE-02: the CLI reads GH_TOKEN / GITHUB_TOKEN from the environment. Only the
variable *name* is ever echoed; the value never reaches stdout or a log.
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Mapping
from pathlib import Path
from typing import Any, Callable


API_BASE = "https://api.github.com"
DEFAULT_REGISTRY = "bobymor1-dev/hermes-component-radar"
USER_AGENT = "radar-github/0.1"
_GITHUB_HOST = "github.com"

#: Environment variables consulted for an optional token, in priority order.
TOKEN_ENV_VARS: tuple[str, ...] = ("GH_TOKEN", "GITHUB_TOKEN")

#: Names shorter than this are too generic to identify a candidate by name alone.
MIN_NAME_LENGTH = 4

#: Verdicts returned by :func:`dedupe`.
VERDICT_DUPLICATE = "duplicate"
VERDICT_REVIEW = "review_required"
VERDICT_INDETERMINATE = "indeterminate"
VERDICT_NOT_FOUND = "not_found"

#: Verdicts that forbid creating a new card.
BLOCKING_VERDICTS = (VERDICT_DUPLICATE, VERDICT_REVIEW, VERDICT_INDETERMINATE)

#: Replacement used when a credential value has to be scrubbed from a message.
REDACTED = "***REDACTED***"

#: Exit code returned by ``dedupe`` when the duplicate gate refuses. Distinct
#: from 0 (clear) and 1 (the check itself failed), so a script can tell them apart.
EXIT_BLOCKED = 3


def redact_text(text: str, token: str | None) -> str:
    """Remove a credential value from any text before it is emitted."""
    if not token:
        return text
    return text.replace(token, REDACTED)


class RadarGitHubError(RuntimeError):
    """Expected, human-readable failure from the helper."""


def _strip_git_suffix(value: str) -> str:
    return value[:-4] if value.endswith(".git") else value


def parse_github_url(text: str) -> dict[str, str]:
    """Normalize a GitHub reference to owner/repo + canonical html_url."""
    if not text or not text.strip():
        raise RadarGitHubError("empty input")

    raw = text.strip()

    # SSH form: git@github.com:owner/repo.git
    if raw.startswith("git@") and ":" in raw:
        host_part, _, path_part = raw.partition(":")
        if _GITHUB_HOST in host_part:
            raw = path_part

    # Absolute URL form: https://github.com/owner/repo[/...]
    if "://" in raw:
        parsed = urllib.parse.urlsplit(raw)
        if parsed.netloc.split("@")[-1].lower() not in (_GITHUB_HOST, f"www.{_GITHUB_HOST}"):
            raise RadarGitHubError(f"not a GitHub URL: {text!r}")
        parts = [p for p in parsed.path.split("/") if p]
        if len(parts) < 2:
            raise RadarGitHubError(f"URL missing owner/repo: {text!r}")
        owner, repo = parts[0], _strip_git_suffix(parts[1])
    else:
        # Bare slug: owner/repo (optionally .git)
        parts = [p for p in raw.split("/") if p]
        if len(parts) < 2:
            raise RadarGitHubError(f"expected owner/repo, got: {text!r}")
        owner, repo = parts[0], _strip_git_suffix(parts[1])

    if not re.fullmatch(r"[A-Za-z0-9_.-]+", owner) or not re.fullmatch(
        r"[A-Za-z0-9_.-]+", repo
    ):
        raise RadarGitHubError(f"invalid owner/repo: {owner}/{repo}")

    return {
        "owner": owner,
        "repo": repo,
        "slug": f"{owner}/{repo}",
        "html_url": f"https://github.com/{owner}/{repo}",
    }


def read_token(
    env: Mapping[str, str] | None = None,
) -> tuple[str | None, str | None]:
    """Return ``(token, source_name)`` from the environment (DE-02).

    ``GH_TOKEN`` takes precedence over ``GITHUB_TOKEN``. The value is handed only
    to request builders; it is never printed, logged or written to disk.
    ``source_name`` is the *variable name*, so callers can report which
    credential was used without disclosing its value.

    A blank or whitespace-only variable counts as absent, so an empty export
    cannot silently switch the helper into authenticated mode.

    A value containing CR, LF or any non-ASCII character is refused outright:
    ``urllib`` embeds such a value verbatim in ``ValueError("Invalid header
    value ...")``, which would print the credential in a traceback. Rejecting it
    here closes that path before the value can ever reach a header.
    """
    environ = os.environ if env is None else env
    for name in TOKEN_ENV_VARS:
        raw = environ.get(name)
        if not raw or not raw.strip():
            continue
        token = raw.strip()
        if not _is_plain_ascii(token):
            raise RadarGitHubError(
                f"{name} contains a control or non-ASCII character; refusing to "
                "use it (the value itself is never echoed)"
            )
        return token, name
    return None, None


def _is_plain_ascii(value: str) -> bool:
    """True when every character is a printable ASCII character."""
    return all(32 <= ord(char) < 127 for char in value)


def _http_get(
    url: str, headers: dict[str, str] | None = None, timeout: float = 30.0
) -> bytes:
    req = urllib.request.Request(url)
    for key, value in (headers or {}).items():
        req.add_header(key, value)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.read()
    except urllib.error.HTTPError as exc:
        raise RadarGitHubError(f"HTTP {exc.code} from {url}") from exc
    except ValueError as exc:
        # urllib raises ValueError for a malformed header, and its message quotes
        # the offending value verbatim. The text is deliberately not forwarded.
        raise RadarGitHubError(f"invalid request header for {url}") from exc
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise RadarGitHubError(f"network error from {url}: {exc}") from exc


def _headers(token: str | None) -> dict[str, str]:
    headers = {
        "Accept": "application/vnd.github+json",
        "User-Agent": USER_AGENT,
    }
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return headers


def _get_json(url: str, token: str | None, timeout: float, _get: Callable) -> Any:
    raw = _get(url, _headers(token), timeout)
    try:
        return json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RadarGitHubError(f"non-JSON response from {url}") from exc


def _decode_base64_maybe(content: str | None) -> str | None:
    if not content:
        return None
    try:
        return base64.b64decode(content).decode("utf-8", "replace")
    except Exception:
        return content


def fetch_repo(
    owner: str,
    repo: str,
    token: str | None = None,
    timeout: float = 30.0,
    _get: Callable | None = None,
) -> dict[str, Any]:
    """Fetch a read-only summary from the GitHub API.

    ``_get`` is the transport hook; ``None`` resolves to the real HTTP client at
    call time, so the transport stays swappable (tests, offline runs).
    """
    _get = _get or _http_get
    base = f"{API_BASE}/repos/{owner}/{repo}"

    meta = _get_json(base, token, timeout, _get)
    if not isinstance(meta, dict):
        raise RadarGitHubError("unexpected repository payload")

    summary: dict[str, Any] = {
        "slug": f"{owner}/{repo}",
        "html_url": meta.get("html_url") or f"https://github.com/{owner}/{repo}",
        "description": meta.get("description"),
        "homepage": meta.get("homepage"),
        "default_branch": meta.get("default_branch"),
        "language": meta.get("language"),
        "topics": meta.get("topics") or [],
        "stargazers_count": meta.get("stargazers_count"),
        "forks_count": meta.get("forks_count"),
        "open_issues_count": meta.get("open_issues_count"),
        "archived": bool(meta.get("archived")),
        "disabled": bool(meta.get("disabled")),
        "fork": bool(meta.get("fork")),
        "created_at": meta.get("created_at"),
        "updated_at": meta.get("updated_at"),
        "pushed_at": meta.get("pushed_at"),
        "visibility": meta.get("visibility"),
        "license": meta.get("license"),
    }

    # License body (relevant for non-standard licenses such as BSL/SSPL).
    try:
        license_payload = _get_json(f"{base}/license", token, timeout, _get)
        if isinstance(license_payload, dict):
            summary["license"] = license_payload.get("license") or summary.get("license")
            summary["license_path"] = license_payload.get("path")
            summary["license_text"] = _decode_base64_maybe(license_payload.get("content"))
    except RadarGitHubError:
        summary["license_text"] = None

    # Languages.
    try:
        languages = _get_json(f"{base}/languages", token, timeout, _get)
        summary["languages"] = languages if isinstance(languages, dict) else {}
    except RadarGitHubError:
        summary["languages"] = {}

    # Latest release.
    try:
        release = _get_json(f"{base}/releases/latest", token, timeout, _get)
        if isinstance(release, dict):
            summary["latest_release"] = {
                "tag_name": release.get("tag_name"),
                "name": release.get("name"),
                "prerelease": bool(release.get("prerelease")),
                "published_at": release.get("published_at"),
            }
    except RadarGitHubError:
        summary["latest_release"] = None

    # README preview.
    try:
        readme_raw = _get(
            f"{base}/readme",
            {**_headers(token), "Accept": "application/vnd.github.raw+json"},
            timeout,
        )
        summary["readme"] = readme_raw.decode("utf-8", "replace")[:6000]
    except RadarGitHubError:
        summary["readme"] = None

    return summary


def _normalize_name(value: str) -> str:
    """Lowercase, alphanumerics only: ``DonSeTch`` and ``donsetch`` collide."""
    return re.sub(r"[^a-z0-9]+", "", value.lower())


def _leading_cell(line: str) -> str:
    """Text of the first Markdown table cell, or ``""`` if the line is not one."""
    stripped = line.strip()
    if not stripped.startswith("|"):
        return ""
    cells = stripped.split("|")
    return cells[1].strip() if len(cells) > 2 else ""


def _name_tokens(line: str) -> set[str]:
    """Whole tokens of a line, normalized. ``| DonSeTch |`` -> ``{'donsetch'}``."""
    return {
        _normalize_name(part)
        for part in re.split(r"[^A-Za-z0-9]+", line)
        if part
    }


def _slugs_in(line: str) -> list[tuple[str, str]]:
    """Every ``owner/repo``-looking pair on a line, lowercased."""
    found: list[tuple[str, str]] = []
    for match in re.finditer(
        r"(?:github\.com/)?([A-Za-z0-9_.-]+)/([A-Za-z0-9_.-]+)", line
    ):
        found.append((match.group(1).lower(), match.group(2).lower()))
    return found


#: A Markdown table separator row (``|---|:--:|``); never a candidate record.
_TABLE_SEPARATOR_RE = re.compile(r"^\|[\s:|-]+\|$")

#: Prose only counts as a record when it looks like one: a repository link or an
#: Issue reference. Without this, an ordinary word used in the document's own
#: prose ("candidato", "radar") would flag every candidate of that name.
_RECORD_HINT_RE = re.compile(r"github\.com/|/issues/\d+|issue\s*#\d+", re.IGNORECASE)

#: Separators used in registry card titles: ``DonSeTch — Probar...``.
_LEAD_SEPARATORS = ("\u2014", "\u2013", ":", "|")


def _title_lead(title: str) -> str:
    """The candidate-name part of a card title (``DonSeTch — Probar`` -> ``DonSeTch``)."""
    head = title
    for separator in _LEAD_SEPARATORS:
        if separator in head:
            head = head.split(separator, 1)[0]
    return head


def _classify_issue_hits(
    items: list[dict[str, Any]], owner: str, repo: str, aliases: tuple[str, ...] = ()
) -> tuple[list[Any], list[Any], list[Any]]:
    """Split search hits into conclusive, suggestive and unrelated.

    The Issues search is full-text, so an unrelated card matches whenever the
    candidate's name is an ordinary word that appears somewhere in its body. A
    card is only evidence about *this* candidate when its title names it: a title
    that opens with the name is conclusive; one that mentions it elsewhere is
    suggestive; a match coming only from the body is not evidence at all.
    """
    slug = f"{owner}/{repo}".lower()
    names = {_normalize_name(repo)} | {_normalize_name(alias) for alias in aliases}
    names.discard("")
    conclusive: list[Any] = []
    suggestive: list[Any] = []
    unrelated: list[Any] = []
    for item in items:
        title = str(item.get("title") or "")
        if slug in title.lower():
            conclusive.append(item)
            continue
        lead = _normalize_name(_title_lead(title))
        if lead and lead in names:
            conclusive.append(item)
        elif names & _name_tokens(title):
            suggestive.append(item)
        else:
            unrelated.append(item)
    return conclusive, suggestive, unrelated


def _inventory_scan(
    inventory_text: str, owner: str, repo: str, aliases: tuple[str, ...] = ()
) -> dict[str, Any]:
    """Classify how a candidate appears in ``INVENTARIO.md``.

    Returns three things:

    ``exact``
        lines carrying the candidate's full slug or repository URL -- conclusive.
    ``name``
        lines naming the candidate without a slug (``DonSeTch`` matches
        ``donsetch``) -- suggestive, never conclusive.
    ``ambiguous``
        the name match cannot be trusted: the name is shorter than
        :data:`MIN_NAME_LENGTH`, or the same repository name appears under a
        *different* owner on the same line (a collision, not our candidate).

    Matching is deliberately narrow to avoid flagging unrelated prose:

    * a Markdown table row is matched by its **candidate-name column** (the first
      cell) only, so a name mentioned in a description cell -- or in another
      candidate's row -- does not count;
    * headings and table separators are skipped;
    * any other line must carry the name as a **whole token**, never as a
      substring of a longer word.

    ``aliases`` declares additional visible names for the candidate (for example
    when the display name and the repository name differ).
    """
    scan: dict[str, Any] = {"exact": [], "name": [], "ambiguous": False}
    if not inventory_text:
        return scan

    stripped_lines = [line.strip() for line in inventory_text.splitlines()]
    wanted = {_normalize_name(repo)} | {_normalize_name(alias) for alias in aliases}
    wanted.discard("")
    short_names = {name for name in wanted if len(name) < MIN_NAME_LENGTH}
    trusted = wanted - short_names

    slug_patterns = (f"github.com/{owner}/{repo}", f"{owner}/{repo}")
    owner_lower = owner.lower()
    repo_norm = _normalize_name(repo)

    for index, stripped in enumerate(stripped_lines):
        if not stripped:
            continue

        line_lower = stripped.lower()
        if any(pattern.lower() in line_lower for pattern in slug_patterns):
            scan["exact"].append(stripped)
            continue

        if not wanted or stripped.startswith("#") or _TABLE_SEPARATOR_RE.match(stripped):
            continue

        # The Markdown table header is the row immediately above the separator.
        # Its column titles ("Candidato", "Ficha") are not candidate records, and
        # a candidate whose name is one of those words must not match them.
        following = stripped_lines[index + 1] if index + 1 < len(stripped_lines) else ""
        if stripped.startswith("|") and _TABLE_SEPARATOR_RE.match(following):
            continue

        cell = _normalize_name(_leading_cell(stripped))
        if cell:
            is_table_row, hit = True, cell in wanted
        else:
            # Prose is not a record unless it points at one.
            is_table_row = False
            hit = bool(trusted & _name_tokens(stripped)) and bool(
                _RECORD_HINT_RE.search(stripped)
            )
        if not hit:
            continue

        if is_table_row and cell in short_names:
            # Too short to tell candidates apart by name alone.
            scan["ambiguous"] = True
            continue

        # A same-named repository under another owner is a different candidate.
        for found_owner, found_repo in _slugs_in(stripped):
            found_repo_norm = _normalize_name(found_repo)
            if found_repo_norm == repo_norm and found_owner != owner_lower:
                scan["ambiguous"] = True
            elif found_repo_norm not in wanted and found_owner == owner_lower:
                scan["ambiguous"] = True

        scan["name"].append(stripped)

    return scan


def _inventory_matches(inventory_text: str, owner: str, repo: str) -> list[str]:
    """Exact slug/URL matches only. Kept as the narrow, conclusive predicate."""
    return _inventory_scan(inventory_text, owner, repo)["exact"]


def resolve_inventory_path(explicit: str | None = None) -> str | None:
    """Locate ``INVENTARIO.md`` without assuming one fixed layout.

    Order: the explicit argument, ``$RADAR_INVENTORY``, then a couple of
    conventional locations. Returns ``None`` when nothing is found -- callers
    must treat that as "inventory not checked", never as "no duplicates".
    """
    if explicit:
        return explicit

    env_path = os.environ.get("RADAR_INVENTORY")
    if env_path and env_path.strip():
        return env_path.strip()

    here = Path(__file__).resolve()
    candidates = [
        Path.cwd() / "INVENTARIO.md",
        here.parents[2] / "INVENTARIO.md",
        here.parents[1] / "INVENTARIO.md",
    ]
    for candidate in candidates:
        if candidate.is_file():
            return str(candidate)
    return None


def dedupe(
    owner: str,
    repo: str,
    inventory_path: str | None = None,
    registry_repo: str = DEFAULT_REGISTRY,
    token: str | None = None,
    timeout: float = 30.0,
    aliases: tuple[str, ...] = (),
    inventory_text: str | None = None,
    _get: Callable | None = None,
) -> dict[str, Any]:
    """Detect an already-registered candidate in ``INVENTARIO.md`` and Issues.

    The ``verdict`` field is the decision:

    ``duplicate``       exact slug/URL in the inventory, or a matching Issue.
    ``review_required`` name-only inventory match; a human must confirm.
    ``indeterminate``   the Issues lookup failed -- absence of results is *not*
                        evidence of absence, so this never means "not found".
    ``not_found``       both sources were actually consulted and came back empty.

    :func:`is_duplicate` reports only the conclusive case; use
    :func:`must_not_create` to decide whether a new card may be written at all.
    """
    _get = _get or _http_get
    scan: dict[str, Any] = {"exact": [], "name": [], "ambiguous": False}
    inventory_checked = False
    if inventory_text is not None:
        # A caller that already holds the authoritative text -- for instance one
        # fetched from the registry through the contents API -- needs no local
        # clone, and is checking the version that actually counts.
        scan = _inventory_scan(inventory_text, owner, repo, aliases)
        inventory_checked = True
    elif inventory_path:
        try:
            text = Path(inventory_path).read_text(encoding="utf-8")
        except OSError as exc:
            raise RadarGitHubError(f"cannot read inventory {inventory_path}: {exc}") from exc
        scan = _inventory_scan(text, owner, repo, aliases)
        inventory_checked = True

    result: dict[str, Any] = {
        "slug": f"{owner}/{repo}",
        "aliases": list(aliases),
        "inventory": scan["exact"],
        "inventory_name": scan["name"],
        "inventory_ambiguous": scan["ambiguous"],
        "inventory_checked": inventory_checked,
        "issues": {
            "total_count": 0,
            "items": [],
            "matches": [],
            "suggestive": [],
            "unrelated": 0,
        },
        "issues_checked": False,
        "checks_complete": False,
        "review_required": False,
        "verdict": VERDICT_NOT_FOUND,
    }

    query = urllib.parse.quote(f"repo:{registry_repo} {owner}/{repo} OR {repo}")
    url = f"{API_BASE}/search/issues?q={query}"
    issues_failed = False
    try:
        payload = _get_json(url, token, timeout, _get)
    except RadarGitHubError as exc:
        result["issues"]["error"] = str(exc)
        issues_failed = True
    else:
        result["issues_checked"] = True
        if isinstance(payload, dict):
            result["issues"]["total_count"] = payload.get("total_count", 0)
            items = [
                {
                    "number": item.get("number"),
                    "title": item.get("title"),
                    "html_url": item.get("html_url"),
                    "state": item.get("state"),
                }
                for item in payload.get("items", [])
                if isinstance(item, dict) and "pull_request" not in item
            ]
            conclusive, suggestive, unrelated = _classify_issue_hits(
                items, owner, repo, aliases
            )
            result["issues"]["items"] = items
            result["issues"]["matches"] = conclusive
            result["issues"]["suggestive"] = suggestive
            result["issues"]["unrelated"] = len(unrelated)

    result["checks_complete"] = bool(result["inventory_checked"]) and bool(
        result["issues_checked"]
    )
    result["verdict"] = _verdict(result, issues_failed)
    # review_required is exactly the review verdict, so the two can never
    # disagree (e.g. claiming "duplicate" while also asking for a review).
    result["review_required"] = result["verdict"] == VERDICT_REVIEW
    return result


def _verdict(result: dict[str, Any], issues_failed: bool) -> str:
    """Decide the dedupe verdict. Conclusive evidence is checked first.

    ``not_found`` requires *complete* evidence: both the inventory and the
    registry Issues must actually have been consulted. A partial check stays
    ``indeterminate``, because absence of results is not absence of duplicates.

    An Issue only counts as conclusive when its title names the candidate. A
    full-text hit coming from an unrelated card's body is not evidence, and a
    title that merely mentions the name is suggestive -- enough to ask for a
    review, not enough to declare a duplicate.
    """
    if result.get("inventory"):
        return VERDICT_DUPLICATE

    issues = result.get("issues") or {}
    if result.get("issues_checked") and (issues.get("matches") or []):
        return VERDICT_DUPLICATE

    if issues_failed or not result.get("checks_complete"):
        # The registry (or the local inventory) could not be consulted: stay
        # undecided instead of claiming the candidate is unregistered.
        return VERDICT_INDETERMINATE

    if (
        result.get("inventory_name")
        or result.get("inventory_ambiguous")
        or (issues.get("suggestive") or [])
    ):
        return VERDICT_REVIEW

    return VERDICT_NOT_FOUND


def is_duplicate(result: dict[str, Any]) -> bool:
    """True only for a *confirmed* registration.

    Falls back to the legacy field-based check when no ``verdict`` is present,
    so older callers and pinned expectations keep working.
    """
    verdict = result.get("verdict")
    if verdict is not None:
        return verdict == VERDICT_DUPLICATE

    if bool(result.get("inventory")):
        return True
    issues = result.get("issues") or {}
    if bool(issues.get("total_count")) or bool(issues.get("items")):
        return True
    return False


def must_not_create(result: dict[str, Any]) -> bool:
    """True when a new card must NOT be written.

    Stricter than :func:`is_duplicate`: a review-required or indeterminate result
    also blocks writing, because neither can prove the candidate is new.
    """
    if result.get("review_required"):
        return True
    verdict = result.get("verdict")
    if verdict is not None:
        return verdict in BLOCKING_VERDICTS
    return is_duplicate(result)


_CARD_SECTIONS = (
    ("need", "Necesidad que resuelve"),
    ("benefit", "Beneficio esperado"),
    ("classification", "Clasificación técnica"),
    ("state", "Estado"),
    ("evidence", "Evidencia disponible"),
    ("limits", "Límites"),
    ("priority", "Prioridad provisional"),
    ("proof", "Prueba suficiente"),
    ("result", "Resultado"),
    ("next_step", "Siguiente paso"),
    ("destination", "Destino exacto de ejecución"),
)


def _license_label(summary: dict[str, Any]) -> str:
    """Derive a human-readable license name, including non-standard ones."""
    text = summary.get("license_text")
    if text:
        lowered = text.lower()
        for marker, label in (
            ("business source license 1.1", "Business Source License 1.1 (BSL 1.1)"),
            ("server side public license", "Server Side Public License (SSPL)"),
            ("elastic license 2.0", "Elastic License 2.0"),
            ("apache license", "Apache License"),
            ("gnu general public license", "GNU General Public License"),
            ("mit license", "MIT License"),
        ):
            if marker in lowered:
                return label
        first_line = next((ln.strip() for ln in text.splitlines() if ln.strip()), "")
        if first_line and len(first_line) <= 80:
            return first_line
    license_info = summary.get("license") or {}
    spdx = license_info.get("spdx_id")
    if spdx and spdx not in ("NOASSERTION", "other"):
        return spdx
    return license_info.get("name") or "No determinada"


def render_card(
    owner: str,
    repo: str,
    summary: dict[str, Any] | None,
    fields: dict[str, Any] | None,
) -> str:
    """Render the candidate card (ficha) as Markdown."""
    summary = summary or {}
    fields = fields or {}
    slug = f"{owner}/{repo}"
    license_name = _license_label(summary)

    lines: list[str] = []
    lines.append(f"# {repo} — ficha de candidato")
    lines.append("")
    lines.append("## Nombre y enlace original")
    lines.append("")
    lines.append(f"- Repositorio: {summary.get('html_url') or f'https://github.com/{slug}'}")
    lines.append(f"- Slug canónico: `{slug}`")
    if summary.get("description"):
        lines.append(f"- Descripción: {summary['description']}")
    lines.append("")

    for key, label in _CARD_SECTIONS:
        value = fields.get(key)
        lines.append(f"## {label}")
        lines.append("")
        lines.append(value if value else "Pendiente.")
        lines.append("")

    lines.append("## Datos técnicos observados")
    lines.append("")
    lines.append(f"- Licencia: {license_name}")
    if summary.get("license_path"):
        lines.append(f"- Archivo de licencia: `{summary['license_path']}`")
    lines.append(f"- Última actividad (push): {summary.get('pushed_at') or 'No determinada'}")
    lines.append(f"- Archivado: {'sí' if summary.get('archived') else 'no'}")
    lines.append(f"- Idioma principal: {summary.get('language') or 'No determinado'}")
    if summary.get("stargazers_count") is not None:
        lines.append(f"- Estrellas: {summary['stargazers_count']}")
    if summary.get("open_issues_count") is not None:
        lines.append(f"- Issues abiertos: {summary['open_issues_count']}")
    release = summary.get("latest_release")
    if release and release.get("tag_name"):
        lines.append(
            f"- Última release: {release['tag_name']} "
            f"(publicada {release.get('published_at') or 'sin fecha'})"
        )
    if summary.get("topics"):
        lines.append(f"- Topics: {', '.join(summary['topics'])}")
    lines.append("")

    return "\n".join(lines).rstrip() + "\n"


def _json_fields(path: str | None) -> dict[str, Any]:
    if not path:
        return {}
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except OSError as exc:
        raise RadarGitHubError(f"cannot read fields file {path}: {exc}") from exc
    except json.JSONDecodeError as exc:
        raise RadarGitHubError(f"invalid JSON in {path}: {exc}") from exc
    return data if isinstance(data, dict) else {}


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="radar_github.py")
    sub = parser.add_subparsers(dest="command", required=True)

    p_parse = sub.add_parser("parse", help="normalize a GitHub link")
    p_parse.add_argument("text")

    p_inspect = sub.add_parser("inspect", help="fetch a repo summary")
    p_inspect.add_argument("owner")
    p_inspect.add_argument("repo")

    p_dedupe = sub.add_parser("dedupe", help="check for an existing candidate")
    p_dedupe.add_argument("owner")
    p_dedupe.add_argument("repo")
    p_dedupe.add_argument(
        "--inventory",
        default=None,
        help="path to INVENTARIO.md (default: $RADAR_INVENTORY, then auto-detect)",
    )
    p_dedupe.add_argument("--registry-repo", default=DEFAULT_REGISTRY)
    p_dedupe.add_argument(
        "--alias",
        action="append",
        default=[],
        help="extra visible name for the candidate (repeatable)",
    )

    p_card = sub.add_parser("card", help="render a candidate card")
    p_card.add_argument("owner")
    p_card.add_argument("repo")
    p_card.add_argument("--data", default=None, help="JSON file with card fields")
    p_card.add_argument("--out", default=None, help="write Markdown to a file")
    p_card.add_argument("--inspect", action="store_true", help="fetch summary before render")

    return parser


def _dispatch(args: argparse.Namespace, token: str | None, token_source: str | None) -> int:
    """Run the parsed command. Split out so :func:`main` owns the error contract."""
    if args.command == "parse":
        parsed = parse_github_url(args.text)
        print(json.dumps(parsed, ensure_ascii=False))
        return 0

    if args.command == "inspect":
        summary = fetch_repo(args.owner, args.repo, token=token)
        summary["token_source"] = token_source
        print(json.dumps(summary, ensure_ascii=False, indent=2))
        return 0

    if args.command == "dedupe":
        inventory = resolve_inventory_path(args.inventory)
        result = dedupe(
            args.owner,
            args.repo,
            inventory_path=inventory,
            registry_repo=args.registry_repo,
            aliases=tuple(args.alias or ()),
            token=token,
        )
        result["token_source"] = token_source
        result["inventory_path"] = inventory
        result["blocked"] = must_not_create(result)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        # A blocked verdict is a refusal, not a query result: scripted callers
        # key on the exit status, so it must not read as success.
        return EXIT_BLOCKED if result["blocked"] else 0

    if args.command == "card":
        summary = fetch_repo(args.owner, args.repo, token=token) if args.inspect else None
        fields = _json_fields(args.data)
        markdown = render_card(args.owner, args.repo, summary, fields)
        if args.out:
            Path(args.out).parent.mkdir(parents=True, exist_ok=True)
            Path(args.out).write_text(markdown, encoding="utf-8")
            print(f"wrote {args.out}")
        else:
            sys.stdout.write(markdown)
        return 0

    return 2


def main(argv: list[str] | None = None) -> int:
    """CLI entry point. Returns the exit code; never raises.

    Exit codes: 0 clear, 1 the operation failed, 3 the duplicate gate refused.

    Keeping the error contract inside ``main`` makes the failure path testable.
    Any unexpected exception is turned into a redacted one-liner: a traceback
    from ``urllib`` can quote a header value, i.e. the credential.
    """
    parser = _build_parser()
    args = parser.parse_args(argv)

    # DE-02: the CLI honours GH_TOKEN / GITHUB_TOKEN. Only the variable *name*
    # ever reaches the output; the value is not printed or logged.
    token: str | None = None
    try:
        token, token_source = read_token()
        return _dispatch(args, token, token_source)
    except RadarGitHubError as exc:
        print(f"radar_github: {redact_text(str(exc), token)}", file=sys.stderr)
        return 1
    except Exception as exc:  # noqa: BLE001 - deliberate: never leak a traceback
        print(
            f"radar_github: unexpected {type(exc).__name__}: "
            f"{redact_text(str(exc), token)}",
            file=sys.stderr,
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
