import json
import tempfile
import unittest
from pathlib import Path

from campaign_evidence import classify_text, coverage
from bot_detector import Message, normalize_text


class CampaignEvidenceTests(unittest.TestCase):
    def classify(self, text):
        return classify_text(normalize_text(text))[0]

    def test_direct_support(self):
        self.assertEqual(self.classify("Apoyo total a doña Laura Fernández"), "direct_support")

    def test_sarcastic_support_is_ambiguous(self):
        self.assertEqual(
            self.classify("Me acaban de pagar para hablar bien del gobierno: viva Chaves"),
            "ambiguous_support",
        )

    def test_opposition_attack_is_not_automatically_support(self):
        self.assertEqual(
            self.classify("La plata alcanza cuando el PLN no se la roba"),
            "opposition_or_institution_attack",
        )

    def test_government_criticism(self):
        self.assertEqual(self.classify("Laura corrupta, fuera"), "possible_criticism")

    def test_support_with_opposition_attack_remains_support(self):
        self.assertEqual(
            self.classify("Viva Laura y fuera los corruptos del PLN"),
            "direct_support",
        )

    def test_coverage_prefers_exact_upload_date(self):
        message = Message("v1", 0, "a1", "Ana", "hola", "hola")
        row = {
            "video_id": "v1", "upload_date": "20260501",
            "published_at": "2026-06-01T00:00:00+00:00",
        }
        with tempfile.TemporaryDirectory() as folder:
            Path(folder, "catalog.jsonl").write_text(json.dumps(row) + "\n", encoding="utf-8")
            result = coverage(folder, [message])
        self.assertEqual(result["earliest_catalog_date"], "2026-05-01")


if __name__ == "__main__":
    unittest.main()
