#!/usr/bin/env python3
"""Cosecha incremental de chats y comentarios de un canal de YouTube.

Configuracion predeterminada:
  canal: https://www.youtube.com/@PresidenciadelaRepublicaCR
  ventana: 2026-05-01 hasta la fecha de ejecucion (inclusive)

Los comentarios usan la API oficial si existe ``YOUTUBE_API_KEY``. El catalogo
tambien usa esa API cuando es posible. La repeticion del chat requiere yt-dlp
y por ello conserva la puerta consciente del script original:

    export YOUTUBE_RASPADO=1

Cada artefacto se registra en ``inventory.json`` con estado, ruta, conteo,
tamanio y SHA-256. Una corrida posterior valida el inventario y no repite una
descarga completa ni una ausencia confirmada.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Dict, Iterable, Iterator, List, Optional, Sequence, Tuple

from bot_detector import extract_run_text


VERSION = "1.1.0"
DEFAULT_CHANNEL = "https://www.youtube.com/@PresidenciadelaRepublicaCR"
DEFAULT_SINCE = "2026-05-01"
LEGACY_CHAT_RE = re.compile(r"__([A-Za-z0-9_-]{11})__chat\.jsonl$")
API_BASE = "https://www.googleapis.com/youtube/v3"
UNAVAILABLE_MARKERS = (
    "comments are disabled",
    "commentsdisabled",
    "chat is disabled",
    "chat replay is disabled",
    "does not have a chat replay",
    "live chat replay is not available",
    "there are no subtitles for the requested languages",
    "subtitles for the requested languages are not available",
)


class UnavailableError(RuntimeError):
    """El recurso no existe o esta deshabilitado de forma confirmada."""


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def count_lines(path: Path) -> int:
    with path.open("rb") as handle:
        return sum(1 for _ in handle)


def atomic_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=path.name + ".", suffix=".part", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
        os.replace(temporary, path)
    except Exception:
        try:
            os.unlink(temporary)
        except OSError:
            pass
        raise


def atomic_jsonl(path: Path, records: Iterable[dict]) -> int:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=path.name + ".", suffix=".part", dir=str(path.parent))
    count = 0
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            for record in records:
                handle.write(json.dumps(record, ensure_ascii=False) + "\n")
                count += 1
        os.replace(temporary, path)
        return count
    except Exception:
        try:
            os.unlink(temporary)
        except OSError:
            pass
        raise


def slugify(value: str, limit: int = 80) -> str:
    value = value.casefold().strip()
    value = re.sub(r"[^\w]+", "-", value, flags=re.UNICODE).strip("-")
    return (value[:limit].rstrip("-") or "video")


def parse_day(value: str) -> date:
    return date.fromisoformat(value)


def entry_day(entry: dict) -> Optional[date]:
    # yt-dlp puede conservar un ``published_at`` aproximado (por ejemplo,
    # 2026-05-01) aunque la hidratación ya haya obtenido ``upload_date`` exacto
    # (por ejemplo, 2026-04-23). La fecha de subida debe tener precedencia.
    value = entry.get("upload_date") or entry.get("published_at")
    if not value:
        return None
    try:
        if len(value) == 8 and value.isdigit():
            return datetime.strptime(value, "%Y%m%d").date()
        return datetime.fromisoformat(value.replace("Z", "+00:00")).date()
    except (ValueError, TypeError):
        return None


def within_window(entry: dict, since: date, until: date) -> bool:
    published = entry_day(entry)
    return published is not None and since <= published <= until


def artifact_filename(entry: dict, kind: str) -> str:
    published = entry_day(entry)
    day = published.isoformat() if published else "fecha-desconocida"
    return "{}__{}__{}__{}.jsonl".format(
        day, slugify(entry.get("title", "video")), entry["video_id"], kind
    )


class Inventory:
    def __init__(self, path: Path, root: Path, channel: str, since: str, until: str):
        self.path = path
        self.root = root
        if path.exists():
            try:
                self.data = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                raise RuntimeError("Inventario invalido: {}".format(path))
        else:
            self.data = {"version": VERSION, "videos": {}}
        self.data.update({
            "version": VERSION,
            "channel": channel,
            "window": {"since": since, "until": until},
        })
        self.data.setdefault("videos", {})

    def ensure_video(self, entry: dict) -> dict:
        video = self.data["videos"].setdefault(entry["video_id"], {})
        video["metadata"] = dict(entry)
        video["active_in_catalog"] = True
        video.setdefault("chat", {"status": "pending", "attempts": 0})
        video.setdefault("comments", {"status": "pending", "attempts": 0})
        return video

    def set_active_video_ids(self, video_ids: Iterable[str]) -> None:
        active = set(video_ids)
        for video_id, video in self.data["videos"].items():
            video["active_in_catalog"] = video_id in active

    def artifact_path(self, video_id: str, kind: str) -> Optional[Path]:
        state = self.data["videos"].get(video_id, {}).get(kind, {})
        relative = state.get("path")
        return self.root / relative if relative else None

    def valid_complete(self, video_id: str, kind: str) -> bool:
        state = self.data["videos"].get(video_id, {}).get(kind, {})
        if state.get("status") != "complete":
            return False
        path = self.artifact_path(video_id, kind)
        if path is None or not path.is_file():
            state["status"] = "missing"
            return False
        if path.stat().st_size != state.get("bytes"):
            state["status"] = "changed"
            return False
        if state.get("sha256") and sha256_file(path) != state["sha256"]:
            state["status"] = "changed"
            return False
        return True

    def adopt(self, video_id: str, kind: str, path: Path, backend: str = "existing") -> bool:
        if not path.exists():
            return False
        self.complete(video_id, kind, path, count_lines(path), backend)
        return True

    def should_process(self, video_id: str, kind: str, retry_unavailable: bool = False) -> bool:
        state = self.data["videos"][video_id][kind]
        if self.valid_complete(video_id, kind):
            return False
        if state.get("status") in {"unavailable", "not_applicable"} and not retry_unavailable:
            return False
        return True

    def attempt(self, video_id: str, kind: str, backend: str) -> None:
        state = self.data["videos"][video_id][kind]
        state["attempts"] = int(state.get("attempts", 0)) + 1
        state.update({"status": "downloading", "backend": backend, "last_attempt_at": utc_now()})
        state.pop("last_error", None)

    def complete(self, video_id: str, kind: str, path: Path, records: int, backend: str) -> None:
        relative = str(path.relative_to(self.root))
        state = self.data["videos"][video_id][kind]
        state.update({
            "status": "complete", "backend": backend, "path": relative,
            "records": records, "bytes": path.stat().st_size,
            "sha256": sha256_file(path), "completed_at": utc_now(),
        })
        state.pop("last_error", None)

    def mark(self, video_id: str, kind: str, status: str, reason: str) -> None:
        state = self.data["videos"][video_id][kind]
        compact_reason = reason if len(reason) <= 1000 else reason[:450] + "\n...\n" + reason[-500:]
        state.update({"status": status, "last_error": compact_reason, "updated_at": utc_now()})

    def save(self) -> None:
        self.data["updated_at"] = utc_now()
        self.data["counts"] = dict(Counter(
            state.get("status", "unknown")
            for video in self.data["videos"].values() if video.get("active_in_catalog", True)
            for kind, state in video.items()
            if kind in {"chat", "comments"}
        ))
        atomic_json(self.path, self.data)


def api_get(resource: str, params: dict, api_key: str) -> dict:
    query = dict(params)
    query["key"] = api_key
    url = API_BASE + "/" + resource + "?" + urllib.parse.urlencode(query)
    request = urllib.request.Request(url, headers={"User-Agent": "asambots-collector/{}".format(VERSION)})
    try:
        with urllib.request.urlopen(request, timeout=45) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", "replace")
        try:
            payload = json.loads(body)
            errors = payload.get("error", {}).get("errors", [])
            reason = errors[0].get("reason", "") if errors else ""
            message = payload.get("error", {}).get("message", body)
        except ValueError:
            reason, message = "", body
        if reason == "commentsDisabled":
            raise UnavailableError("commentsDisabled")
        raise RuntimeError("YouTube API {} {}: {}".format(exc.code, reason, message))


def handle_from_channel(channel_url: str) -> str:
    match = re.search(r"/@([^/?#]+)", channel_url)
    if not match:
        raise ValueError("La URL debe contener un handle de YouTube: /@nombre")
    return match.group(1)


def discover_channel_api(channel_url: str, api_key: str) -> List[dict]:
    channel_payload = api_get("channels", {
        "part": "snippet,contentDetails", "forHandle": handle_from_channel(channel_url),
    }, api_key)
    items = channel_payload.get("items", [])
    if not items:
        raise RuntimeError("La API no encontro el canal {}".format(channel_url))
    channel = items[0]
    channel_id = channel["id"]
    uploads = channel["contentDetails"]["relatedPlaylists"]["uploads"]
    playlist_items = []
    token = None
    while True:
        params = {"part": "snippet,contentDetails,status", "playlistId": uploads, "maxResults": 50}
        if token:
            params["pageToken"] = token
        payload = api_get("playlistItems", params, api_key)
        playlist_items.extend(payload.get("items", []))
        token = payload.get("nextPageToken")
        if not token:
            break

    basic = {}
    for item in playlist_items:
        snippet = item.get("snippet", {})
        video_id = item.get("contentDetails", {}).get("videoId") or snippet.get("resourceId", {}).get("videoId")
        if not video_id:
            continue
        basic[video_id] = {
            "video_id": video_id,
            "title": snippet.get("title", ""),
            "description": snippet.get("description", ""),
            "published_at": item.get("contentDetails", {}).get("videoPublishedAt") or snippet.get("publishedAt"),
            "channel_id": channel_id,
            "channel_title": channel.get("snippet", {}).get("title", ""),
            "url": "https://www.youtube.com/watch?v=" + video_id,
            "catalog_backend": "youtube_data_api",
        }
    ids = list(basic)
    for start in range(0, len(ids), 50):
        payload = api_get("videos", {
            "part": "snippet,contentDetails,liveStreamingDetails,status,statistics",
            "id": ",".join(ids[start:start + 50]),
        }, api_key)
        for item in payload.get("items", []):
            entry = basic[item["id"]]
            live = item.get("liveStreamingDetails")
            if live:
                if live.get("actualEndTime"):
                    live_status = "was_live"
                elif live.get("actualStartTime"):
                    live_status = "is_live"
                else:
                    live_status = "is_upcoming"
            else:
                live_status = "not_live"
            entry.update({
                "is_stream": bool(live),
                "live_status": live_status,
                "duration": item.get("contentDetails", {}).get("duration"),
                "comment_count": int(item.get("statistics", {}).get("commentCount", 0)),
                "privacy_status": item.get("status", {}).get("privacyStatus"),
            })
    return list(basic.values())


def resolve_ytdlp() -> Optional[str]:
    configured = os.environ.get("YTDLP_BIN")
    candidates = [configured, shutil.which("yt-dlp"), str(Path(sys.executable).parent / "yt-dlp")]
    return next((candidate for candidate in candidates if candidate and os.path.isfile(candidate)), None)


def cookie_args(args: argparse.Namespace) -> List[str]:
    if args.cookies:
        return ["--cookies", args.cookies]
    if args.cookies_from_browser:
        return ["--cookies-from-browser", args.cookies_from_browser]
    return []


def discover_channel_ytdlp(channel_url: str, ytdlp: str, args: argparse.Namespace) -> List[dict]:
    merged: Dict[str, dict] = {}
    for tab in ("videos", "streams"):
        command = [
            ytdlp, "--flat-playlist", "--dump-json", "--quiet", "--no-warnings",
            # Las entradas planas de un canal suelen traer timestamp=null. Este
            # argumento oficial extrae upload_date/timestamp aproximados desde
            # el texto de fecha de la pestana sin hidratar cada video.
            "--extractor-args", "youtubetab:approximate_date",
        ]
        if args.catalog_limit:
            command += ["--playlist-end", str(args.catalog_limit)]
        command += cookie_args(args) + [channel_url.rstrip("/") + "/" + tab]
        result = subprocess.run(command, capture_output=True, text=True)
        if result.returncode and not result.stdout.strip():
            raise RuntimeError("yt-dlp catalogo /{}: {}".format(tab, result.stderr.strip()[-1000:]))
        for line in result.stdout.splitlines():
            try:
                item = json.loads(line)
            except ValueError:
                continue
            video_id = item.get("id")
            if not video_id:
                continue
            timestamp = item.get("release_timestamp") or item.get("timestamp")
            published_at = (
                datetime.fromtimestamp(timestamp, timezone.utc).isoformat() if timestamp else None
            )
            live_status = item.get("live_status") or "not_live"
            if tab == "streams" and live_status not in {"is_live", "is_upcoming"}:
                live_status = "was_live"
            entry = merged.setdefault(video_id, {
                "video_id": video_id,
                "title": item.get("title", ""),
                "published_at": published_at,
                "upload_date": item.get("upload_date"),
                "channel_id": item.get("channel_id"),
                "channel_title": item.get("channel"),
                "url": "https://www.youtube.com/watch?v=" + video_id,
                "catalog_backend": "yt_dlp",
                "date_precision": "approximate",
                "is_stream": False,
                "live_status": live_status,
            })
            if tab == "streams":
                entry["is_stream"] = True
                entry["live_status"] = live_status
    return list(merged.values())


def hydrate_catalog_dates_ytdlp(entries: Sequence[dict], ytdlp: str, args: argparse.Namespace) -> List[dict]:
    """Reemplaza fechas aproximadas de pestaña por metadatos exactos del video."""
    clients = ["android", "web,ios", "tv", "mweb", "web_embedded"]

    def hydrate_one(original: dict) -> dict:
        entry = dict(original)
        last_error = ""
        for client_index, client in enumerate(clients):
            command = [
                ytdlp, "--skip-download", "--dump-single-json", "--quiet", "--no-warnings",
                "--extractor-args", "youtube:player_client={}".format(client),
            ] + cookie_args(args) + [entry["url"]]
            result = subprocess.run(command, capture_output=True, text=True)
            if result.returncode == 0 and result.stdout.strip():
                try:
                    metadata = json.loads(result.stdout)
                except ValueError:
                    last_error = "metadatos JSON invalidos"
                    continue
                timestamp = metadata.get("release_timestamp") or metadata.get("timestamp")
                entry.update({
                    "title": metadata.get("title") or entry.get("title", ""),
                    "published_at": (
                        datetime.fromtimestamp(timestamp, timezone.utc).isoformat()
                        if timestamp else entry.get("published_at")
                    ),
                    "upload_date": metadata.get("release_date") or metadata.get("upload_date") or entry.get("upload_date"),
                    "channel_id": metadata.get("channel_id") or entry.get("channel_id"),
                    "channel_title": metadata.get("channel") or entry.get("channel_title"),
                    "is_stream": bool(metadata.get("was_live") or metadata.get("is_live") or entry.get("is_stream")),
                    "live_status": metadata.get("live_status") or entry.get("live_status"),
                    "date_precision": "exact",
                    "metadata_client": client,
                })
                break
            last_error = (result.stderr or result.stdout).strip()[-500:]
            if client_index + 1 < len(clients) and args.client_sleep:
                time.sleep(args.client_sleep)
        if entry.get("date_precision") != "exact":
            entry["date_hydration_error"] = last_error or "sin metadatos exactos"
        return entry

    hydrated = []
    with ThreadPoolExecutor(max_workers=args.metadata_workers) as executor:
        for index, entry in enumerate(executor.map(hydrate_one, entries), 1):
            hydrated.append(entry)
            if index % 10 == 0 or index == len(entries):
                print("    fechas exactas: {}/{}".format(index, len(entries)), flush=True)
    return hydrated


def normalize_comment(comment: dict, video_id: str, source: str) -> dict:
    if source == "youtube_data_api":
        snippet = comment.get("snippet", {})
        author_channel = snippet.get("authorChannelId") or {}
        return {
            "record_type": "comment",
            "video_id": video_id,
            "comment_id": comment.get("id"),
            "parent_id": snippet.get("parentId"),
            "is_reply": bool(snippet.get("parentId")),
            "author_id": author_channel.get("value") if isinstance(author_channel, dict) else author_channel,
            "author_name": snippet.get("authorDisplayName", ""),
            "text": snippet.get("textOriginal") or snippet.get("textDisplay", ""),
            "like_count": snippet.get("likeCount", 0),
            "published_at": snippet.get("publishedAt"),
            "updated_at": snippet.get("updatedAt"),
            "source": source,
        }
    timestamp = comment.get("timestamp")
    parent = comment.get("parent")
    return {
        "record_type": "comment",
        "video_id": video_id,
        "comment_id": comment.get("id"),
        "parent_id": None if parent in {None, "root"} else parent,
        "is_reply": parent not in {None, "root"},
        "author_id": comment.get("author_id"),
        "author_name": comment.get("author", ""),
        "text": comment.get("text", ""),
        "like_count": comment.get("like_count", 0),
        "published_at": datetime.fromtimestamp(timestamp, timezone.utc).isoformat() if timestamp else None,
        "updated_at": None,
        "source": source,
    }


def iter_api_comments(video_id: str, api_key: str) -> Iterator[dict]:
    token = None
    while True:
        params = {
            "part": "snippet", "videoId": video_id, "maxResults": 100,
            "order": "time", "textFormat": "plainText",
        }
        if token:
            params["pageToken"] = token
        payload = api_get("commentThreads", params, api_key)
        for thread in payload.get("items", []):
            snippet = thread.get("snippet", {})
            top = snippet.get("topLevelComment")
            if top:
                yield normalize_comment(top, video_id, "youtube_data_api")
            total_replies = int(snippet.get("totalReplyCount", 0))
            if total_replies and top:
                reply_token = None
                while True:
                    reply_params = {
                        "part": "snippet", "parentId": top["id"], "maxResults": 100,
                        "textFormat": "plainText",
                    }
                    if reply_token:
                        reply_params["pageToken"] = reply_token
                    reply_payload = api_get("comments", reply_params, api_key)
                    for reply in reply_payload.get("items", []):
                        yield normalize_comment(reply, video_id, "youtube_data_api")
                    reply_token = reply_payload.get("nextPageToken")
                    if not reply_token:
                        break
        token = payload.get("nextPageToken")
        if not token:
            break


def download_comments_api(entry: dict, destination: Path, api_key: str) -> int:
    return atomic_jsonl(destination, iter_api_comments(entry["video_id"], api_key))


def download_comments_ytdlp(entry: dict, destination: Path, ytdlp: str, args: argparse.Namespace) -> int:
    max_comments = args.max_comments
    extractor_value = "all,all,all,all" if max_comments == "all" else "{},all,all,all".format(int(max_comments))
    clients = ["web,ios", "android", "tv", "mweb", "web_embedded"]
    errors = []
    unavailable_seen = False
    for index, client in enumerate(clients):
        with tempfile.TemporaryDirectory() as folder:
            prefix = str(Path(folder) / "raw")
            command = [
                ytdlp, "--skip-download", "--write-info-json", "--write-comments",
                "--quiet", "--no-warnings", "--sleep-requests", "1",
                "--extractor-args",
                "youtube:player_client={};comment_sort=new;max_comments={}".format(client, extractor_value),
                "-o", prefix,
            ] + cookie_args(args) + [entry["url"]]
            result = subprocess.run(command, capture_output=True, text=True)
            info_path = Path(prefix + ".info.json")
            if info_path.exists():
                info = json.loads(info_path.read_text(encoding="utf-8"))
                comments = info.get("comments")
                if comments is None and int(info.get("comment_count") or 0) == 0:
                    comments = []
                if comments is not None:
                    return atomic_jsonl(
                        destination,
                        (normalize_comment(comment, entry["video_id"], "yt_dlp") for comment in comments),
                    )
                errors.append("cliente {} no devolvio la coleccion comments".format(client))
            else:
                error = result.stderr.strip()[-1500:]
                errors.append("{}: {}".format(client, error or "sin info JSON"))
                unavailable_seen = unavailable_seen or any(
                    marker in error.casefold() for marker in UNAVAILABLE_MARKERS
                )
        if index + 1 < len(clients):
            time.sleep(args.client_sleep)
    message = " | ".join(errors)[-4000:]
    if unavailable_seen:
        raise UnavailableError(message or "comentarios no disponibles")
    raise RuntimeError("yt-dlp comentarios: {}".format(message or "sin info JSON"))


def process_comments_parallel(
    entries: Sequence[dict], inventory: Inventory, root: Path,
    comments_backend: str, api_key: Optional[str], ytdlp: str,
    args: argparse.Namespace,
) -> int:
    """Descarga comentarios en paralelo y serializa las mutaciones del inventario."""
    pending = []
    touched = 0
    for entry in entries:
        if args.limit and touched >= args.limit:
            break
        video_id = entry["video_id"]
        if not inventory.should_process(video_id, "comments", args.retry_unavailable):
            continue
        existing = inventory.artifact_path(video_id, "comments")
        destination = existing or root / "comments" / artifact_filename(entry, "comments")
        if existing is None and inventory.adopt(video_id, "comments", destination):
            print("[=] comentarios existentes adoptados {}".format(video_id))
            continue
        inventory.attempt(video_id, "comments", comments_backend)
        pending.append((entry, destination))
        touched += 1
    inventory.save()
    print("[*] comentarios en cola: {} ({} trabajadores)".format(len(pending), args.comment_workers))

    def download(item):
        entry, destination = item
        if comments_backend == "api":
            count = download_comments_api(entry, destination, api_key)
        else:
            count = download_comments_ytdlp(entry, destination, ytdlp, args)
        return entry, destination, count

    with ThreadPoolExecutor(max_workers=args.comment_workers) as executor:
        futures = {executor.submit(download, item): item for item in pending}
        for future in as_completed(futures):
            entry, destination = futures[future]
            video_id = entry["video_id"]
            try:
                _, destination, count = future.result()
                inventory.complete(video_id, "comments", destination, count, comments_backend)
                print("[+] comentarios {}: {} ({})".format(video_id, count, entry.get("title", "")[:70]), flush=True)
            except UnavailableError as exc:
                inventory.mark(video_id, "comments", "unavailable", str(exc))
                print("[=] comentarios {} no disponibles".format(video_id), flush=True)
            except Exception as exc:
                inventory.mark(video_id, "comments", "error", str(exc))
                print("[!] comentarios {}: {}".format(video_id, str(exc)[-500:]), flush=True)
            inventory.save()
    return touched


def canonical_chat_records(payload: dict, video_id: str) -> Iterator[dict]:
    replay = payload.get("replayChatItemAction", {})
    try:
        replay_ms = int(replay.get("videoOffsetTimeMsec", 0))
    except (TypeError, ValueError):
        replay_ms = 0
    for action in replay.get("actions", []):
        item = action.get("addChatItemAction", {}).get("item", {})
        for renderer_type, renderer in item.items():
            if not isinstance(renderer, dict) or not renderer_type.startswith("liveChat"):
                continue
            author_id = renderer.get("authorExternalChannelId")
            if not author_id:
                continue
            text = extract_run_text(renderer.get("message", {}).get("runs", []))
            timestamp_usec = renderer.get("timestampUsec")
            published_at = None
            if timestamp_usec:
                try:
                    published_at = datetime.fromtimestamp(int(timestamp_usec) / 1_000_000, timezone.utc).isoformat()
                except (TypeError, ValueError, OSError):
                    pass
            yield {
                "record_type": "chat_message",
                "video_id": video_id,
                "message_id": renderer.get("id"),
                "time_ms": replay_ms,
                "published_at": published_at,
                "author_id": author_id,
                "author_name": renderer.get("authorName", {}).get("simpleText", ""),
                "text": text,
                "message_type": renderer_type,
                "amount": renderer.get("purchaseAmountText", {}).get("simpleText"),
                "source": "yt_dlp_live_chat",
            }


def iter_raw_chat(path: Path, video_id: str) -> Iterator[dict]:
    with path.open("r", encoding="utf-8") as handle:
        for line in handle:
            try:
                payload = json.loads(line)
            except ValueError:
                continue
            yield from canonical_chat_records(payload, video_id)


def discover_legacy_chats(root: Path) -> Dict[str, Path]:
    """Indexa exportaciones crudas cuyo video_id aparece en el nombre."""
    if not root.is_dir():
        raise RuntimeError("Directorio de chats heredados inexistente: {}".format(root))
    indexed: Dict[str, Path] = {}
    for path in sorted(root.rglob("*.jsonl")):
        match = LEGACY_CHAT_RE.search(path.name)
        if not match:
            continue
        video_id = match.group(1)
        previous = indexed.get(video_id)
        if previous is not None and previous.resolve() != path.resolve():
            raise RuntimeError(
                "Dos chats heredados para {}: {} y {}".format(video_id, previous, path)
            )
        indexed[video_id] = path
    return indexed


def import_legacy_chat(source: Path, destination: Path, video_id: str) -> int:
    """Convierte una repetición cruda a JSONL canónico sin alterar la fuente."""
    count = atomic_jsonl(destination, iter_raw_chat(source, video_id))
    if count == 0:
        try:
            destination.unlink()
        except OSError:
            pass
        raise RuntimeError("El chat heredado no produjo mensajes canónicos: {}".format(source))
    return count


def chat_downloader_record(item: dict, video_id: str) -> dict:
    author = item.get("author") or {}
    seconds = item.get("time_in_seconds")
    if seconds is None:
        seconds = 0
    timestamp = item.get("timestamp")
    published_at = None
    if timestamp:
        try:
            value = float(timestamp)
            if value > 10_000_000_000:
                value /= 1_000_000
            published_at = datetime.fromtimestamp(value, timezone.utc).isoformat()
        except (TypeError, ValueError, OSError):
            pass
    return {
        "record_type": "chat_message", "video_id": video_id,
        "message_id": item.get("message_id"), "time_ms": int(float(seconds) * 1000),
        "published_at": published_at, "author_id": author.get("id"),
        "author_name": author.get("name", ""), "text": item.get("message", ""),
        "message_type": item.get("message_type"), "amount": item.get("money", {}).get("text"),
        "source": "chat_downloader",
    }


def download_chat(entry: dict, destination: Path, ytdlp: str, args: argparse.Namespace) -> Tuple[int, str]:
    clients = ["web,ios", "android", "tv", "mweb", "web_embedded"]
    errors = []
    unavailable_seen = False
    for index, client in enumerate(clients):
        with tempfile.TemporaryDirectory() as folder:
            prefix = str(Path(folder) / "raw")
            command = [
                ytdlp, "--skip-download", "--write-subs", "--sub-langs", "live_chat",
                "--extractor-args", "youtube:player_client={}".format(client),
                "--socket-timeout", "30", "--retries", "3", "--fragment-retries", "3",
                "--quiet", "--no-warnings", "-o", prefix,
            ] + cookie_args(args) + [entry["url"]]
            result = subprocess.run(command, capture_output=True, text=True)
            raw = Path(prefix + ".live_chat.json")
            if raw.exists() and raw.stat().st_size:
                count = atomic_jsonl(destination, iter_raw_chat(raw, entry["video_id"]))
                return count, "yt_dlp:{}".format(client)
            error = result.stderr.strip()[-1500:]
            errors.append(error)
            unavailable_seen = unavailable_seen or any(marker in error.casefold() for marker in UNAVAILABLE_MARKERS)
        if index + 1 < len(clients):
            time.sleep(args.client_sleep)

    try:
        from chat_downloader import ChatDownloader
    except ImportError:
        ChatDownloader = None
    if ChatDownloader is not None:
        def records() -> Iterator[dict]:
            chat = ChatDownloader().get_chat(entry["url"])
            for item in chat:
                yield chat_downloader_record(item, entry["video_id"])
        try:
            return atomic_jsonl(destination, records()), "chat_downloader"
        except Exception as exc:
            errors.append(str(exc))
            unavailable_seen = unavailable_seen or any(marker in str(exc).casefold() for marker in UNAVAILABLE_MARKERS)
    message = " | ".join(error for error in errors if error)[-3000:]
    if unavailable_seen:
        raise UnavailableError(message or "chat no disponible")
    raise RuntimeError(message or "no fue posible descargar el chat")


def load_catalog(path: Path) -> List[dict]:
    if not path.exists():
        return []
    entries = []
    with path.open("r", encoding="utf-8") as handle:
        for line in handle:
            try:
                entries.append(json.loads(line))
            except ValueError:
                continue
    return entries


def select_backend(requested: str, api_key: Optional[str]) -> str:
    if requested == "auto":
        return "api" if api_key else "ytdlp"
    return requested


def parse_args(argv: Optional[Sequence[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--channel", default=DEFAULT_CHANNEL)
    parser.add_argument("--output-dir", default="presidencia_data")
    parser.add_argument("--since", default=DEFAULT_SINCE, help="fecha inicial inclusiva")
    parser.add_argument("--until", default=date.today().isoformat(), help="fecha final inclusiva")
    parser.add_argument("--mode", choices=("all", "catalog", "chats", "comments"), default="all")
    parser.add_argument("--catalog-backend", choices=("auto", "api", "ytdlp"), default="auto")
    parser.add_argument("--comments-backend", choices=("auto", "api", "ytdlp"), default="auto")
    parser.add_argument("--no-refresh-catalog", action="store_true")
    parser.add_argument("--catalog-limit", type=int, default=0, help="0 significa catalogo completo")
    parser.add_argument(
        "--approximate-catalog-dates", action="store_true",
        help="no hidratar cada video; mas rapido pero impreciso cerca de los limites",
    )
    parser.add_argument("--limit", type=int, default=10, help="videos con descargas por corrida; 0=sin limite")
    parser.add_argument("--max-comments", default="all", help="all o maximo por video para yt-dlp")
    parser.add_argument("--sleep", type=float, default=5.0, help="pausa entre videos")
    parser.add_argument("--client-sleep", type=float, default=3.0, help="pausa al rotar clientes de chat")
    parser.add_argument(
        "--metadata-workers", type=int, default=4,
        help="procesos simultáneos para verificar fechas exactas (predeterminado: 4)",
    )
    parser.add_argument(
        "--comment-workers", type=int, default=1,
        help="descargas simultáneas cuando --mode comments (predeterminado: 1)",
    )
    parser.add_argument("--cookies")
    parser.add_argument("--cookies-from-browser", help="por ejemplo chrome o firefox")
    parser.add_argument(
        "--legacy-chat-dir",
        help="directorio de chats crudos existentes; se importan por video_id antes de descargar",
    )
    parser.add_argument(
        "--legacy-only", action="store_true",
        help="importar únicamente chats presentes en --legacy-chat-dir, sin solicitudes remotas",
    )
    parser.add_argument("--retry-unavailable", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)
    try:
        args.since_day = parse_day(args.since)
        args.until_day = parse_day(args.until)
    except ValueError:
        parser.error("--since y --until deben usar YYYY-MM-DD")
    if args.since_day > args.until_day:
        parser.error("--since no puede ser posterior a --until")
    if args.metadata_workers <= 0:
        parser.error("--metadata-workers debe ser positivo")
    if args.comment_workers <= 0:
        parser.error("--comment-workers debe ser positivo")
    if args.legacy_only and not args.legacy_chat_dir:
        parser.error("--legacy-only requiere --legacy-chat-dir")
    if args.max_comments != "all":
        try:
            if int(args.max_comments) <= 0:
                raise ValueError
        except ValueError:
            parser.error("--max-comments debe ser all o un entero positivo")
    return args


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = parse_args(argv)
    root = Path(args.output_dir).resolve()
    root.mkdir(parents=True, exist_ok=True)
    catalog_path = root / "catalog.jsonl"
    inventory = Inventory(root / "inventory.json", root, args.channel, args.since, args.until)
    legacy_chats = (
        discover_legacy_chats(Path(args.legacy_chat_dir).resolve())
        if args.legacy_chat_dir else {}
    )
    if legacy_chats:
        print("[=] chats heredados indexados: {}".format(len(legacy_chats)))
    api_key = os.environ.get("YOUTUBE_API_KEY")
    catalog_backend = select_backend(args.catalog_backend, api_key)
    comments_backend = select_backend(args.comments_backend, api_key)
    ytdlp = resolve_ytdlp()

    scraping_needed = (
        (not args.no_refresh_catalog and catalog_backend == "ytdlp")
        or (args.mode in {"all", "chats"} and not args.legacy_only)
        or (args.mode in {"all", "comments"} and comments_backend == "ytdlp" and not args.legacy_only)
    )
    if scraping_needed and os.environ.get("YOUTUBE_RASPADO") != "1":
        print("[!] Esta corrida requiere yt-dlp/raspado. Exporte YOUTUBE_RASPADO=1 conscientemente.")
        return 2
    if scraping_needed and not ytdlp:
        print("[!] No se encontro yt-dlp. Instale con: python3 -m pip install -U yt-dlp")
        return 2
    if (catalog_backend == "api" or comments_backend == "api") and not api_key:
        print("[!] El backend API requiere YOUTUBE_API_KEY")
        return 2

    if args.no_refresh_catalog:
        entries = load_catalog(catalog_path)
        if not entries:
            print("[!] No existe catalogo local; quite --no-refresh-catalog")
            return 2
        loaded_count = len(entries)
        entries = [entry for entry in entries if within_window(entry, args.since_day, args.until_day)]
        if len(entries) != loaded_count:
            print("[=] catalogo local depurado por fecha: {} -> {}".format(loaded_count, len(entries)))
            atomic_jsonl(catalog_path, entries)
    else:
        if catalog_backend == "api":
            entries = discover_channel_api(args.channel, api_key)
        else:
            entries = discover_channel_ytdlp(args.channel, ytdlp, args)
        discovered_count = len(entries)
        undated_count = sum(entry_day(entry) is None for entry in entries)
        entries = [entry for entry in entries if within_window(entry, args.since_day, args.until_day)]
        if catalog_backend == "ytdlp" and entries and not args.approximate_catalog_dates:
            print("[*] verificando fechas exactas de {} candidatos".format(len(entries)))
            entries = hydrate_catalog_dates_ytdlp(entries, ytdlp, args)
            entries = [entry for entry in entries if within_window(entry, args.since_day, args.until_day)]
        if discovered_count and not entries and undated_count == discovered_count:
            print(
                "[!] yt-dlp encontro {} videos pero ninguno tenia fecha; "
                "no se sobrescribio el catalogo. Actualice yt-dlp o use YOUTUBE_API_KEY.".format(discovered_count)
            )
            return 3
        entries.sort(key=lambda entry: entry.get("published_at") or entry.get("upload_date") or "", reverse=True)
        atomic_jsonl(catalog_path, entries)

    for entry in entries:
        inventory.ensure_video(entry)
    inventory.set_active_video_ids(entry["video_id"] for entry in entries)
    inventory.save()
    streams = sum(bool(entry.get("is_stream")) for entry in entries)
    print("[=] catalogo: {} videos, {} transmisiones, ventana {}..{}".format(
        len(entries), streams, args.since, args.until
    ))
    if args.mode == "catalog" or args.dry_run:
        if args.dry_run:
            for entry in entries[:25]:
                print("    {} {} {}".format(entry_day(entry), entry["video_id"], entry.get("title", "")[:90]))
        return 0

    if args.mode == "comments" and args.comment_workers > 1:
        process_comments_parallel(
            entries, inventory, root, comments_backend, api_key, ytdlp, args
        )
        inventory.save()
        print("[=] inventario actualizado: {}".format(inventory.path))
        return 0

    touched = 0
    for entry in entries:
        if args.limit and touched >= args.limit:
            print("[=] tope de {} videos; relance para continuar".format(args.limit))
            break
        video_id = entry["video_id"]
        if args.legacy_only and video_id not in legacy_chats:
            continue
        work_done = False

        if args.mode in {"all", "chats"}:
            chat_state = inventory.data["videos"][video_id]["chat"]
            live_status = entry.get("live_status")
            if not entry.get("is_stream"):
                if chat_state.get("status") == "pending":
                    inventory.mark(video_id, "chat", "not_applicable", "video no identificado como transmision")
            elif live_status in {"is_live", "is_upcoming"}:
                inventory.mark(video_id, "chat", "deferred", "transmision aun activa o futura")
            elif inventory.should_process(video_id, "chat", args.retry_unavailable):
                existing = inventory.artifact_path(video_id, "chat")
                destination = existing or root / "chats" / artifact_filename(entry, "chat")
                if existing is None and inventory.adopt(video_id, "chat", destination):
                    print("[=] chat existente adoptado {}".format(video_id))
                elif existing is None and video_id in legacy_chats:
                    source = legacy_chats[video_id]
                    inventory.attempt(video_id, "chat", "legacy_import")
                    inventory.save()
                    print("[*] importando chat heredado {} — {}".format(video_id, source.name))
                    try:
                        count = import_legacy_chat(source, destination, video_id)
                        inventory.complete(video_id, "chat", destination, count, "legacy_import")
                        chat_state = inventory.data["videos"][video_id]["chat"]
                        chat_state["imported_from"] = str(source)
                        chat_state["source_bytes"] = source.stat().st_size
                        chat_state["source_sha256"] = sha256_file(source)
                        print("    {} mensajes canónicos importados".format(count))
                    except Exception as exc:
                        inventory.mark(video_id, "chat", "error", str(exc))
                        print("    [!] {}".format(exc))
                    work_done = True
                else:
                    inventory.attempt(video_id, "chat", "yt_dlp")
                    inventory.save()
                    print("[*] chat {} — {}".format(video_id, entry.get("title", "")[:80]))
                    try:
                        count, backend = download_chat(entry, destination, ytdlp, args)
                        inventory.complete(video_id, "chat", destination, count, backend)
                        print("    {} mensajes ({})".format(count, backend))
                    except UnavailableError as exc:
                        inventory.mark(video_id, "chat", "unavailable", str(exc))
                        print("    [=] chat no disponible")
                    except Exception as exc:
                        inventory.mark(video_id, "chat", "error", str(exc))
                        print("    [!] {}".format(exc))
                    work_done = True

        if (
            not args.legacy_only and args.mode in {"all", "comments"}
            and inventory.should_process(video_id, "comments", args.retry_unavailable)
        ):
            existing = inventory.artifact_path(video_id, "comments")
            destination = existing or root / "comments" / artifact_filename(entry, "comments")
            if existing is None and inventory.adopt(video_id, "comments", destination):
                print("[=] comentarios existentes adoptados {}".format(video_id))
            else:
                inventory.attempt(video_id, "comments", comments_backend)
                inventory.save()
                print("[*] comentarios {} — {}".format(video_id, entry.get("title", "")[:80]))
                try:
                    if comments_backend == "api":
                        count = download_comments_api(entry, destination, api_key)
                    else:
                        count = download_comments_ytdlp(entry, destination, ytdlp, args)
                    inventory.complete(video_id, "comments", destination, count, comments_backend)
                    print("    {} comentarios ({})".format(count, comments_backend))
                except UnavailableError as exc:
                    inventory.mark(video_id, "comments", "unavailable", str(exc))
                    print("    [=] comentarios deshabilitados/no disponibles")
                except Exception as exc:
                    inventory.mark(video_id, "comments", "error", str(exc))
                    print("    [!] {}".format(exc))
                work_done = True

        inventory.save()
        if work_done:
            touched += 1
            if args.sleep and (not args.limit or touched < args.limit):
                time.sleep(args.sleep)
    print("[=] inventario actualizado: {}".format(inventory.path))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
