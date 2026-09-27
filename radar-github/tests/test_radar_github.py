import json
import tempfile
import unittest
from pathlib import Path

import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from radar_github import (
    RadarGitHubError,
    _inventory_matches,
    dedupe,
    fetch_repo,
    _license_label,
    parse_github_url,
    render_card,
)


class ParseTests(unittest.TestCase):
    def test_https_url(self):
        out = parse_github_url("https://github.com/EKKOLearnAI/ekko-studio")
        self.assertEqual(out["slug"], "EKKOLearnAI/ekko-studio")
        self.assertEqual(out["html_url"], "https://github.com/EKKOLearnAI/ekko-studio")

    def test_url_with_trailing_slash_and_git(self):
        out = parse_github_url("https://github.com/owner/repo.git/")
        self.assertEqual(out["slug"], "owner/repo")

    def test_subpath_ignored(self):
        out = parse_github_url("https://github.com/owner/repo/blob/main/LICENSE")
        self.assertEqual(out["slug"], "owner/repo")

    def test_bare_slug(self):
        out = parse_github_url("owner/repo")
        self.assertEqual(out["slug"], "owner/repo")

    def test_ssh_form(self):
        out = parse_github_url("git@github.com:owner/repo.git")
        self.assertEqual(out["slug"], "owner/repo")

    def test_rejects_non_github(self):
        with self.assertRaises(RadarGitHubError):
            parse_github_url("https://example.com/owner/repo")

    def test_rejects_missing_repo(self):
        with self.assertRaises(RadarGitHubError):
            parse_github_url("https://github.com/owner")


class InventoryTests(unittest.TestCase):
    def test_inventory_matches_by_url_and_slug(self):
        text = (
            "| ekko | https://github.com/EKKOLearnAI/ekko-studio |\n"
            "| other | https://github.com/other/thing |\n"
        )
        hits = _inventory_matches(text, "EKKOLearnAI", "ekko-studio")
        self.assertEqual(len(hits), 1)

    def test_inventory_matches_by_slug_only(self):
        text = "| candidate | [ficha](...#1) | EKKOLearnAI/ekko-studio |\n"
        hits = _inventory_matches(text, "EKKOLearnAI", "ekko-studio")
        self.assertEqual(len(hits), 1)

    def test_inventory_no_match(self):
        hits = _inventory_matches("| x | github.com/a/b |\n", "EKKOLearnAI", "ekko-studio")
        self.assertEqual(hits, [])


class RenderCardTests(unittest.TestCase):
    def test_card_has_required_sections(self):
        summary = {
            "html_url": "https://github.com/o/r",
            "description": "demo",
            "license": {"spdx_id": "BSL-1.1", "name": "Business Source License 1.1"},
            "license_path": "LICENSE",
            "pushed_at": "2026-09-26T13:14:07Z",
            "archived": False,
            "language": "TypeScript",
            "stargazers_count": 11199,
            "open_issues_count": 427,
            "topics": ["dashboard", "hermes"],
            "latest_release": {
                "tag_name": "v0.7.24",
                "published_at": "2026-09-22T13:37:05Z",
            },
        }
        fields = {
            "need": "demo need",
            "benefit": "demo benefit",
            "classification": "🟡",
            "state": "🔎 DESCUBIERTO",
            "evidence": "demo",
            "limits": "demo",
            "priority": "media",
            "proof": "demo",
            "result": "demo",
            "next_step": "demo",
            "destination": "demo",
        }
        card = render_card("o", "r", summary, fields)
        for heading in (
            "Nombre y enlace original",
            "Necesidad que resuelve",
            "Clasificación técnica",
            "Estado",
            "Prueba suficiente",
            "Datos técnicos observados",
        ):
            self.assertIn(heading, card)
        self.assertIn("BSL-1.1", card)
        self.assertIn("v0.7.24", card)

    def test_license_label_from_bsl_text(self):
        summary = {
            "license": {"spdx_id": "NOASSERTION", "name": "Other"},
            "license_text": "Business Source License 1.1\n\nParameters\n",
        }
        self.assertEqual(_license_label(summary), "Business Source License 1.1 (BSL 1.1)")

    def test_license_label_falls_back_to_spdx(self):
        summary = {"license": {"spdx_id": "MIT", "name": "MIT License"}, "license_text": None}
        self.assertEqual(_license_label(summary), "MIT")


class FetchTests(unittest.TestCase):
    def _payloads(self):
        meta = {
            "html_url": "https://github.com/o/r",
            "description": "desc",
            "homepage": "https://r.example",
            "default_branch": "main",
            "language": "TypeScript",
            "topics": ["a"],
            "stargazers_count": 1,
            "forks_count": 2,
            "open_issues_count": 3,
            "archived": False,
            "disabled": False,
            "fork": False,
            "created_at": "2026-01-01T00:00:00Z",
            "updated_at": "2026-02-01T00:00:00Z",
            "pushed_at": "2026-03-01T00:00:00Z",
            "visibility": "public",
            "license": {"spdx_id": "MIT", "name": "MIT"},
        }
        return meta

    def test_fetch_summary(self):
        import base64

        meta = self._payloads()
        license_body = base64.b64encode(b"MIT License text").decode()

        def fake_get(url, headers=None, timeout=30.0):
            if url.endswith("/license"):
                return json.dumps(
                    {
                        "license": meta["license"],
                        "path": "LICENSE",
                        "content": license_body,
                    }
                ).encode()
            if url.endswith("/languages"):
                return json.dumps({"TypeScript": 100}).encode()
            if url.endswith("/releases/latest"):
                return json.dumps({"tag_name": "v1", "prerelease": False}).encode()
            if url.endswith("/readme"):
                return b"# README"
            return json.dumps(meta).encode()

        summary = fetch_repo("o", "r", _get=fake_get)
        self.assertEqual(summary["slug"], "o/r")
        self.assertEqual(summary["license"]["spdx_id"], "MIT")
        self.assertEqual(summary["license_text"], "MIT License text")
        self.assertEqual(summary["languages"], {"TypeScript": 100})
        self.assertEqual(summary["latest_release"]["tag_name"], "v1")


class DedupeTests(unittest.TestCase):
    def test_dedupe_uses_inventory_without_network(self):
        with tempfile.TemporaryDirectory() as tmp:
            inv = Path(tmp) / "INVENTARIO.md"
            inv.write_text("| x | https://github.com/EKKOLearnAI/ekko-studio |\n")
            result = dedupe(
                "EKKOLearnAI",
                "ekko-studio",
                inventory_path=str(inv),
                _get=lambda *a, **k: (_ for _ in ()).throw(RadarGitHubError("offline")),
            )
        self.assertEqual(len(result["inventory"]), 1)
        self.assertIn("error", result["issues"])


if __name__ == "__main__":
    unittest.main()
