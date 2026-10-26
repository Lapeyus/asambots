"""Pruebas de lógica pura complementarias (sin red, sin claves, sin servicios externos).

Cubren reglas de puntuación, utilidades estadísticas, contratos de CLI locales y el
inventario del colector. Todos los datos son sintéticos.
"""
import csv
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import advanced_analysis
import bot_detector
import campaign_evidence
import deep_coordination_analysis as deep
import text_content_analysis
import youtube_channel_collector as collector
from bot_detector import Message, normalize_text


def msg(author, text, session="s1", time_ms=0, source="chat", precision="exact"):
    return Message(
        session=session, time_ms=time_ms, author_id=author, author_name="@" + author,
        text=text, normalized=normalize_text(text), source=source, time_precision=precision,
    )


class BotDetectorPureTests(unittest.TestCase):
    def test_risk_label_thresholds(self):
        self.assertEqual(bot_detector.risk_label(99, 4), "evidencia_insuficiente")
        self.assertEqual(bot_detector.risk_label(70, 5), "muy_alta_revision")
        self.assertEqual(bot_detector.risk_label(50, 5), "alta_revision")
        self.assertEqual(bot_detector.risk_label(25, 5), "revision")
        self.assertEqual(bot_detector.risk_label(24.99, 5), "baja")

    def test_percentile_ranks(self):
        ranks = bot_detector.percentile_ranks({"a": 1, "b": 2, "c": 3})
        self.assertEqual(ranks, {"a": 0.0, "b": 0.5, "c": 1.0})
        self.assertEqual(bot_detector.percentile_ranks({"solo": 7}), {"solo": 0.0})

    def test_exact_match_requires_length_and_tokens(self):
        self.assertFalse(bot_detector.qualifies_exact("hola todos"))  # menos de 12 caracteres
        self.assertTrue(bot_detector.qualifies_exact("hola a todos"))  # límite exacto: 12 y 3 tokens
        self.assertTrue(bot_detector.qualifies_exact("buenas noches costa rica"))
        self.assertFalse(bot_detector.qualifies_exact("jajajajajajajaja"))

    def test_informative_tokens_drop_stopwords_and_short_words(self):
        tokens = bot_detector.informative_tokens(normalize_text("El pueblo y la patria https://x.y"))
        self.assertEqual(tokens, {"pueblo", "patria", "<url>"})

    def test_parse_iso_time_ms(self):
        self.assertEqual(bot_detector.parse_iso_time_ms("1970-01-01T00:00:01Z"), 1000)
        self.assertEqual(bot_detector.parse_iso_time_ms("no es fecha"), 0)
        self.assertEqual(bot_detector.parse_iso_time_ms(None), 0)

    def test_build_clusters_uses_only_strong_edges(self):
        edges = [
            {"left": "a", "right": "b", "coordination_score": 60.0, "evidence_events": 3},
            {"left": "b", "right": "c", "coordination_score": 40.0, "evidence_events": 2},
            {"left": "x", "right": "y", "coordination_score": 34.9, "evidence_events": 9},
        ]
        clusters = bot_detector.build_clusters(edges)
        self.assertEqual(len(clusters), 1)
        self.assertEqual(clusters[0]["members"], 3)
        self.assertEqual(clusters[0]["member_ids"], "a | b | c")
        self.assertEqual(clusters[0]["max_edge_score"], 60.0)
        self.assertEqual(clusters[0]["cluster_id"], 1)

    def test_cli_analyze_writes_all_outputs(self):
        lines = []
        for index in range(12):
            for author in ("UC_A", "UC_B"):
                lines.append({
                    "record_type": "chat_message", "video_id": "vid{}".format(index % 3),
                    "time_ms": index * 60000 + (500 if author == "UC_B" else 0),
                    "author_id": author, "author_name": author,
                    "text": "mensaje plantilla distintivo numero {} para revisar".format(index),
                })
        with tempfile.TemporaryDirectory() as folder:
            source = os.path.join(folder, "entrada.jsonl")
            with open(source, "w", encoding="utf-8") as handle:
                for line in lines:
                    handle.write(json.dumps(line) + "\n")
            output = os.path.join(folder, "salida")
            with mock.patch("builtins.print"):
                code = bot_detector.main(["analyze", source, "--output", output])
            self.assertEqual(code, 0)
            for name in ("report.html", "accounts.csv", "coordination.csv", "clusters.csv", "summary.json"):
                self.assertTrue(os.path.isfile(os.path.join(output, name)), name)
            with open(os.path.join(output, "summary.json"), encoding="utf-8") as handle:
                summary = json.load(handle)
            self.assertEqual(summary["counts"]["messages"], 24)
            self.assertEqual(summary["counts"]["accounts"], 2)
            self.assertGreaterEqual(summary["counts"]["coordination_edges"], 1)

    def test_cli_rejects_empty_input_and_bad_threshold(self):
        with tempfile.TemporaryDirectory() as folder, mock.patch("sys.stderr"):
            self.assertEqual(bot_detector.main(["analyze", folder]), 2)
            path = os.path.join(folder, "x.jsonl")
            Path(path).write_text("{}\n", encoding="utf-8")
            self.assertEqual(bot_detector.main(["analyze", path, "--near-threshold", "0.3"]), 2)
            self.assertEqual(bot_detector.main(["analyze", path]), 2)  # sin mensajes compatibles


class AdvancedPureTests(unittest.TestCase):
    def test_fisher_combined_p(self):
        self.assertEqual(advanced_analysis.fisher_combined_p([]), 1.0)
        self.assertAlmostEqual(advanced_analysis.fisher_combined_p([0.2]), 0.2)
        self.assertLess(advanced_analysis.fisher_combined_p([0.01, 0.01]), 0.01)
        self.assertEqual(advanced_analysis.fisher_combined_p([1.0, 1.0]), 1.0)

    def test_log_interval_bin(self):
        self.assertEqual(advanced_analysis.log_interval_bin(0.5), 0)
        self.assertEqual(advanced_analysis.log_interval_bin(1), 1)
        self.assertEqual(advanced_analysis.log_interval_bin(3), 2)
        self.assertEqual(advanced_analysis.log_interval_bin(8), 4)

    def test_shared_scripts_need_long_text_and_two_authors(self):
        long_text = "este es un texto largo compartido por dos cuentas distintas"
        messages = [
            msg("a", long_text), msg("b", long_text, session="s2"), msg("c", "texto corto"),
            msg("c", "texto corto"), msg("d", "otro mensaje cualquiera sin relacion alguna aqui"),
        ]
        rows = advanced_analysis.shared_script_edges(messages)
        self.assertEqual([(row["left"], row["right"]) for row in rows], [("a", "b")])
        self.assertEqual(rows[0]["template_sessions"], 2)
        self.assertGreater(rows[0]["script_score"], 0)

    def test_network_components_ignore_single_view_edges(self):
        edges = [
            {"left": "a", "right": "b", "evidence_views": 2, "multiview_score": 50.0},
            {"left": "b", "right": "c", "evidence_views": 1, "multiview_score": 90.0},
            {"left": "d", "right": "e", "evidence_views": 2, "multiview_score": 10.0},
        ]
        rows = advanced_analysis.network_components(edges)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["member_ids"], "a | b")
        self.assertEqual(rows[0]["density"], 1.0)

    def test_strong_multiview_requires_recurrent_temporal_overlap(self):
        base = [{"left": "a", "right": "b", "coordination_score": 60.0, "evidence_events": 3, "examples": "x"}]
        temporal = [{
            "left": "a", "right": "b", "temporal_score": 60.0, "q_value": 0.01,
            "synchrony_lift": 3.0, "shared_active_bins": 5, "overlap_sessions": 1,
        }]
        one_session = advanced_analysis.multiview_edges(base, temporal, [])
        self.assertEqual(one_session[0]["review_level"], "multivista_una_sesion")
        temporal[0]["overlap_sessions"] = 2
        recurrent = advanced_analysis.multiview_edges(base, temporal, [])
        self.assertEqual(recurrent[0]["review_level"], "multivista_fuerte")
        self.assertEqual(recurrent[0]["evidence_views"], 2)


class CampaignPureTests(unittest.TestCase):
    def test_concentration(self):
        messages = [msg("a", "x"), msg("a", "y"), msg("b", "z"), msg("c", "w")]
        result = campaign_evidence.concentration(messages)
        self.assertEqual(result["top_1_share"], 0.5)
        self.assertEqual(result["top_10_share"], 1.0)
        self.assertEqual(result["hhi"], 0.375)
        self.assertEqual(campaign_evidence.concentration([])["hhi"], 0)

    def test_overrides_are_normalized_and_validated(self):
        with tempfile.TemporaryDirectory() as folder:
            path = os.path.join(folder, "etiquetas.csv")
            with open(path, "w", encoding="utf-8", newline="") as handle:
                writer = csv.writer(handle)
                writer.writerow(["normalized_text", "label"])
                writer.writerow(["  ¡Texto DE prueba!  ", "other"])
                writer.writerow(["etiqueta invalida", "no_existe"])
            overrides = campaign_evidence.load_overrides(path)
        self.assertEqual(overrides, {"texto de prueba": "other"})
        self.assertEqual(campaign_evidence.load_overrides(None), {})

    def test_override_replaces_rule_label(self):
        message = msg("a", "Viva el gobierno, excelente trabajo")
        labels, rules = campaign_evidence.classify_messages([message], {})
        self.assertEqual(labels[id(message)], "direct_support")
        labels, rules = campaign_evidence.classify_messages([message], {message.normalized: "other"})
        self.assertEqual(labels[id(message)], "other")
        self.assertEqual(rules[id(message)], "revision_humana")


class DeepPureTests(unittest.TestCase):
    def test_minhash_is_deterministic_and_seeded(self):
        tokens = {"uno", "dos", "tres"}
        self.assertEqual(deep.minhash(tokens), deep.minhash(set(tokens)))
        self.assertEqual(len(deep.minhash(tokens)), 24)
        self.assertNotEqual(deep.stable_hash("x", 0), deep.stable_hash("x", 1))

    def test_union_find(self):
        uf = deep.UnionFind(["a", "b", "c", "d"])
        uf.union("a", "b")
        uf.union("c", "b")
        self.assertEqual(uf.find("a"), uf.find("c"))
        self.assertNotEqual(uf.find("a"), uf.find("d"))

    def test_high_confidence_rule(self):
        def candidate():
            return {
                "left": "a", "right": "b", "lexical_clusters": 2, "lexical_sessions": 3,
                "sequence_events": 0, "sequence_sessions": 0, "shared_resources": 0,
                "resource_sessions": 0, "reply_interactions": 0, "reply_videos": 0,
                "candidate_priority": 1.0,
            }
        timing = {"left": "a", "right": "b", "q_value": 0.01, "permutation_lift": 2.0, "overlap_sessions": 2}
        rows, strong = deep.integrate_temporal([candidate()], [timing])
        self.assertEqual(rows[0]["evidence_views"], 2)
        self.assertEqual(rows[0]["high_confidence"], 1)
        self.assertEqual(len(strong), 1)
        rows, strong = deep.integrate_temporal([candidate()], [dict(timing, q_value=0.2)])
        self.assertEqual(rows[0]["high_confidence"], 0)
        self.assertEqual(strong, [])


class TextContentPureTests(unittest.TestCase):
    def test_span_distance(self):
        self.assertEqual(text_content_analysis.span_distance((0, 5), (10, 12)), 5)
        self.assertEqual(text_content_analysis.span_distance((10, 12), (0, 5)), 5)
        self.assertEqual(text_content_analysis.span_distance((0, 5), (3, 8)), 0)

    def test_slogan_candidate_requires_length_and_words(self):
        self.assertFalse(text_content_analysis.is_slogan_candidate("hola"))
        self.assertTrue(text_content_analysis.is_slogan_candidate("el pueblo unido jamas sera vencido"))


class CollectorPureTests(unittest.TestCase):
    def test_select_backend(self):
        self.assertEqual(collector.select_backend("auto", "clave"), "api")
        self.assertEqual(collector.select_backend("auto", None), "ytdlp")
        self.assertEqual(collector.select_backend("ytdlp", "clave"), "ytdlp")

    def test_handle_from_channel(self):
        self.assertEqual(collector.handle_from_channel("https://www.youtube.com/@Canal/videos"), "Canal")
        with self.assertRaises(ValueError):
            collector.handle_from_channel("https://www.youtube.com/channel/UC123")

    def test_artifact_filename(self):
        entry = {"video_id": "abcdefghijk", "title": "Conferencia de prensa!", "upload_date": "20260502"}
        self.assertEqual(
            collector.artifact_filename(entry, "chat"),
            "2026-05-02__conferencia-de-prensa__abcdefghijk__chat.jsonl",
        )
        self.assertTrue(collector.artifact_filename({"video_id": "x"}, "comments").startswith("fecha-desconocida__"))

    def test_inventory_detects_modified_artifact_and_respects_unavailable(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            inventory = collector.Inventory(root / "inventory.json", root, "canal", "2026-05-01", "2026-05-31")
            inventory.ensure_video({"video_id": "v1"})
            artifact = root / "chats" / "v1.jsonl"
            artifact.parent.mkdir()
            artifact.write_text('{"a": 1}\n', encoding="utf-8")
            inventory.complete("v1", "chat", artifact, 1, "prueba")
            self.assertFalse(inventory.should_process("v1", "chat"))
            artifact.write_text('{"a": 2}\n', encoding="utf-8")  # mismo tamaño, otro hash
            self.assertTrue(inventory.should_process("v1", "chat"))
            self.assertEqual(inventory.data["videos"]["v1"]["chat"]["status"], "changed")

            inventory.mark("v1", "comments", "unavailable", "comentarios deshabilitados")
            self.assertFalse(inventory.should_process("v1", "comments"))
            self.assertTrue(inventory.should_process("v1", "comments", retry_unavailable=True))
            inventory.save()
            saved = json.loads((root / "inventory.json").read_text(encoding="utf-8"))
            self.assertEqual(saved["window"], {"since": "2026-05-01", "until": "2026-05-31"})
            self.assertIn("counts", saved)

    def test_scraping_requires_explicit_opt_in(self):
        with tempfile.TemporaryDirectory() as folder:
            env = {key: value for key, value in os.environ.items()
                   if key not in {"YOUTUBE_RASPADO", "YOUTUBE_API_KEY"}}
            with mock.patch.dict(os.environ, env, clear=True), mock.patch("builtins.print"):
                code = collector.main(["--output-dir", folder, "--mode", "catalog"])
            self.assertEqual(code, 2)
            self.assertFalse((Path(folder) / "catalog.jsonl").exists())


if __name__ == "__main__":
    unittest.main()
