import unittest

from bot_detector import Message, normalize_text
from text_content_analysis import classify_content, classify_stance, slogan_key


class TextContentAnalysisTests(unittest.TestCase):
    def message(self, text):
        return Message("v1", 0, "a1", "@ana", text, normalize_text(text))

    def test_pro_government_stance(self):
        self.assertEqual(classify_stance(normalize_text("Viva Laura Fernández, excelente presidenta")), "pro_gobierno")

    def test_anti_government_stance(self):
        self.assertEqual(classify_stance(normalize_text("El gobierno de Chaves es corrupto")), "contra_gobierno")

    def test_support_verb_inside_criticism_is_not_pro_government(self):
        text = normalize_text("Usted es un idiota que apoya las estupideces del oficialismo")
        self.assertEqual(classify_stance(text), "contra_gobierno")

    def test_unrelated_lauras_are_not_treated_as_current_government(self):
        text = normalize_text("Laura Chinchilla viajó en una avioneta de narcos")
        self.assertEqual(classify_stance(text), "sin_postura_clara")

    def test_negated_support_is_anti_government(self):
        text = normalize_text("No apoyamos a este gobierno")
        self.assertEqual(classify_stance(text), "contra_gobierno")

    def test_fuera_del_gobierno_is_not_a_demand_to_remove_it(self):
        text = normalize_text("El PLN tiene 16 años fuera del gobierno")
        self.assertEqual(classify_stance(text), "sin_postura_clara")

    def test_hypothetical_president_is_not_current_government(self):
        text = normalize_text("Ahorraría tiempo si fuera presidente")
        self.assertEqual(classify_stance(text), "sin_postura_clara")

    def test_foreign_president_is_not_current_government(self):
        text = normalize_text("Estoy con el presidente panameño, excelente")
        self.assertEqual(classify_stance(text), "sin_postura_clara")

    def test_assembly_president_is_not_executive_support(self):
        text = normalize_text("Todos apoyamos a la señora presidenta de la Asamblea")
        self.assertNotEqual(classify_stance(text), "pro_gobierno")

    def test_conditional_support_is_not_authors_stance(self):
        text = normalize_text("Aunque apoyen al gobierno o a la oposición, trabajen")
        self.assertEqual(classify_stance(text), "sin_postura_clara")

    def test_reported_subjunctive_fuera_is_not_removal_demand(self):
        text = normalize_text("Yo no dije que fuera Chaves")
        self.assertEqual(classify_stance(text), "sin_postura_clara")

    def test_attack_links_insult_to_named_target(self):
        result = classify_content(self.message("Villalta es un idiota y un corrupto"))
        self.assertIn("jose_maria_villalta", result["attacked_targets"])
        self.assertIn("insulto_personal", result["insult_categories"])

    def test_policy_criticism_is_not_automatically_an_insult(self):
        result = classify_content(self.message("No al proyecto 23.414, el ICE se defiende"))
        self.assertIn("proyecto_23414", result["themes"])
        self.assertFalse(result["insult_categories"])

    def test_attack_is_assigned_to_nearest_target(self):
        result = classify_content(self.message("Excelente Villalta; los chavelos son idiotas"))
        self.assertIn("ppso", result["attacked_targets"])
        self.assertNotIn("jose_maria_villalta", result["attacked_targets"])

    def test_slogan_normalizes_handles_and_repeated_letters(self):
        self.assertEqual(slogan_key("@ana VIVAAA el ICE!!!"), slogan_key("VIVAA el ICE"))


if __name__ == "__main__":
    unittest.main()
