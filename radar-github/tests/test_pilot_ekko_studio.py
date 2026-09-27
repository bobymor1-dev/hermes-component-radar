"""Regression: the pilot candidate must already be recognised.

This is the offline half of the Agente 0 acceptance test. It runs the real
``dedupe`` logic against an inventory fixture and proves the flow would NOT
create a duplicate card.

The fixture lives next to this test, so the check no longer depends on the skill
being checked out inside a particular repository layout. Set ``RADAR_INVENTORY``
to point the same test at a live ``INVENTARIO.md``.
"""

import os
import sys
import unittest
from pathlib import Path


SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
FIXTURE = Path(__file__).resolve().parent / "fixtures" / "INVENTARIO.md"
sys.path.insert(0, str(SCRIPTS))

from radar_github import (  # noqa: E402
    RadarGitHubError,
    dedupe,
    is_duplicate,
    must_not_create,
)


def _inventory_path() -> Path:
    """Explicit override first, then the committed fixture (always present)."""
    override = os.environ.get("RADAR_INVENTORY")
    if override and override.strip():
        return Path(override.strip())
    return FIXTURE


class PilotEkkoStudioTests(unittest.TestCase):
    def test_inventory_already_lists_ekko_studio(self):
        inventory = _inventory_path()
        self.assertTrue(inventory.is_file(), f"missing {inventory}")

        # Offline: the Issues search is expected to fail here; dedupe must still
        # surface the local inventory hit instead of claiming "not registered".
        result = dedupe(
            "EKKOLearnAI",
            "ekko-studio",
            inventory_path=str(inventory),
            _get=lambda *a, **k: (_ for _ in ()).throw(RadarGitHubError("offline")),
        )

        self.assertTrue(result["inventory"], "ekko-studio should already be in the inventory")
        self.assertIn("ekkolearnai/ekko-studio", result["inventory"][0].lower())
        self.assertTrue(is_duplicate(result))
        self.assertEqual(result["verdict"], "duplicate")
        self.assertTrue(must_not_create(result))

    def test_card_must_not_be_created_when_duplicate(self):
        # Mirrors SKILL.md step 2: do not write a new card when the gate blocks.
        duplicate_result = {
            "inventory": ["ekko-studio (`EKKOLearnAI/ekko-studio`)"],
            "issues": {"total_count": 0, "items": []},
        }
        self.assertTrue(is_duplicate(duplicate_result))
        self.assertFalse(
            is_duplicate({"inventory": [], "issues": {"total_count": 0, "items": []}})
        )

    def test_checked_but_empty_result_is_not_a_duplicate(self):
        result = {
            "inventory": [],
            "inventory_name": [],
            "issues": {"total_count": 0, "items": []},
            "issues_checked": True,
            "verdict": "not_found",
        }
        self.assertFalse(is_duplicate(result))
        self.assertFalse(must_not_create(result))

    def test_fixture_is_not_the_production_inventory(self):
        # Guard against a test silently reading a live file and changing meaning.
        self.assertEqual(_inventory_path().name, "INVENTARIO.md")


if __name__ == "__main__":
    unittest.main()
