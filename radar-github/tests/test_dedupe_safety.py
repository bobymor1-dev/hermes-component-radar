"""DE-01 regression suite: duplicate detection must not produce false negatives.

Every case here is offline. The two failure modes this suite pins down are:

* a candidate listed by *visible name* only (no slug in the row) used to be
  invisible to ``dedupe`` -- the exact cause of the DonSeTch #7/#8 duplication;
* a registry that could not be consulted used to look identical to "the
  candidate is not registered".
"""

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path


SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
FIXTURE = Path(__file__).resolve().parent / "fixtures" / "INVENTARIO.md"
sys.path.insert(0, str(SCRIPTS))

from radar_github import (  # noqa: E402
    MIN_NAME_LENGTH,
    RadarGitHubError,
    _inventory_scan,
    dedupe,
    is_duplicate,
    must_not_create,
    resolve_inventory_path,
)


# The exact shape of the real row that was missed in production.
REAL_DONSETCH_ROW = (
    "| DonSeTch | [Issue #7](https://github.com/bobymor1-dev/hermes-component-radar"
    "/issues/7) | PROBAR. Sustituye a Hound, abandonado; sin validación local. | "
    "Comprobar requisitos y preparar una prueba real. |"
)

FIXTURE_TEXT = FIXTURE.read_text(encoding="utf-8")


def _online_empty(url, headers=None, timeout=30.0):
    """An Issues search that really ran and really found nothing."""
    return json.dumps({"total_count": 0, "items": []}).encode("utf-8")


def _online_hit_for(lead: str):
    """An Issues search that found one card whose title leads with ``lead``."""
    def getter(url, headers=None, timeout=30.0):
        return json.dumps(
            {
                "total_count": 1,
                "items": [
                    {
                        "number": 7,
                        "title": f"{lead} \u2014 Probar b\u00fasqueda y lectura web",
                        "html_url": (
                            "https://github.com/bobymor1-dev/hermes-component-radar"
                            "/issues/7"
                        ),
                        "state": "open",
                    }
                ],
            }
        ).encode("utf-8")

    return getter


_online_hit = _online_hit_for("DonSeTch")


def _online_hits(*titles):
    """An Issues search returning cards with the given titles."""
    def getter(url, headers=None, timeout=30.0):
        return json.dumps(
            {
                "total_count": len(titles),
                "items": [
                    {
                        "number": index,
                        "title": title,
                        "html_url": "https://github.com/bobymor1-dev/hermes-component-radar/issues/1",
                        "state": "open",
                    }
                    for index, title in enumerate(titles, start=1)
                ],
            }
        ).encode("utf-8")

    return getter


def _offline(*args, **kwargs):
    raise RadarGitHubError("network error: simulated outage")


class InventoryScanTests(unittest.TestCase):
    def test_full_slug_match_is_exact(self):
        scan = _inventory_scan(FIXTURE_TEXT, "EKKOLearnAI", "ekko-studio")
        self.assertEqual(len(scan["exact"]), 1)
        self.assertIn("ekko-studio", scan["exact"][0])
        self.assertEqual(scan["name"], [])

    def test_visible_name_match_without_slug(self):
        scan = _inventory_scan(REAL_DONSETCH_ROW, "dondai44423", "donsetch")
        self.assertEqual(scan["exact"], [], "the row carries no candidate slug")
        self.assertEqual(len(scan["name"]), 1, "'DonSeTch' must match 'donsetch'")
        self.assertFalse(scan["ambiguous"])

    def test_case_and_separator_insensitive(self):
        for row in (
            "| DonSeTch | [Issue #7](x) |",
            "| donsetch | [Issue #7](x) |",
            "| DON_SETCH | [Issue #7](x) |",
            "| don-set-ch | [Issue #7](x) |",
        ):
            with self.subTest(row=row):
                scan = _inventory_scan(row, "dondai44423", "donsetch")
                self.assertEqual(len(scan["name"]), 1, row)

    def test_hyphenated_repo_matches_display_name(self):
        scan = _inventory_scan("| Ekko Studio | [Issue #1](x) |", "EKKOLearnAI", "ekko-studio")
        self.assertEqual(len(scan["name"]), 1)

    def test_same_name_different_owner_is_ambiguous(self):
        row = "| donsetch | https://github.com/someone-else/donsetch |"
        scan = _inventory_scan(row, "dondai44423", "donsetch")
        self.assertTrue(scan["ambiguous"], "another owner's donsetch is a collision")

    def test_too_short_name_is_ambiguous(self):
        scan = _inventory_scan("| go | [Issue #1](x) |", "example", "go")
        self.assertTrue(scan["ambiguous"])
        self.assertTrue(len("go") < MIN_NAME_LENGTH)

    def test_unrelated_row_does_not_match(self):
        scan = _inventory_scan(FIXTURE_TEXT, "someone", "totally-different")
        self.assertEqual(scan["exact"], [])
        self.assertEqual(scan["name"], [])
        self.assertFalse(scan["ambiguous"])


class DedupeVerdictTests(unittest.TestCase):
    def _dedupe(self, owner, repo, _get, inventory=FIXTURE):
        return dedupe(owner, repo, inventory_path=str(inventory), _get=_get)

    def test_exact_slug_is_duplicate_even_offline(self):
        result = self._dedupe("EKKOLearnAI", "ekko-studio", _offline)
        self.assertEqual(result["verdict"], "duplicate")
        self.assertTrue(is_duplicate(result))
        self.assertTrue(must_not_create(result))

    def test_issue_hit_is_duplicate(self):
        result = self._dedupe("nobody", "nothing-here", _online_hit_for("nothing-here"))
        self.assertEqual(result["verdict"], "duplicate")
        self.assertTrue(must_not_create(result))

    def test_a_body_only_search_hit_is_not_evidence(self):
        """A full-text hit on an ordinary word must not read as a duplicate.

        The registry's own cards mention words like ``candidato`` in their text,
        so a candidate of that name used to be declared duplicate instantly.
        """
        result = self._dedupe(
            "otro",
            "candidato",
            _online_hits(
                "jCodeMunch \u2014 Reserva por falta de utilidad actual",
                "Superpowers \u2014 Evaluar plugin Hermes",
            ),
        )
        self.assertEqual(result["verdict"], "not_found")
        self.assertFalse(result["review_required"])
        self.assertEqual(result["issues"]["matches"], [])
        self.assertEqual(result["issues"]["suggestive"], [])
        self.assertGreater(result["issues"]["unrelated"], 0)

    def test_a_title_mentioning_the_name_is_only_suggestive(self):
        result = self._dedupe("otro", "radar", _online_hits("Mejoras del radar \u2014 pendientes"))
        self.assertEqual(result["issues"]["matches"], [])
        self.assertEqual(len(result["issues"]["suggestive"]), 1)
        self.assertEqual(result["verdict"], "review_required")

    def test_a_title_leading_with_the_name_is_conclusive(self):
        result = self._dedupe("otro", "radar", _online_hits("Radar \u2014 Probar"))
        self.assertEqual(len(result["issues"]["matches"]), 1)
        self.assertEqual(result["verdict"], "duplicate")

    def test_a_prose_line_without_a_record_hint_does_not_match(self):
        """The document's own prose mentions candidates without listing them."""
        with tempfile.TemporaryDirectory() as tmp:
            inv = Path(tmp) / "INVENTARIO.md"
            inv.write_text(
                "Registrar un candidato no autoriza su instalaci\u00f3n.\n"
                "1. Elegir un candidato pendiente.\n",
                encoding="utf-8",
            )
            result = dedupe("otro", "candidato", inventory_path=str(inv), _get=_online_empty)
        self.assertEqual(result["inventory_name"], [])
        self.assertEqual(result["verdict"], "not_found")

    def test_a_prose_line_pointing_at_a_record_still_matches(self):
        with tempfile.TemporaryDirectory() as tmp:
            inv = Path(tmp) / "INVENTARIO.md"
            inv.write_text(
                "- Descartado: hound, ver https://github.com/owner/hound\n",
                encoding="utf-8",
            )
            result = dedupe("otro", "hound", inventory_path=str(inv), _get=_online_empty)
        self.assertTrue(result["inventory_name"], "a prose record must still be found")
        self.assertEqual(result["verdict"], "review_required")

    def test_name_only_match_requires_review_online(self):
        """The real DonSeTch case: indexed only by display name."""
        result = self._dedupe("dondai44423", "donsetch", _online_empty)
        self.assertEqual(result["inventory"], [])
        self.assertTrue(result["inventory_name"])
        self.assertTrue(result["review_required"])
        self.assertEqual(result["verdict"], "review_required")
        self.assertFalse(is_duplicate(result), "a name match is not conclusive")
        self.assertTrue(must_not_create(result), "but it must still block creation")

    def test_offline_is_indeterminate_never_not_found(self):
        result = self._dedupe("nobody", "nothing-here", _offline)
        self.assertEqual(result["verdict"], "indeterminate")
        self.assertFalse(is_duplicate(result))
        self.assertTrue(must_not_create(result))
        self.assertIn("error", result["issues"])
        self.assertFalse(result["checks_complete"])

    def test_offline_with_name_match_stays_blocked(self):
        result = self._dedupe("dondai44423", "donsetch", _offline)
        self.assertTrue(must_not_create(result))
        self.assertNotEqual(result["verdict"], "not_found")

    def test_missing_inventory_is_not_conclusive(self):
        """An unchecked inventory must not yield a green light either."""
        result = dedupe("nobody", "nothing-here", inventory_path=None, _get=_online_empty)
        self.assertFalse(result["inventory_checked"])
        self.assertFalse(result["checks_complete"])
        self.assertEqual(result["verdict"], "indeterminate")
        self.assertTrue(must_not_create(result))

    def test_complete_and_empty_is_the_only_not_found(self):
        result = self._dedupe("nobody", "nothing-here", _online_empty)
        self.assertTrue(result["checks_complete"])
        self.assertEqual(result["verdict"], "not_found")
        self.assertFalse(must_not_create(result))

    def test_a_name_only_inside_another_candidates_cell_does_not_match(self):
        """Name matching reads the candidate-name column, not the whole line.

        The fixture's DonSeTch row mentions other projects in its description
        cells; a candidate sharing one of those words is not registered.
        """
        for repo in ("hound", "radar"):
            with self.subTest(repo=repo):
                result = self._dedupe("otro", repo, _online_empty)
                self.assertEqual(result["inventory"], [])
                self.assertEqual(result["inventory_name"], [])
                self.assertEqual(result["verdict"], "not_found")

    def test_a_column_title_is_not_a_candidate(self):
        """`Candidato` is a column heading, not a registered candidate."""
        with tempfile.TemporaryDirectory() as tmp:
            inv = Path(tmp) / "INVENTARIO.md"
            inv.write_text(
                "| Candidato | Ficha |\n|---|---|\n| algo | [Issue #1](x) |\n",
                encoding="utf-8",
            )
            result = dedupe("otro", "candidato", inventory_path=str(inv), _get=_online_empty)
        self.assertEqual(result["inventory_name"], [], "the header row is not a record")
        self.assertEqual(result["verdict"], "not_found")

    def test_the_candidate_name_column_still_matches(self):
        result = self._dedupe("dondai44423", "donsetch", _online_empty)
        self.assertTrue(result["inventory_name"])
        self.assertEqual(result["verdict"], "review_required")

    def test_a_title_mentioning_the_word_does_not_match(self):
        with tempfile.TemporaryDirectory() as tmp:
            inv = Path(tmp) / "INVENTARIO.md"
            inv.write_text("# Inventario de candidatos del radar\n", encoding="utf-8")
            result = dedupe("otro", "radar", inventory_path=str(inv), _get=_online_empty)
        self.assertEqual(result["inventory_name"], [])
        self.assertEqual(result["verdict"], "not_found")

    def test_an_alias_can_declare_a_display_name(self):
        """A row listing a display name unlike the slug must still be found."""
        with tempfile.TemporaryDirectory() as tmp:
            inv = Path(tmp) / "INVENTARIO.md"
            inv.write_text("| DonSeTch tool | [Issue #7](x) | PROBAR. |\n", encoding="utf-8")
            without = dedupe(
                "dondai44423", "donsetch-cli", inventory_path=str(inv), _get=_online_empty
            )
            with_alias = dedupe(
                "dondai44423", "donsetch-cli", inventory_path=str(inv),
                aliases=("DonSeTch tool",), _get=_online_empty,
            )
        self.assertEqual(
            without["inventory_name"], [], "the slug does not spell the display name"
        )
        self.assertTrue(with_alias["inventory_name"], "the declared alias must match")
        self.assertEqual(with_alias["verdict"], "review_required")

    def test_collision_with_a_hyphenated_name_is_ambiguous(self):
        """The same repository name under another owner is a collision."""
        with tempfile.TemporaryDirectory() as tmp:
            inv = Path(tmp) / "INVENTARIO.md"
            inv.write_text(
                "| ekko-studio | https://github.com/someone-else/ekko-studio | PROBAR. |\n",
                encoding="utf-8",
            )
            result = dedupe("EKKOLearnAI", "ekko-studio", inventory_path=str(inv), _get=_online_empty)
        self.assertTrue(
            result["inventory_ambiguous"],
            "a hyphenated name must not defeat the collision check",
        )
        self.assertEqual(result["verdict"], "review_required")

    def test_the_remote_inventory_text_can_be_checked_directly(self):
        """The gate can use the authoritative remote copy, with no local clone."""
        text = "| DonSeTch | [Issue #7](x) | PROBAR. |\n"
        result = dedupe(
            "dondai44423", "donsetch", inventory_text=text, _get=_online_empty
        )
        self.assertTrue(result["inventory_checked"])
        self.assertTrue(result["checks_complete"])
        self.assertTrue(result["inventory_name"])
        self.assertEqual(result["verdict"], "review_required")

    def test_short_name_ambiguity_blocks(self):
        with tempfile.TemporaryDirectory() as tmp:
            inv = Path(tmp) / "INVENTARIO.md"
            inv.write_text("| go | [Issue #1](x) | REVISAR. |\n", encoding="utf-8")
            result = dedupe("example", "go", inventory_path=str(inv), _get=_online_empty)
        self.assertTrue(result["inventory_ambiguous"])
        self.assertEqual(result["verdict"], "review_required")
        self.assertTrue(must_not_create(result))

    def test_review_flag_never_contradicts_the_verdict(self):
        """A conclusive verdict must not also ask for a review."""
        for repo, getter in (
            ("donsetch", _online_hit),
            ("donsetch", _online_empty),
            ("ekko-studio", _offline),
        ):
            with self.subTest(repo=repo):
                result = self._dedupe("dondai44423" if repo == "donsetch" else "EKKOLearnAI",
                                      repo, getter)
                self.assertEqual(
                    result["review_required"], result["verdict"] == "review_required"
                )

    def test_registry_issue_hit_beats_a_name_only_inventory_row(self):
        """DonSeTch in production: name-only row, but #7/#8 confirm it."""
        result = self._dedupe("dondai44423", "donsetch", _online_hit)
        self.assertTrue(result["inventory_name"])
        self.assertEqual(result["verdict"], "duplicate")
        self.assertFalse(result["review_required"])
        self.assertTrue(must_not_create(result))

    def test_missing_inventory_file_raises(self):
        with self.assertRaises(RadarGitHubError):
            dedupe("a", "b", inventory_path="/nonexistent/INVENTARIO.md", _get=_online_empty)


class ResolveInventoryPathTests(unittest.TestCase):
    def test_explicit_path_wins(self):
        self.assertEqual(resolve_inventory_path("/tmp/x.md"), "/tmp/x.md")

    def test_env_override(self):
        previous = os.environ.get("RADAR_INVENTORY")
        os.environ["RADAR_INVENTORY"] = "/tmp/from-env.md"
        try:
            self.assertEqual(resolve_inventory_path(), "/tmp/from-env.md")
        finally:
            if previous is None:
                os.environ.pop("RADAR_INVENTORY", None)
            else:
                os.environ["RADAR_INVENTORY"] = previous

    def test_none_when_nothing_found(self):
        previous = os.environ.pop("RADAR_INVENTORY", None)
        cwd = os.getcwd()
        os.chdir("/")
        try:
            self.assertIsNone(resolve_inventory_path())
        finally:
            os.chdir(cwd)
            if previous is not None:
                os.environ["RADAR_INVENTORY"] = previous

    def test_finds_inventory_in_cwd(self):
        previous = os.environ.pop("RADAR_INVENTORY", None)
        cwd = os.getcwd()
        os.chdir(str(FIXTURE.parent))
        try:
            found = resolve_inventory_path()
            self.assertIsNotNone(found)
            self.assertTrue(found.endswith("INVENTARIO.md"))
        finally:
            os.chdir(cwd)
            if previous is not None:
                os.environ["RADAR_INVENTORY"] = previous


if __name__ == "__main__":
    unittest.main()
