import unittest

from advanced_analysis import (
    account_fingerprints,
    benjamini_hochberg,
    hypergeom_survival,
    multiview_edges,
    sensitivity_analysis,
    shannon_entropy_normalized,
    temporal_synchrony,
)
from bot_detector import Message, normalize_text


def message(session, seconds, author, text):
    return Message(session, seconds * 1000, author, "@" + author, text, normalize_text(text))


class AdvancedAnalysisTests(unittest.TestCase):
    def test_entropy_extremes(self):
        self.assertEqual(shannon_entropy_normalized(["x"] * 10), 0.0)
        self.assertAlmostEqual(shannon_entropy_normalized(list(range(10))), 1.0)

    def test_hypergeometric_known_case(self):
        # P(X>=2), N=10, K=3, n=4 = (C3,2*C7,2 + C3,3*C7,1)/C10,4
        expected = (3 * 21 + 1 * 7) / 210.0
        self.assertAlmostEqual(hypergeom_survival(2, 10, 3, 4), expected)

    def test_bh_is_monotone_in_p_order(self):
        p_values = [0.01, 0.04, 0.03, 0.20]
        q_values = benjamini_hochberg(p_values)
        ordered = sorted(zip(p_values, q_values))
        self.assertEqual([q for _, q in ordered], sorted(q for _, q in ordered))
        self.assertTrue(all(p <= q <= 1 for p, q in zip(p_values, q_values)))

    def test_synchronized_accounts_rank_above_unsynchronized(self):
        messages = []
        for session_index in range(4):
            session = "s{}".format(session_index)
            for index in range(12):
                base = index * 60
                messages.append(message(session, base, "A", "plantilla a {}".format(index)))
                messages.append(message(session, base + 1, "B", "plantilla b {}".format(index)))
                messages.append(message(session, base + 30 + (index % 3) * 5, "H", "humano {}".format(index)))
        rows, metadata = temporal_synchrony(messages, bin_seconds=10, min_account_messages=10)
        by_pair = {(row["left"], row["right"]): row for row in rows}
        self.assertEqual(metadata["tested_pairs"], 3)
        self.assertGreater(by_pair[("A", "B")]["temporal_score"], by_pair[("A", "H")]["temporal_score"])

    def test_fingerprints_capture_repetition(self):
        repeated = [message("s", i * 5, "R", "mismo mensaje siempre") for i in range(20)]
        varied = [message("s", i * 7, "V", "mensaje distinto numero {}".format(i)) for i in range(20)]
        rows = {row["author_id"]: row for row in account_fingerprints(repeated + varied)}
        self.assertLess(rows["R"]["text_entropy"], rows["V"]["text_entropy"])
        self.assertLess(rows["R"]["compression_ratio"], rows["V"]["compression_ratio"])

    def test_day_precision_comments_do_not_create_temporal_synchrony(self):
        messages = [
            Message("v__comments", 1000, author, "@" + author, "texto", "texto", "comment", "day")
            for author in ("A", "B") for _ in range(12)
        ]
        rows, metadata = temporal_synchrony(messages, bin_seconds=30, min_account_messages=10)
        self.assertEqual(rows, [])
        self.assertEqual(metadata["eligible_accounts"], 0)

    def test_fingerprints_report_modalities(self):
        messages = [
            message("s", 1, "A", "chat"),
            Message("v__comments", 0, "A", "@A", "comentario", "comentario", "comment", "day"),
        ]
        row = account_fingerprints(messages)[0]
        self.assertEqual(row["chat_messages"], 1)
        self.assertEqual(row["comment_messages"], 1)

    def test_multiview_requires_two_material_views(self):
        base = [{
            "left": "A", "right": "B", "coordination_score": 40,
            "evidence_events": 3, "examples": "x",
        }]
        temporal = [{
            "left": "A", "right": "B", "shared_active_bins": 8,
            "temporal_score": 50, "q_value": 0.01, "synchrony_lift": 2.5,
            "overlap_sessions": 2,
        }]
        rows = multiview_edges(base, temporal, [])
        self.assertEqual(rows[0]["evidence_views"], 2)
        self.assertIn(rows[0]["review_level"], {"multivista_una_sesion", "multivista_fuerte"})

    def test_sensitivity_reuses_and_compares_bins(self):
        messages = []
        for session_index in range(2):
            for index in range(10):
                messages.append(message("s{}".format(session_index), index * 30, "A", "texto a {}".format(index)))
                messages.append(message("s{}".format(session_index), index * 30 + 1, "B", "texto b {}".format(index)))
        temporal, _ = temporal_synchrony(messages, 10, 10)
        summaries, stability = sensitivity_analysis(messages, [10, 20], 10, 0.05, [], [], 10, temporal)
        self.assertEqual([row["bin_seconds"] for row in summaries], [10, 20])
        self.assertTrue(all(row["tested_pairs"] == 1 for row in summaries))
        self.assertIsInstance(stability, list)


if __name__ == "__main__":
    unittest.main()
