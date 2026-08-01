#!/usr/bin/env python3
"""Detector exploratorio de automatizacion y coordinacion en chats de YouTube.

No clasifica identidades como bots. Produce senales auditables para priorizar una
revision humana: repeticion, cadencia, reutilizacion entre sesiones y mensajes
iguales o casi iguales publicados por cuentas distintas en una ventana corta.
"""

import argparse
import csv
import glob
import hashlib
import html
import json
import math
import os
import re
import statistics
import sys
import unicodedata
from collections import Counter, defaultdict, deque
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Dict, Iterable, Iterator, List, Optional, Sequence, Set, Tuple


VERSION = "0.1.0"
URL_RE = re.compile(r"https?://\S+|www\.\S+", re.IGNORECASE)
SPACE_RE = re.compile(r"\s+")
NON_WORD_RE = re.compile(r"[^\w<>]+", re.UNICODE)
TOKEN_RE = re.compile(r"\w+|<url>", re.UNICODE)

# Se usan solamente para comparar textos casi iguales; nunca para puntuar una
# postura politica o el vocabulario de una cuenta.
STOPWORDS = {
    "a", "al", "algo", "ante", "como", "con", "contra", "de", "del", "desde",
    "donde", "el", "ella", "en", "entre", "era", "es", "esa", "ese", "eso",
    "esta", "este", "esto", "ha", "hay", "la", "las", "le", "les", "lo", "los",
    "mas", "me", "mi", "muy", "no", "nos", "o", "para", "pero", "por", "que",
    "se", "si", "sin", "son", "su", "sus", "te", "tiene", "tu", "un", "una",
    "uno", "y", "ya",
}


@dataclass(frozen=True)
class Message:
    session: str
    time_ms: int
    author_id: str
    author_name: str
    text: str
    normalized: str
    source: str = "chat"
    time_precision: str = "exact"


def clamp(value: float, low: float = 0.0, high: float = 1.0) -> float:
    return max(low, min(high, value))


def normalize_text(text: str) -> str:
    text = unicodedata.normalize("NFKC", text).casefold()
    text = URL_RE.sub(" <url> ", text)
    text = NON_WORD_RE.sub(" ", text)
    return SPACE_RE.sub(" ", text).strip()


def extract_run_text(runs: Sequence[dict]) -> str:
    pieces = []
    for run in runs:
        if "text" in run:
            pieces.append(str(run["text"]))
            continue
        emoji = run.get("emoji", {})
        label = (
            emoji.get("accessibility", {})
            .get("accessibilityData", {})
            .get("label", "")
        )
        pieces.append(str(label))
    return "".join(pieces)


def parse_iso_time_ms(value: object) -> int:
    if not value:
        return 0
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return int(parsed.timestamp() * 1000)
    except (TypeError, ValueError, OverflowError):
        return 0


def iter_messages(paths: Iterable[str]) -> Iterator[Message]:
    for path in sorted(paths):
        session = os.path.basename(os.path.dirname(path))
        with open(path, "r", encoding="utf-8") as handle:
            for line_number, line in enumerate(handle, 1):
                try:
                    payload = json.loads(line)
                except json.JSONDecodeError as exc:
                    raise ValueError("JSON invalido en {}:{}: {}".format(path, line_number, exc))
                if payload.get("record_type") == "chat_message":
                    author_id = str(payload.get("author_id") or "").strip()
                    if author_id:
                        text = str(payload.get("text") or "")
                        try:
                            time_ms = int(payload.get("time_ms") or 0)
                        except (TypeError, ValueError):
                            time_ms = 0
                        yield Message(
                            session=str(payload.get("video_id") or payload.get("session") or session),
                            time_ms=time_ms,
                            author_id=author_id,
                            author_name=str(payload.get("author_name") or ""),
                            text=text,
                            normalized=normalize_text(text),
                            source="chat",
                            time_precision="exact",
                        )
                    continue
                if payload.get("record_type") == "comment":
                    author_id = str(payload.get("author_id") or "").strip()
                    if author_id:
                        text = str(payload.get("text") or "")
                        # yt-dlp expone en estos archivos una fecha aproximada al
                        # dia. La API oficial, en cambio, conserva hora exacta.
                        precision = "day" if payload.get("source") == "yt_dlp" else "exact"
                        yield Message(
                            session="{}__comments".format(
                                payload.get("video_id") or payload.get("session") or session
                            ),
                            time_ms=parse_iso_time_ms(payload.get("published_at")),
                            author_id=author_id,
                            author_name=str(payload.get("author_name") or ""),
                            text=text,
                            normalized=normalize_text(text),
                            source="comment",
                            time_precision=precision,
                        )
                    continue
                replay = payload.get("replayChatItemAction", {})
                try:
                    time_ms = int(replay.get("videoOffsetTimeMsec", 0))
                except (TypeError, ValueError):
                    time_ms = 0
                for action in replay.get("actions", []):
                    item = action.get("addChatItemAction", {}).get("item", {})
                    renderer = item.get("liveChatTextMessageRenderer")
                    if not renderer:
                        continue
                    author_id = str(renderer.get("authorExternalChannelId", "")).strip()
                    if not author_id:
                        continue
                    text = extract_run_text(renderer.get("message", {}).get("runs", []))
                    yield Message(
                        session=session,
                        time_ms=time_ms,
                        author_id=author_id,
                        author_name=str(renderer.get("authorName", {}).get("simpleText", "")),
                        text=text,
                        normalized=normalize_text(text),
                        source="chat",
                        time_precision="exact",
                    )


def discover_jsonl(input_path: str) -> List[str]:
    if os.path.isfile(input_path):
        return [input_path]
    return sorted(glob.glob(os.path.join(input_path, "**", "*.jsonl"), recursive=True))


def load_catalog_video_ids(input_path: str) -> Optional[Set[str]]:
    """Carga el universo permitido cuando la raiz contiene catalog.jsonl."""
    if not os.path.isdir(input_path):
        return None
    catalog = os.path.join(input_path, "catalog.jsonl")
    if not os.path.isfile(catalog):
        return None
    video_ids: Set[str] = set()
    with open(catalog, "r", encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, 1):
            try:
                payload = json.loads(line)
            except json.JSONDecodeError as exc:
                raise ValueError("JSON invalido en {}:{}: {}".format(catalog, line_number, exc))
            video_id = str(payload.get("video_id") or "").strip()
            if video_id:
                video_ids.add(video_id)
    return video_ids


def filter_messages_to_catalog(messages: Sequence[Message], input_path: str) -> Tuple[List[Message], int]:
    allowed = load_catalog_video_ids(input_path)
    if allowed is None:
        return list(messages), 0
    filtered = [
        message for message in messages
        if message.session.removesuffix("__comments") in allowed
    ]
    return filtered, len(messages) - len(filtered)


def informative_tokens(normalized: str) -> Set[str]:
    return {
        token for token in TOKEN_RE.findall(normalized)
        if token not in STOPWORDS and (len(token) >= 3 or token == "<url>")
    }


def qualifies_exact(normalized: str) -> bool:
    tokens = TOKEN_RE.findall(normalized)
    return len(normalized) >= 12 and len(tokens) >= 3


def text_key(text: str) -> str:
    return hashlib.sha1(text.encode("utf-8")).hexdigest()[:12]


def add_evidence(
    evidence: Dict[Tuple[str, str, str, str], dict],
    first: Message,
    second: Message,
    kind: str,
    similarity: float,
    fingerprint: str,
    author_counts: Dict[str, int],
) -> None:
    if first.author_id == second.author_id:
        return
    left, right = sorted((first.author_id, second.author_id))
    key = (left, right, first.session, fingerprint)
    lag_seconds = abs(second.time_ms - first.time_ms) / 1000.0
    shared_authors = max(2, author_counts.get(first.normalized, 2))
    rarity = 1.0 / math.log2(1.0 + shared_authors)
    candidate = {
        "left": left,
        "right": right,
        "session": first.session,
        "kind": kind,
        "similarity": similarity,
        "lag_seconds": lag_seconds,
        "rarity": rarity,
        "example": first.text[:240],
        "fingerprint": fingerprint,
    }
    existing = evidence.get(key)
    if existing is None or lag_seconds < existing["lag_seconds"]:
        evidence[key] = candidate


def find_coordination(
    messages: Sequence[Message], sync_window_seconds: int, near_threshold: float
) -> Tuple[List[dict], Dict[str, List[dict]]]:
    """Devuelve enlaces agregados y evidencia por cuenta."""
    window_ms = sync_window_seconds * 1000
    author_sets: Dict[str, Set[str]] = defaultdict(set)
    exact_groups: Dict[Tuple[str, str], List[Message]] = defaultdict(list)
    by_session: Dict[str, List[Message]] = defaultdict(list)
    for message in messages:
        if message.time_precision != "exact":
            continue
        by_session[message.session].append(message)
        if qualifies_exact(message.normalized):
            author_sets[message.normalized].add(message.author_id)
            exact_groups[(message.session, message.normalized)].append(message)
    author_counts = {text: len(authors) for text, authors in author_sets.items()}
    evidence: Dict[Tuple[str, str, str, str], dict] = {}

    # Coincidencias exactas dentro de una ventana temporal corta.
    for (session, normalized), occurrences in exact_groups.items():
        if len(author_sets[normalized]) < 2:
            continue
        occurrences.sort(key=lambda item: item.time_ms)
        active: deque = deque()
        for current in occurrences:
            while active and current.time_ms - active[0].time_ms > window_ms:
                active.popleft()
            for prior in active:
                add_evidence(
                    evidence, prior, current, "exact", 1.0,
                    "e:" + text_key(normalized), author_counts,
                )
            active.append(current)

    # Coincidencias casi iguales. Solo se comparan mensajes cercanos en tiempo.
    for session, session_messages in by_session.items():
        session_messages.sort(key=lambda item: item.time_ms)
        active = deque()
        for current in session_messages:
            while active and current.time_ms - active[0][0].time_ms > window_ms:
                active.popleft()
            current_tokens = informative_tokens(current.normalized)
            if len(current.normalized) >= 20 and 3 <= len(current_tokens) <= 60:
                for prior, prior_tokens in active:
                    if prior.author_id == current.author_id or prior.normalized == current.normalized:
                        continue
                    if not prior_tokens or current_tokens.isdisjoint(prior_tokens):
                        continue
                    union = current_tokens | prior_tokens
                    similarity = len(current_tokens & prior_tokens) / float(len(union))
                    if similarity >= near_threshold:
                        combined = "|".join(sorted((text_key(prior.normalized), text_key(current.normalized))))
                        add_evidence(
                            evidence, prior, current, "near", similarity,
                            "n:" + combined, author_counts,
                        )
            active.append((current, current_tokens))

    grouped: Dict[Tuple[str, str], List[dict]] = defaultdict(list)
    for item in evidence.values():
        grouped[(item["left"], item["right"])].append(item)

    edges = []
    by_account: Dict[str, List[dict]] = defaultdict(list)
    for (left, right), items in grouped.items():
        sessions = {item["session"] for item in items}
        texts = {item["fingerprint"] for item in items}
        exact_count = sum(item["kind"] == "exact" for item in items)
        near_count = len(items) - exact_count
        weighted_events = sum(
            item["rarity"] * (1.15 if item["kind"] == "near" else 1.0)
            for item in items
        )
        score = 100.0 * (
            0.45 * clamp((weighted_events - 0.35) / 3.5)
            + 0.25 * clamp((len(sessions) - 1.0) / 4.0)
            + 0.20 * clamp((len(texts) - 1.0) / 5.0)
            + 0.10 * clamp((len(items) - 1.0) / 8.0)
        )
        edge = {
            "left": left,
            "right": right,
            "coordination_score": round(score, 2),
            "evidence_events": len(items),
            "exact_events": exact_count,
            "near_events": near_count,
            "sessions": len(sessions),
            "shared_texts": len(texts),
            "weighted_events": round(weighted_events, 3),
            "median_lag_seconds": round(statistics.median(item["lag_seconds"] for item in items), 2),
            "examples": " | ".join(dict.fromkeys(item["example"] for item in items))[:600],
        }
        edges.append(edge)
        by_account[left].append(edge)
        by_account[right].append(edge)
    edges.sort(key=lambda item: (item["coordination_score"], item["evidence_events"]), reverse=True)
    return edges, by_account


def percentile_ranks(values: Dict[str, int]) -> Dict[str, float]:
    ordered = sorted(values.values())
    if len(ordered) <= 1:
        return {key: 0.0 for key in values}
    return {
        key: sum(candidate < value for candidate in ordered) / float(len(ordered) - 1)
        for key, value in values.items()
    }


def risk_label(score: float, message_count: int) -> str:
    if message_count < 5:
        return "evidencia_insuficiente"
    if score >= 70:
        return "muy_alta_revision"
    if score >= 50:
        return "alta_revision"
    if score >= 25:
        return "revision"
    return "baja"


def analyze(messages: Sequence[Message], sync_window_seconds: int, near_threshold: float) -> Tuple[List[dict], List[dict], List[dict]]:
    by_author: Dict[str, List[Message]] = defaultdict(list)
    names: Dict[str, Counter] = defaultdict(Counter)
    for message in messages:
        by_author[message.author_id].append(message)
        if message.author_name:
            names[message.author_id][message.author_name] += 1

    edges, edges_by_account = find_coordination(messages, sync_window_seconds, near_threshold)
    volumes = {author: len(items) for author, items in by_author.items()}
    volume_percentiles = percentile_ranks(volumes)
    accounts = []
    for author, items in by_author.items():
        items.sort(key=lambda item: (item.session, item.time_ms))
        usable = [item for item in items if item.normalized]
        text_counts = Counter(item.normalized for item in usable)
        unique_texts = len(text_counts)
        repetition_ratio = 1.0 - unique_texts / float(max(1, len(usable)))
        dominant_ratio = max(text_counts.values(), default=0) / float(max(1, len(usable)))
        sessions_by_text: Dict[str, Set[str]] = defaultdict(set)
        for item in usable:
            sessions_by_text[item.normalized].add(item.session)
        cross_messages = sum(
            count for text, count in text_counts.items() if len(sessions_by_text[text]) >= 2
        )
        cross_ratio = cross_messages / float(max(1, len(usable)))

        timed_items = [item for item in items if item.time_precision == "exact"]
        rapid = 0
        intervals = []
        previous_by_session: Dict[str, int] = {}
        for item in timed_items:
            previous = previous_by_session.get(item.session)
            if previous is not None:
                interval = max(0.0, (item.time_ms - previous) / 1000.0)
                intervals.append(interval)
                if interval <= 15.0:
                    rapid += 1
            previous_by_session[item.session] = item.time_ms
        rapid_fraction = rapid / float(max(1, len(intervals)))
        active_minutes = len({(item.session, item.time_ms // 60000) for item in timed_items})
        active_density = len(timed_items) / float(max(1, active_minutes))

        account_edges = edges_by_account.get(author, [])
        strongest_edge = max((edge["coordination_score"] for edge in account_edges), default=0.0)
        credible_edges = [edge for edge in account_edges if edge["evidence_events"] >= 2]
        coord_sessions = len({
            edge["left"] + ":" + edge["right"]
            for edge in credible_edges
        })
        coord_signal = clamp(strongest_edge / 70.0) * 0.65 + clamp(coord_sessions / 4.0) * 0.35

        repeat_signal = 0.7 * clamp((repetition_ratio - 0.20) / 0.65) + 0.3 * clamp((dominant_ratio - 0.10) / 0.55)
        cadence_signal = 0.6 * clamp((rapid_fraction - 0.25) / 0.60) + 0.4 * clamp((active_density - 2.0) / 8.0)
        cross_signal = clamp((cross_ratio - 0.15) / 0.70)
        volume_signal = clamp((volume_percentiles[author] - 0.80) / 0.20)
        score = 100.0 * (
            0.25 * repeat_signal
            + 0.15 * cross_signal
            + 0.20 * cadence_signal
            + 0.10 * volume_signal
            + 0.30 * coord_signal
        )
        # Con muy poca actividad no existe base suficiente para una puntuacion alta.
        if len(items) < 10:
            score *= len(items) / 10.0
        account_name = names[author].most_common(1)[0][0] if names[author] else ""
        component_values = {
            "repeticion": repeat_signal,
            "entre_sesiones": cross_signal,
            "cadencia": cadence_signal,
            "volumen": volume_signal,
            "coordinacion": coord_signal,
        }
        reasons = [
            label for label, value in sorted(component_values.items(), key=lambda pair: pair[1], reverse=True)
            if value >= 0.35
        ]
        dominant_text = text_counts.most_common(1)[0][0] if text_counts else ""
        accounts.append({
            "author_id": author,
            "author_name": account_name,
            "signal_score": round(score, 2),
            "review_level": risk_label(score, len(items)),
            "messages": len(items),
            "sessions": len({item.session for item in items}),
            "active_minutes": active_minutes,
            "messages_per_active_minute": round(active_density, 3),
            "unique_texts": unique_texts,
            "repetition_ratio": round(repetition_ratio, 4),
            "dominant_text_ratio": round(dominant_ratio, 4),
            "cross_session_repeat_ratio": round(cross_ratio, 4),
            "rapid_15s_fraction": round(rapid_fraction, 4),
            "median_interval_seconds": round(statistics.median(intervals), 2) if intervals else "",
            "volume_percentile": round(volume_percentiles[author], 4),
            "coordination_partners": len(credible_edges),
            "strongest_coordination_score": round(strongest_edge, 2),
            "repetition_component": round(100 * repeat_signal, 2),
            "cross_session_component": round(100 * cross_signal, 2),
            "cadence_component": round(100 * cadence_signal, 2),
            "volume_component": round(100 * volume_signal, 2),
            "coordination_component": round(100 * coord_signal, 2),
            "review_reasons": " | ".join(reasons) if reasons else "sin_senal_dominante",
            "dominant_text": dominant_text[:300],
        })
    accounts.sort(key=lambda item: (item["signal_score"], item["messages"]), reverse=True)
    clusters = build_clusters(edges)
    return accounts, edges, clusters


def build_clusters(edges: Sequence[dict], threshold: float = 35.0) -> List[dict]:
    adjacency: Dict[str, Set[str]] = defaultdict(set)
    qualifying = [edge for edge in edges if edge["coordination_score"] >= threshold]
    for edge in qualifying:
        adjacency[edge["left"]].add(edge["right"])
        adjacency[edge["right"]].add(edge["left"])
    clusters = []
    visited: Set[str] = set()
    for start in adjacency:
        if start in visited:
            continue
        stack = [start]
        members = []
        visited.add(start)
        while stack:
            node = stack.pop()
            members.append(node)
            for neighbor in adjacency[node]:
                if neighbor not in visited:
                    visited.add(neighbor)
                    stack.append(neighbor)
        member_set = set(members)
        internal = [
            edge for edge in qualifying
            if edge["left"] in member_set and edge["right"] in member_set
        ]
        clusters.append({
            "cluster_id": 0,
            "members": len(members),
            "member_ids": " | ".join(sorted(members)),
            "strong_edges": len(internal),
            "max_edge_score": round(max(edge["coordination_score"] for edge in internal), 2),
            "evidence_events": sum(edge["evidence_events"] for edge in internal),
        })
    clusters.sort(key=lambda item: (item["max_edge_score"], item["members"]), reverse=True)
    for index, cluster in enumerate(clusters, 1):
        cluster["cluster_id"] = index
    return clusters


def write_csv(path: str, rows: Sequence[dict], fieldnames: Sequence[str]) -> None:
    def spreadsheet_safe(value: object) -> object:
        # Evita que texto controlado por usuarios se interprete como formula.
        if isinstance(value, str) and value.startswith(("=", "+", "-", "@")):
            return "'" + value
        return value

    with open(path, "w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow({key: spreadsheet_safe(value) for key, value in row.items()})


def make_summary(messages: Sequence[Message], accounts: Sequence[dict], edges: Sequence[dict], clusters: Sequence[dict], args: argparse.Namespace) -> dict:
    levels = Counter(account["review_level"] for account in accounts)
    return {
        "tool_version": VERSION,
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "input": os.path.abspath(args.input),
        "parameters": {
            "sync_window_seconds": args.sync_window,
            "near_similarity_threshold": args.near_threshold,
            "cluster_edge_threshold": 35.0,
        },
        "counts": {
            "messages": len(messages),
            "accounts": len(accounts),
            "sessions": len({message.session for message in messages}),
            "coordination_edges": len(edges),
            "strong_coordination_edges": sum(edge["coordination_score"] >= 35 for edge in edges),
            "coordination_clusters": len(clusters),
        },
        "review_levels": dict(levels),
        "interpretation": (
            "La puntuacion prioriza revision humana; no prueba que una cuenta sea un bot "
            "ni demuestra coordinacion intencional. El contenido politico no forma parte del puntaje."
        ),
    }


def write_html(path: str, summary: dict, accounts: Sequence[dict], edges: Sequence[dict], names: Dict[str, str]) -> None:
    def esc(value: object) -> str:
        return html.escape(str(value))

    account_rows = []
    for item in accounts[:100]:
        account_rows.append(
            "<tr><td>{}</td><td>{}</td><td>{}</td><td>{}</td><td>{}</td>"
            "<td>{}</td><td>{}</td><td>{}</td></tr>".format(
                esc(item["author_name"]), esc(item["author_id"]), esc(item["signal_score"]),
                esc(item["review_level"]), esc(item["messages"]), esc(item["sessions"]),
                esc(item["repetition_ratio"]), esc(item["strongest_coordination_score"]),
            )
        )
    edge_rows = []
    for edge in edges[:100]:
        edge_rows.append(
            "<tr><td>{}</td><td>{}</td><td>{}</td><td>{}</td><td>{}</td>"
            "<td>{}</td><td>{}</td></tr>".format(
                esc(names.get(edge["left"], edge["left"])),
                esc(names.get(edge["right"], edge["right"])),
                esc(edge["coordination_score"]), esc(edge["evidence_events"]),
                esc(edge["sessions"]), esc(edge["median_lag_seconds"]), esc(edge["examples"]),
            )
        )
    counts = summary["counts"]
    document = """<!doctype html>
<html lang="es"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Informe de señales de automatización y coordinación</title>
<style>
body{{font:15px system-ui,sans-serif;margin:0;background:#f5f6f8;color:#172033}}main{{max-width:1200px;margin:auto;padding:32px}}
h1{{margin-bottom:6px}}.note{{background:#fff4ce;border-left:5px solid #d99a00;padding:14px;margin:20px 0}}
.cards{{display:grid;grid-template-columns:repeat(auto-fit,minmax(160px,1fr));gap:12px}}.card{{background:white;padding:18px;border-radius:10px}}
.big{{font-size:28px;font-weight:700}}section{{margin-top:30px}}.table{{overflow:auto;background:white;border-radius:10px}}
table{{border-collapse:collapse;width:100%}}th,td{{padding:9px 11px;text-align:left;border-bottom:1px solid #e5e8ee;vertical-align:top}}
th{{position:sticky;top:0;background:#edf0f5}}td:last-child{{max-width:380px}}code{{font-size:12px}}
</style></head><body><main>
<h1>Señales de automatización y coordinación</h1><div>Generado: {generated}</div>
<div class="note"><strong>Importante:</strong> {interpretation}</div>
<div class="cards"><div class="card"><div class="big">{messages}</div>mensajes</div>
<div class="card"><div class="big">{accounts}</div>cuentas</div><div class="card"><div class="big">{sessions}</div>sesiones</div>
<div class="card"><div class="big">{strong}</div>enlaces fuertes</div><div class="card"><div class="big">{clusters}</div>grupos candidatos</div></div>
<section><h2>Cuentas priorizadas</h2><p>Ordenadas por señales combinadas. Revise las columnas y el texto original antes de concluir.</p>
<div class="table"><table><thead><tr><th>Cuenta</th><th>ID</th><th>Puntaje</th><th>Nivel</th><th>Mensajes</th><th>Sesiones</th><th>Repetición</th><th>Coordinación</th></tr></thead>
<tbody>{account_rows}</tbody></table></div></section>
<section><h2>Enlaces de coordinación candidatos</h2><p>Mensajes iguales o casi iguales, de cuentas diferentes y próximos en el tiempo.</p>
<div class="table"><table><thead><tr><th>Cuenta A</th><th>Cuenta B</th><th>Puntaje</th><th>Eventos</th><th>Sesiones</th><th>Mediana desfase (s)</th><th>Ejemplos</th></tr></thead>
<tbody>{edge_rows}</tbody></table></div></section>
<section><h2>Método</h2><p>El puntaje combina repetición (25%), reutilización entre sesiones (15%), cadencia (20%), volumen relativo (10%) y coordinación textual-temporal (30%). No usa postura, nombres de figuras públicas ni sentimiento.</p></section>
</main></body></html>""".format(
        generated=esc(summary["generated_at_utc"]), interpretation=esc(summary["interpretation"]),
        messages=counts["messages"], accounts=counts["accounts"], sessions=counts["sessions"],
        strong=counts["strong_coordination_edges"], clusters=counts["coordination_clusters"],
        account_rows="".join(account_rows), edge_rows="".join(edge_rows),
    )
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(document)


def command_analyze(args: argparse.Namespace) -> int:
    paths = discover_jsonl(args.input)
    if not paths:
        print("No se encontraron archivos .jsonl en {}".format(args.input), file=sys.stderr)
        return 2
    if not (0.5 <= args.near_threshold <= 1.0):
        print("--near-threshold debe estar entre 0.5 y 1.0", file=sys.stderr)
        return 2
    messages = list(iter_messages(paths))
    if not messages:
        print("No se encontraron mensajes de chat compatibles", file=sys.stderr)
        return 2
    accounts, edges, clusters = analyze(messages, args.sync_window, args.near_threshold)
    os.makedirs(args.output, exist_ok=True)
    summary = make_summary(messages, accounts, edges, clusters, args)
    with open(os.path.join(args.output, "summary.json"), "w", encoding="utf-8") as handle:
        json.dump(summary, handle, ensure_ascii=False, indent=2)
    write_csv(os.path.join(args.output, "accounts.csv"), accounts, list(accounts[0].keys()))
    edge_fields = [
        "left", "right", "coordination_score", "evidence_events", "exact_events",
        "near_events", "sessions", "shared_texts", "weighted_events",
        "median_lag_seconds", "examples",
    ]
    write_csv(os.path.join(args.output, "coordination.csv"), edges, edge_fields)
    cluster_fields = ["cluster_id", "members", "member_ids", "strong_edges", "max_edge_score", "evidence_events"]
    write_csv(os.path.join(args.output, "clusters.csv"), clusters, cluster_fields)
    names = {account["author_id"]: account["author_name"] for account in accounts}
    write_html(os.path.join(args.output, "report.html"), summary, accounts, edges, names)
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command")
    analyze_parser = subparsers.add_parser("analyze", help="Analizar un archivo o directorio de chats JSONL")
    analyze_parser.add_argument("input", help="Archivo JSONL o directorio que contiene archivos JSONL")
    analyze_parser.add_argument("--output", default="results", help="Directorio de salida (predeterminado: results)")
    analyze_parser.add_argument("--sync-window", type=int, default=120, help="Ventana temporal en segundos (predeterminado: 120)")
    analyze_parser.add_argument("--near-threshold", type=float, default=0.84, help="Jaccard mínimo para texto casi igual (predeterminado: 0.84)")
    analyze_parser.set_defaults(func=command_analyze)
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if not hasattr(args, "func"):
        parser.print_help()
        return 2
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
