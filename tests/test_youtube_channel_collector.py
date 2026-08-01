import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from bot_detector import iter_messages
from youtube_channel_collector import (
    Inventory,
    canonical_chat_records,
    normalize_comment,
    parse_args,
    slugify,
    within_window,
    discover_channel_ytdlp,
    download_comments_ytdlp,
    hydrate_catalog_dates_ytdlp,
)


class ChannelCollectorTests(unittest.TestCase):
    def test_default_window_starts_may_first_2026(self):
        args = parse_args([])
        self.assertEqual(args.since, "2026-05-01")
        self.assertGreaterEqual(args.until, args.since)

    def test_window_is_inclusive_and_rejects_missing_date(self):
        from datetime import date
        since = date(2026, 5, 1)
        until = date(2026, 7, 31)
        self.assertTrue(within_window({"published_at": "2026-05-01T00:00:00Z"}, since, until))
        self.assertTrue(within_window({"upload_date": "20260731"}, since, until))
        self.assertFalse(within_window({"published_at": "2026-04-30T23:59:59Z"}, since, until))
        self.assertFalse(within_window({}, since, until))

    def test_chat_renderer_becomes_canonical(self):
        payload = {"replayChatItemAction": {"videoOffsetTimeMsec": "1250", "actions": [
            {"addChatItemAction": {"item": {"liveChatTextMessageRenderer": {
                "id": "m1", "authorExternalChannelId": "UC1",
                "authorName": {"simpleText": "@ana"},
                "message": {"runs": [{"text": "Hola"}]},
                "timestampUsec": "1780000000000000",
            }}}}
        ]}}
        rows = list(canonical_chat_records(payload, "vid"))
        self.assertEqual(rows[0]["record_type"], "chat_message")
        self.assertEqual(rows[0]["author_id"], "UC1")
        self.assertEqual(rows[0]["time_ms"], 1250)

    def test_canonical_chat_is_consumed_by_detector(self):
        row = {"record_type": "chat_message", "video_id": "video-123", "author_id": "UC1", "author_name": "@a", "text": "Hola", "time_ms": 500}
        with tempfile.TemporaryDirectory() as folder:
            session = Path(folder) / "sesion"
            session.mkdir()
            path = session / "chat.jsonl"
            path.write_text(json.dumps(row) + "\n", encoding="utf-8")
            messages = list(iter_messages([str(path)]))
        self.assertEqual(len(messages), 1)
        self.assertEqual(messages[0].normalized, "hola")
        self.assertEqual(messages[0].session, "video-123")

    def test_canonical_comment_is_consumed_without_false_second_precision(self):
        row = {
            "record_type": "comment", "video_id": "video-123", "author_id": "UC1",
            "author_name": "@a", "text": "Comentario", "published_at": "2026-07-30T00:00:00+00:00",
            "source": "yt_dlp",
        }
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "comments.jsonl"
            path.write_text(json.dumps(row) + "\n", encoding="utf-8")
            messages = list(iter_messages([str(path)]))
        self.assertEqual(len(messages), 1)
        self.assertEqual(messages[0].source, "comment")
        self.assertEqual(messages[0].session, "video-123__comments")
        self.assertEqual(messages[0].time_precision, "day")
        self.assertGreater(messages[0].time_ms, 0)

    def test_comment_normalization_keeps_reply_parent(self):
        raw = {"id": "r1", "snippet": {"parentId": "p1", "authorDisplayName": "Ana", "authorChannelId": {"value": "UC1"}, "textOriginal": "Respuesta", "publishedAt": "2026-06-01T00:00:00Z"}}
        row = normalize_comment(raw, "v1", "youtube_data_api")
        self.assertTrue(row["is_reply"])
        self.assertEqual(row["parent_id"], "p1")

    def test_inventory_adopts_and_skips_existing_file(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            inventory = Inventory(root / "inventory.json", root, "channel", "2026-05-01", "2026-07-31")
            entry = {"video_id": "v1", "title": "Titulo", "published_at": "2026-06-01T00:00:00Z"}
            inventory.ensure_video(entry)
            artifact = root / "comments" / "file.jsonl"
            artifact.parent.mkdir()
            artifact.write_text('{"x": 1}\n', encoding="utf-8")
            self.assertTrue(inventory.adopt("v1", "comments", artifact))
            self.assertFalse(inventory.should_process("v1", "comments"))
            inventory.save()
            loaded = Inventory(root / "inventory.json", root, "channel", "2026-05-01", "2026-07-31")
            self.assertFalse(loaded.should_process("v1", "comments"))

    def test_slug_is_stable_and_bounded(self):
        self.assertEqual(slugify("¡Hola, Costa Rica!"), "hola-costa-rica")
        self.assertLessEqual(len(slugify("x" * 200)), 80)

    @mock.patch("youtube_channel_collector.subprocess.run")
    def test_ytdlp_catalog_requests_approximate_dates(self, run):
        item = {"id": "v1", "title": "Video", "upload_date": "20260720", "timestamp": 1784505600}
        run.return_value = subprocess.CompletedProcess([], 0, json.dumps(item) + "\n", "")
        args = parse_args(["--catalog-limit", "1"])
        rows = discover_channel_ytdlp("https://www.youtube.com/@canal", "/bin/yt-dlp", args)
        commands = [call.args[0] for call in run.call_args_list]
        self.assertTrue(all("youtubetab:approximate_date" in command for command in commands))
        self.assertEqual(rows[0]["upload_date"], "20260720")

    @mock.patch("youtube_channel_collector.subprocess.run")
    def test_ytdlp_hydration_replaces_approximate_date(self, run):
        metadata = {
            "id": "v1", "title": "Video exacto", "upload_date": "20260408",
            "release_date": "20260408", "release_timestamp": 1775673145,
            "was_live": True, "live_status": "was_live",
        }
        run.return_value = subprocess.CompletedProcess([], 0, json.dumps(metadata), "")
        args = parse_args([])
        rows = hydrate_catalog_dates_ytdlp([{
            "video_id": "v1", "title": "aproximado", "url": "https://www.youtube.com/watch?v=v1",
            "upload_date": "20260501", "published_at": "2026-05-01T00:00:00+00:00",
            "date_precision": "approximate", "is_stream": True,
        }], "/bin/yt-dlp", args)
        self.assertEqual(rows[0]["upload_date"], "20260408")
        self.assertEqual(rows[0]["date_precision"], "exact")

    @mock.patch("youtube_channel_collector.time.sleep")
    @mock.patch("youtube_channel_collector.subprocess.run")
    def test_comments_rotate_client_after_reload_error(self, run, sleep):
        def fake_run(command, **kwargs):
            extractor = command[command.index("--extractor-args") + 1]
            if "player_client=android" in extractor:
                prefix = Path(command[command.index("-o") + 1])
                payload = {"comment_count": 1, "comments": [{
                    "id": "c1", "author": "Ana", "author_id": "UC1",
                    "text": "Hola", "timestamp": 1780000000, "parent": "root",
                }]}
                Path(str(prefix) + ".info.json").write_text(json.dumps(payload), encoding="utf-8")
                return subprocess.CompletedProcess(command, 0, "", "")
            return subprocess.CompletedProcess(command, 1, "", "The page needs to be reloaded")

        run.side_effect = fake_run
        args = parse_args([])
        entry = {"video_id": "v1", "url": "https://www.youtube.com/watch?v=v1"}
        with tempfile.TemporaryDirectory() as folder:
            destination = Path(folder) / "comments.jsonl"
            count = download_comments_ytdlp(entry, destination, "/bin/yt-dlp", args)
            rows = [json.loads(line) for line in destination.read_text(encoding="utf-8").splitlines()]
        self.assertEqual(count, 1)
        self.assertEqual(rows[0]["comment_id"], "c1")
        self.assertEqual(run.call_count, 2)


if __name__ == "__main__":
    unittest.main()
