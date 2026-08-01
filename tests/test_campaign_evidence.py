import unittest

from campaign_evidence import classify_text
from bot_detector import normalize_text


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


if __name__ == "__main__":
    unittest.main()
