import unittest

from bot_detector import Message, normalize_text
from campaign_evidence import classify_text
from deep_coordination_analysis import canonical_tokens, narrative_tags, primary_tag


class DeepCoordinationTests(unittest.TestCase):
    def message(self, text):
        return Message("s", 0, "A", "@a", text, normalize_text(text))

    def test_continuity_narrative_has_priority(self):
        message = self.message("Viva el gobierno de la continuidad")
        label, _ = classify_text(message.normalized)
        tags = narrative_tags(message, label)
        self.assertIn("continuidad", tags)
        self.assertIn("apoyo_gobierno", tags)
        self.assertEqual(primary_tag(tags), "continuidad")

    def test_canonical_tokens_merge_government_entities(self):
        tokens = canonical_tokens("viva laura y viva chaves")
        self.assertIn("gobierno", tokens)
        self.assertIn("apoyo", tokens)


if __name__ == "__main__":
    unittest.main()
