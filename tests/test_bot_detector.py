import json
import os
import tempfile
import unittest

from bot_detector import Message, analyze, filter_messages_to_catalog, iter_messages, normalize_text, write_csv


class DetectorTests(unittest.TestCase):
    def test_normalize_text(self):
        self.assertEqual(normalize_text("  ¡HOLA,  Costa Rica! "), "hola costa rica")
        self.assertEqual(normalize_text("Visite https://example.com/a?q=1"), "visite <url>")

    def test_youtube_jsonl_parser(self):
        payload = {
            "replayChatItemAction": {
                "videoOffsetTimeMsec": "1234",
                "actions": [{"addChatItemAction": {"item": {"liveChatTextMessageRenderer": {
                    "authorExternalChannelId": "UC1",
                    "authorName": {"simpleText": "@ana"},
                    "message": {"runs": [{"text": "Hola "}, {"emoji": {"accessibility": {"accessibilityData": {"label": "🇨🇷"}}}}]},
                }}}}],
            }
        }
        with tempfile.TemporaryDirectory() as folder:
            session = os.path.join(folder, "sesion_1")
            os.mkdir(session)
            path = os.path.join(session, "chat.jsonl")
            with open(path, "w", encoding="utf-8") as handle:
                handle.write(json.dumps(payload) + "\n")
            messages = list(iter_messages([path]))
        self.assertEqual(len(messages), 1)
        self.assertEqual(messages[0].session, "sesion_1")
        self.assertEqual(messages[0].author_id, "UC1")
        self.assertEqual(messages[0].time_ms, 1234)
        self.assertEqual(messages[0].text, "Hola 🇨🇷")

    def test_synchronized_templates_create_coordination_edge(self):
        messages = []
        for session_index in range(1, 5):
            session = "s{}".format(session_index)
            for index in range(6):
                text_a = "Apoyamos esta propuesta nacional numero {} ahora".format(index)
                text_b = "Apoyamos esta propuesta nacional numero {} ahora mismo".format(index)
                messages.append(Message(session, index * 30000, "A", "@a", text_a, normalize_text(text_a)))
                messages.append(Message(session, index * 30000 + 2000, "B", "@b", text_b, normalize_text(text_b)))
                human = "Comentario humano distinto {} {}".format(session_index, index)
                messages.append(Message(session, index * 30000 + 15000, "H", "@h", human, normalize_text(human)))
        accounts, edges, clusters = analyze(messages, 10, 0.80)
        edge = next(item for item in edges if {item["left"], item["right"]} == {"A", "B"})
        self.assertGreaterEqual(edge["near_events"], 20)
        self.assertGreaterEqual(edge["sessions"], 4)
        scores = {item["author_id"]: item["signal_score"] for item in accounts}
        self.assertGreater(scores["A"], scores["H"])
        self.assertGreater(scores["B"], scores["H"])
        self.assertTrue(clusters)

    def test_csv_neutralizes_spreadsheet_formulas(self):
        with tempfile.TemporaryDirectory() as folder:
            path = os.path.join(folder, "out.csv")
            write_csv(path, [{"name": "@cuenta", "text": "=1+1"}], ["name", "text"])
            with open(path, "r", encoding="utf-8") as handle:
                contents = handle.read()
        self.assertIn("'@cuenta", contents)
        self.assertIn("'=1+1", contents)

    def test_catalog_filter_excludes_downloaded_out_of_window_video(self):
        with tempfile.TemporaryDirectory() as folder:
            catalog = os.path.join(folder, "catalog.jsonl")
            with open(catalog, "w", encoding="utf-8") as handle:
                handle.write(json.dumps({"video_id": "inside"}) + "\n")
            messages = [
                Message("inside", 0, "A", "@a", "x", "x"),
                Message("outside__comments", 0, "B", "@b", "y", "y", "comment", "day"),
            ]
            filtered, excluded = filter_messages_to_catalog(messages, folder)
        self.assertEqual([message.session for message in filtered], ["inside"])
        self.assertEqual(excluded, 1)


if __name__ == "__main__":
    unittest.main()
