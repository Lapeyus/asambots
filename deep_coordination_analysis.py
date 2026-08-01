#!/usr/bin/env python3
"""Analisis multivista de narrativas y coordinacion en chats/comentarios.

Genera evidencia auditable; no atribuye bots, intencion ni afiliacion.
"""

import argparse
import csv
import hashlib
import json
import math
import os
import re
import statistics
import unicodedata
from collections import Counter, defaultdict
from itertools import combinations
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Sequence, Set, Tuple

from advanced_analysis import benjamini_hochberg
from bot_detector import (
    Message, STOPWORDS, URL_RE, discover_jsonl, filter_messages_to_catalog,
    iter_messages, load_catalog_video_ids, write_csv,
)
from campaign_evidence import LABELS, classify_messages, load_overrides, network_view


VERSION = "0.2.0"
LEXICAL_MIN_MEAN_SIMILARITY = 0.65
LEXICAL_MAX_ACCOUNTS_FOR_PAIRING = 50
HASHTAG_RE = re.compile(r"(?<!\w)#[\wáéíóúüñ]+", re.I)
DOMAIN_RE = re.compile(r"https?://(?:www\.)?([^/\s?#]+)", re.I)
TOKEN_RE = re.compile(r"[\wáéíóúüñ]+", re.I)
NARRATIVE_RULES = [
    ("continuidad", re.compile(r"\b(?:continuidad|continuar\w*|ocho\s+años|8\s+años)\b")),
    ("referendum", re.compile(r"\brefer[eé]ndum\w*\b")),
    ("poder_judicial", re.compile(r"\b(?:poder\s+judicial|magistrad\w*|oij|corte\s+suprema)\b")),
    ("anti_pln", re.compile(r"\b(?:pln|liberaci[oó]n\s+nacional|pericos?)\b")),
    ("anti_frente_amplio", re.compile(r"\b(?:frente\s+amplio|fraude\s+amplio|comunistas?)\b")),
    ("prensa", re.compile(r"\b(?:prensa|naci[oó]n|crhoy|teletica|canalla|sicarios?\s+digital)\b")),
    ("seguridad_narcotrafico", re.compile(r"\b(?:seguridad|narco\w*|sicari\w*|delincuen\w*|polic[ií]a|extradici[oó]n)\b")),
    ("obras_gestion", re.compile(r"\b(?:obras?|inaugur\w*|carretera\w*|puente\w*|escuela\w*|hospital\w*|acueducto\w*)\b")),
    ("elecciones_2030", re.compile(r"\b(?:2030|diputad\w*|votos?|elecci[oó]n\w*)\b")),
    ("religion_bendicion", re.compile(r"\b(?:dios|bendiga\w*|bendici[oó]n\w*|oraci[oó]n|cristo)\b")),
    ("corrupcion", re.compile(r"\b(?:corrupt\w*|rob\w*|ladron\w*|rata\w*|mafia|red\s+de\s+cuido)\b")),
]
PRIMARY_PRIORITY = [
    "continuidad", "referendum", "poder_judicial", "anti_pln", "anti_frente_amplio",
    "prensa", "seguridad_narcotrafico", "elecciones_2030", "obras_gestion",
    "religion_bendicion", "corrupcion", "apoyo_gobierno", "critica_gobierno",
    "ataque_oposicion_institucion", "apoyo_ambiguo",
]
CONCEPTS = {
    "presidenta": "gobierno", "presidente": "gobierno", "laura": "gobierno",
    "fernandez": "gobierno", "chaves": "gobierno", "chavez": "gobierno",
    "pilar": "gobierno", "gabinete": "gobierno", "gobierno": "gobierno",
    "apoyamos": "apoyo", "apoyo": "apoyo", "apoyar": "apoyo", "viva": "apoyo",
    "excelente": "apoyo", "gracias": "apoyo", "bendiga": "bendicion",
    "corruptos": "corrupcion", "corrupto": "corrupcion", "corrupta": "corrupcion",
    "ladrones": "robo", "ladron": "robo", "roban": "robo", "roba": "robo",
}


def narrative_tags(message: Message, label: str) -> Set[str]:
    tags = {name for name, pattern in NARRATIVE_RULES if pattern.search(message.normalized)}
    mapping = {
        "direct_support": "apoyo_gobierno", "possible_criticism": "critica_gobierno",
        "opposition_or_institution_attack": "ataque_oposicion_institucion",
        "ambiguous_support": "apoyo_ambiguo",
    }
    if label in mapping:
        tags.add(mapping[label])
    return tags


def primary_tag(tags: Set[str]) -> Optional[str]:
    return next((tag for tag in PRIMARY_PRIORITY if tag in tags), None)


def canonical_tokens(text: str) -> Set[str]:
    text = re.sub(r"(.)\1{2,}", r"\1\1", text)
    output = set()
    for token in TOKEN_RE.findall(text):
        token = unicodedata.normalize("NFKD", token).encode("ascii", "ignore").decode("ascii")
        if token in STOPWORDS or len(token) < 3:
            continue
        output.add(CONCEPTS.get(token, token))
    return output


def stable_hash(value: str, seed: int = 0) -> int:
    return int.from_bytes(
        hashlib.blake2b("{}|{}".format(seed, value).encode(), digest_size=8).digest(), "big"
    )


def minhash(tokens: Set[str]) -> Tuple[int, ...]:
    return tuple(min(stable_hash(token, seed) for token in tokens) for seed in range(24))


class UnionFind:
    def __init__(self, values: Iterable[str]):
        self.parent = {value: value for value in values}

    def find(self, value: str) -> str:
        root = value
        while self.parent[root] != root:
            root = self.parent[root]
        while self.parent[value] != value:
            parent = self.parent[value]; self.parent[value] = root; value = parent
        return root

    def union(self, left: str, right: str) -> None:
        left, right = self.find(left), self.find(right)
        if left != right:
            self.parent[right] = left


def semantic_outputs(messages, labels, tags):
    family_messages = defaultdict(list)
    tripartite = Counter()
    for message in messages:
        for tag in sorted(tags[id(message)]):
            family_messages[tag].append(message)
            tripartite[(message.author_id, tag, message.session)] += 1
    families = []
    for tag, items in family_messages.items():
        texts = Counter(item.normalized for item in items if item.normalized)
        representative = texts.most_common(1)[0] if texts else ("", 0)
        families.append({
            "narrative": tag, "messages": len(items),
            "accounts": len({item.author_id for item in items}),
            "sessions": len({item.session for item in items}),
            "chat_messages": sum(item.source == "chat" for item in items),
            "comment_messages": sum(item.source == "comment" for item in items),
            "direct_support_messages": sum(labels[id(item)] == "direct_support" for item in items),
            "representative_text": next(
                (item.text[:400] for item in items if item.normalized == representative[0]), ""
            ), "representative_count": representative[1],
        })
    families.sort(key=lambda row: (-row["messages"], row["narrative"]))
    names = {message.author_id: message.author_name for message in messages}
    grouped = {}
    for (author, tag, session), count in tripartite.items():
        row = grouped.setdefault((author, tag), {
            "author_id": author, "author_name": names.get(author, ""), "narrative": tag,
            "messages": 0, "sessions": 0,
        })
        row["messages"] += count; row["sessions"] += 1
    account_rows = sorted(
        grouped.values(),
        key=lambda row: (-row["messages"], -row["sessions"], row["author_id"], row["narrative"]),
    )
    return families, account_rows, tripartite


def lexical_clusters(messages, tags, max_bucket=120):
    texts, text_tag, token_sets = defaultdict(list), {}, {}
    for message in messages:
        tag = primary_tag(tags[id(message)])
        tokens = canonical_tokens(message.normalized)
        if not tag or len(message.normalized) < 20 or len(tokens) < 4:
            continue
        texts[message.normalized].append(message); text_tag[message.normalized] = tag
        token_sets[message.normalized] = tokens
    uf, buckets = UnionFind(texts), defaultdict(list)
    for text, tokens in token_sets.items():
        signature = minhash(tokens)
        for band in range(6):
            buckets[(text_tag[text], band, signature[band * 4:(band + 1) * 4])].append(text)
    compared = set()
    for bucket in buckets.values():
        if not 2 <= len(bucket) <= max_bucket:
            continue
        for left, right in combinations(bucket, 2):
            pair = tuple(sorted((left, right)))
            if pair in compared:
                continue
            compared.add(pair); a, b = token_sets[left], token_sets[right]
            if len(a & b) / float(len(a | b)) >= 0.68:
                uf.union(left, right)
    components = defaultdict(list)
    for text in texts:
        components[uf.find(text)].append(text)
    rows, cluster_authors, cluster_sessions = [], {}, {}
    for component in components.values():
        items = [item for text in component for item in texts[text]]
        authors = {item.author_id for item in items}
        if len(authors) < 2 or (len(component) == 1 and len(component[0]) < 30):
            continue
        cluster_id = "L{:05d}".format(len(rows) + 1)
        sessions = {item.session for item in items}
        representative = max(component, key=lambda text: (len(texts[text]), len(text)))
        rep_tokens = token_sets[representative]
        similarities = [
            len(rep_tokens & token_sets[text]) / float(len(rep_tokens | token_sets[text]))
            for text in component
        ]
        cluster_authors[cluster_id], cluster_sessions[cluster_id] = authors, sessions
        rows.append({
            "cluster_id": cluster_id, "narrative": text_tag[representative],
            "messages": len(items), "accounts": len(authors), "sessions": len(sessions),
            "text_variants": len(component), "mean_similarity": round(statistics.mean(similarities), 5),
            "representative_text": next(item.text[:500] for item in items if item.normalized == representative),
            "variants": " | ".join(sorted(component, key=lambda text: len(texts[text]), reverse=True)[:8]),
        })
    rows.sort(key=lambda row: (row["sessions"], row["accounts"], row["messages"]), reverse=True)
    return rows, cluster_authors, cluster_sessions


def shared_sequences(messages, tags, names):
    streams = defaultdict(list)
    for message in messages:
        if message.source == "chat" and message.time_precision == "exact" and primary_tag(tags[id(message)]):
            streams[(message.session, message.author_id)].append(message)
    occurrences, examples = defaultdict(set), defaultdict(list)
    for (session, author), items in streams.items():
        items.sort(key=lambda item: item.time_ms); sequence, text_sequence = [], []
        for item in items:
            tag = primary_tag(tags[id(item)])
            if sequence and sequence[-1] == tag:
                continue
            sequence.append(tag); text_sequence.append(item.text[:120])
        for index in range(len(sequence) - 2):
            ngram = tuple(sequence[index:index + 3])
            if len(set(ngram)) < 2:
                continue
            key = (session, ngram); occurrences[key].add(author)
            if len(examples[key]) < 3:
                examples[key].append(" -> ".join(text_sequence[index:index + 3]))
    rows, pair_stats = [], {}
    for (session, ngram), authors in occurrences.items():
        if not 2 <= len(authors) <= 80:
            continue
        sequence = " -> ".join(ngram)
        rows.append({
            "session": session, "sequence": sequence, "accounts": len(authors),
            "account_names": " | ".join(sorted(names.get(author, author) for author in authors)),
            "examples": " || ".join(examples[(session, ngram)]),
        })
        for pair in combinations(sorted(authors), 2):
            stat = pair_stats.setdefault(pair, {"events": 0, "sessions": set(), "sequences": set()})
            stat["events"] += 1; stat["sessions"].add(session); stat["sequences"].add(sequence)
    rows.sort(key=lambda row: (row["accounts"], row["session"]), reverse=True)
    return rows, pair_stats


def extract_resources(messages, names):
    resources = defaultdict(list)
    for message in messages:
        for url in URL_RE.findall(message.text):
            resources[("url", url.rstrip(".,);]").casefold())].append(message)
        for domain in DOMAIN_RE.findall(message.text):
            resources[("domain", domain.casefold())].append(message)
        for hashtag in HASHTAG_RE.findall(message.text):
            resources[("hashtag", hashtag.casefold())].append(message)
        emojis = "".join(char for char in message.text if unicodedata.category(char) in {"So", "Sk"})
        if len(emojis) >= 4:
            resources[("emoji_sequence", emojis[:80])].append(message)
    rows, pair_stats = [], {}
    for (kind, resource), items in resources.items():
        authors, sessions = {item.author_id for item in items}, {item.session for item in items}
        if not 2 <= len(authors) <= 100:
            continue
        rarity = 1.0 / math.log2(2.0 + len(authors))
        rows.append({
            "kind": kind, "resource": resource, "messages": len(items), "accounts": len(authors),
            "sessions": len(sessions), "rarity_weight": round(rarity, 6),
            "account_names": " | ".join(sorted(names.get(author, author) for author in authors)[:30]),
        })
        for pair in combinations(sorted(authors), 2):
            stat = pair_stats.setdefault(pair, {"count": 0, "sessions": set(), "examples": []})
            stat["count"] += 1; stat["sessions"].update(sessions)
            if len(stat["examples"]) < 4:
                stat["examples"].append("{}:{}".format(kind, resource[:120]))
    rows.sort(key=lambda row: (row["rarity_weight"], row["sessions"], row["messages"]), reverse=True)
    return rows, pair_stats


def reply_network(input_path, allowed, names):
    comments = []
    for path in discover_jsonl(input_path):
        if os.path.basename(path) == "catalog.jsonl":
            continue
        with open(path, encoding="utf-8") as handle:
            for line in handle:
                payload = json.loads(line)
                if payload.get("record_type") == "comment" and payload.get("video_id") in allowed:
                    comments.append(payload)
    parent_authors = {
        (row.get("video_id"), row.get("comment_id")): row.get("author_id")
        for row in comments if row.get("comment_id") and row.get("author_id")
    }
    directed = {}
    records_with_parent_id = 0
    records_with_matched_parent = 0
    for row in comments:
        if row.get("parent_id"):
            records_with_parent_id += 1
        child = row.get("author_id")
        parent = parent_authors.get((row.get("video_id"), row.get("parent_id")))
        if parent:
            records_with_matched_parent += 1
        if not child or not parent or child == parent:
            continue
        stat = directed.setdefault((child, parent), {"replies": 0, "videos": set(), "examples": []})
        stat["replies"] += 1; stat["videos"].add(row.get("video_id"))
        if len(stat["examples"]) < 3:
            stat["examples"].append(str(row.get("text") or "")[:180])
    rows, undirected = [], {}
    for (child, parent), stat in directed.items():
        reverse = directed.get((parent, child), {"replies": 0})
        rows.append({
            "reply_author_id": child, "reply_author_name": names.get(child, ""),
            "parent_author_id": parent, "parent_author_name": names.get(parent, ""),
            "replies": stat["replies"], "videos": len(stat["videos"]),
            "reciprocal_replies": reverse["replies"], "examples": " | ".join(stat["examples"]),
        })
        pair = tuple(sorted((child, parent)))
        merged = undirected.setdefault(pair, {"replies": 0, "videos": set(), "examples": []})
        merged["replies"] += stat["replies"]; merged["videos"].update(stat["videos"])
        merged["examples"].extend(stat["examples"][:2])
    rows.sort(key=lambda row: (row["videos"], row["replies"]), reverse=True)
    metadata = {
        "comment_records": len(comments),
        "records_with_parent_id": records_with_parent_id,
        "records_with_matched_parent": records_with_matched_parent,
        "network_available": records_with_parent_id > 0,
        "status": (
            "disponible" if records_with_parent_id > 0
            else "no_disponible_el_backend_no_entrego_parent_id"
        ),
    }
    return rows, undirected, metadata


def lexical_pair_stats(rows, cluster_authors, cluster_sessions):
    stats, by_id = {}, {row["cluster_id"]: row for row in rows}
    for cluster_id, authors in cluster_authors.items():
        row = by_id[cluster_id]
        # Las componentes de similitud son transitivas: A puede parecerse a B y
        # B a C sin que A se parezca a C. Solo usamos como evidencia de pares
        # clusters compactos y relativamente raros, aunque exportamos todos.
        if (
            len(authors) > LEXICAL_MAX_ACCOUNTS_FOR_PAIRING
            or row["mean_similarity"] < LEXICAL_MIN_MEAN_SIMILARITY
        ):
            continue
        for pair in combinations(sorted(authors), 2):
            stat = stats.setdefault(pair, {"clusters": 0, "sessions": set(), "examples": []})
            stat["clusters"] += 1; stat["sessions"].update(cluster_sessions[cluster_id])
            if len(stat["examples"]) < 4:
                stat["examples"].append(by_id[cluster_id]["representative_text"])
    return stats


def circular_permutation(messages, candidates, bin_seconds, min_messages):
    counts = Counter(m.author_id for m in messages if m.source == "chat" and m.time_precision == "exact")
    candidates = [pair for pair in candidates if counts[pair[0]] >= min_messages and counts[pair[1]] >= min_messages]
    relevant = {author for pair in candidates for author in pair}
    raw, active = defaultdict(set), defaultdict(set); divisor = bin_seconds * 1000
    for message in messages:
        if message.source != "chat" or message.time_precision != "exact":
            continue
        value = message.time_ms // divisor; active[message.session].add(value)
        if message.author_id in relevant:
            raw[(message.session, message.author_id)].add(value)
    profiles = {}
    for session, bins in active.items():
        ordinal = {value: index for index, value in enumerate(sorted(bins))}
        for author in relevant:
            values = raw.get((session, author))
            if values:
                profiles[(session, author)] = {ordinal[value] for value in values}
    rows = []
    for left, right in candidates:
        parts = [
            (a, b, len(active[session])) for session in active
            for a, b in [(profiles.get((session, left)), profiles.get((session, right)))]
            if a and b and len(active[session]) > 1
        ]
        if not parts:
            continue
        observed = sum(len(a & b) for a, b, _ in parts)
        overlap_sessions = sum(bool(a & b) for a, b, _ in parts)
        # Distribucion exacta: en cada sesion se ensayan todos los giros
        # circulares posibles y luego se convolucionan sus probabilidades.
        distribution = {0: 1.0}
        expected = 0.0
        shifts_evaluated = 0
        for a, b, population in parts:
            local = Counter(
                sum(((value + shift) % population) in a for value in b)
                for shift in range(population)
            )
            shifts_evaluated += population
            local_probabilities = {value: count / float(population) for value, count in local.items()}
            expected += sum(value * probability for value, probability in local_probabilities.items())
            combined = defaultdict(float)
            for accumulated, probability in distribution.items():
                for value, local_probability in local_probabilities.items():
                    combined[accumulated + value] += probability * local_probability
            distribution = dict(combined)
        p_value = sum(probability for value, probability in distribution.items() if value >= observed)
        rows.append({
            "left": left, "right": right, "messages_left": counts[left], "messages_right": counts[right],
            "shared_sessions": len(parts), "overlap_sessions": overlap_sessions,
            "observed_overlap_bins": observed, "permuted_expected_bins": round(expected, 6),
            "permutation_lift": round((observed + .5) / (expected + .5), 6),
            "p_value": p_value, "q_value": 1.0,
            "null_method": "todos_los_desplazamientos_circulares",
            "null_shifts_evaluated": shifts_evaluated,
        })
    for row, q_value in zip(rows, benjamini_hochberg([row["p_value"] for row in rows])):
        row["q_value"] = float("{:.12g}".format(q_value)); row["p_value"] = float("{:.12g}".format(row["p_value"]))
    rows.sort(key=lambda row: (row["q_value"], -row["permutation_lift"]))
    return rows


def temporal_overlap_evidence(messages, temporal_rows, names, bin_seconds):
    """Materializa los bins observados para poder auditar falsos positivos."""
    significant = {
        (row["left"], row["right"]): row
        for row in temporal_rows if row["q_value"] <= .05
    }
    if not significant:
        return []
    relevant = {author for pair in significant for author in pair}
    grouped = defaultdict(list)
    divisor = bin_seconds * 1000
    for message in messages:
        if (
            message.source == "chat" and message.time_precision == "exact"
            and message.author_id in relevant
        ):
            grouped[(message.session, message.time_ms // divisor, message.author_id)].append(message)
    rows = []
    sessions = {key[0] for key in grouped}
    for (left, right), timing in significant.items():
        for session in sessions:
            left_bins = {key[1] for key in grouped if key[0] == session and key[2] == left}
            right_bins = {key[1] for key in grouped if key[0] == session and key[2] == right}
            for bin_number in sorted(left_bins & right_bins):
                left_items = sorted(grouped[(session, bin_number, left)], key=lambda item: item.time_ms)
                right_items = sorted(grouped[(session, bin_number, right)], key=lambda item: item.time_ms)
                closest = min(
                    abs(a.time_ms - b.time_ms) for a in left_items for b in right_items
                )
                rows.append({
                    "left": left, "left_name": names.get(left, ""),
                    "right": right, "right_name": names.get(right, ""),
                    "session": session, "bin_number": bin_number,
                    "bin_start_seconds": bin_number * bin_seconds,
                    "closest_gap_seconds": round(closest / 1000.0, 3),
                    "left_times_seconds": " | ".join(str(round(item.time_ms / 1000.0, 3)) for item in left_items),
                    "right_times_seconds": " | ".join(str(round(item.time_ms / 1000.0, 3)) for item in right_items),
                    "left_texts": " | ".join(item.text[:300] for item in left_items),
                    "right_texts": " | ".join(item.text[:300] for item in right_items),
                    "p_value": timing["p_value"], "q_value": timing["q_value"],
                    "permutation_lift": timing["permutation_lift"],
                })
    rows.sort(key=lambda row: (row["q_value"], row["session"], row["bin_number"]))
    return rows


def political_controls(messages, labels, bin_seconds):
    rows = []
    for label in ("direct_support", "opposition_or_institution_attack", "possible_criticism", "ambiguous_support"):
        subset = [message for message in messages if labels[id(message)] == label]
        _, summary = network_view(subset, bin_seconds, 5)
        rows.append({"label": label, "messages": len(subset), "accounts": len({m.author_id for m in subset}), **summary})
    return rows


def combine_candidates(lexical, sequences, resources, replies, names, limit):
    rows = []
    for pair in set(lexical) | set(sequences) | set(resources) | set(replies):
        lex, seq, res, rep = lexical.get(pair, {}), sequences.get(pair, {}), resources.get(pair, {}), replies.get(pair, {})
        row = {
            "left": pair[0], "right": pair[1], "left_name": names.get(pair[0], ""), "right_name": names.get(pair[1], ""),
            "lexical_clusters": lex.get("clusters", 0), "lexical_sessions": len(lex.get("sessions", set())),
            "sequence_events": seq.get("events", 0), "sequence_sessions": len(seq.get("sessions", set())),
            "shared_resources": res.get("count", 0), "resource_sessions": len(res.get("sessions", set())),
            "reply_interactions": rep.get("replies", 0), "reply_videos": len(rep.get("videos", set())),
            "examples": " | ".join(
                lex.get("examples", [])[:2] + sorted(seq.get("sequences", set()))[:2]
                + res.get("examples", [])[:2] + rep.get("examples", [])[:2]
            )[:1200],
        }
        row["candidate_priority"] = round(
            3 * row["lexical_clusters"] + 2 * row["lexical_sessions"] + 2.5 * row["sequence_sessions"]
            + 2 * row["shared_resources"] + 1.5 * row["reply_videos"] + math.log1p(row["reply_interactions"]), 5
        )
        rows.append(row)
    rows.sort(key=lambda row: (-row["candidate_priority"], row["left"], row["right"]))
    return rows[:limit]


def integrate_temporal(candidates, temporal):
    temporal_map = {(row["left"], row["right"]): row for row in temporal}; strong = []
    fields = ("shared_sessions", "overlap_sessions", "observed_overlap_bins", "permuted_expected_bins", "permutation_lift", "p_value", "q_value", "null_method", "null_shifts_evaluated")
    for row in candidates:
        timing = temporal_map.get((row["left"], row["right"]), {})
        for field in fields:
            row[field] = timing.get(field, "")
        content = row["lexical_clusters"] >= 2 or row["sequence_sessions"] >= 2
        resource = row["shared_resources"] >= 1 and row["resource_sessions"] >= 2
        reply = row["reply_interactions"] >= 3 and row["reply_videos"] >= 2
        temporal_view = bool(timing) and timing["q_value"] <= .05 and timing["permutation_lift"] >= 1.5 and timing["overlap_sessions"] >= 2
        row.update({"content_view": int(content), "resource_view": int(resource), "reply_view": int(reply), "temporal_view": int(temporal_view)})
        row["evidence_views"] = sum((content, resource, reply, temporal_view))
        row["recurrence"] = max(row["lexical_sessions"], row["sequence_sessions"], row["resource_sessions"], row["reply_videos"], int(timing.get("overlap_sessions", 0) or 0))
        row["high_confidence"] = int(temporal_view and row["evidence_views"] >= 2 and row["recurrence"] >= 3)
        if row["high_confidence"]:
            strong.append(row)
    candidates.sort(key=lambda row: (row["high_confidence"], row["evidence_views"], row["candidate_priority"]), reverse=True)
    return candidates, strong


def components(edges):
    adjacency = defaultdict(set)
    for edge in edges:
        adjacency[edge["left"]].add(edge["right"]); adjacency[edge["right"]].add(edge["left"])
    rows, visited = [], set()
    for start in adjacency:
        if start in visited:
            continue
        stack, members = [start], []; visited.add(start)
        while stack:
            node = stack.pop(); members.append(node)
            for neighbor in adjacency[node]:
                if neighbor not in visited:
                    visited.add(neighbor); stack.append(neighbor)
        subset = [e for e in edges if e["left"] in members and e["right"] in members]
        rows.append({
            "group_id": len(rows) + 1, "members": len(members),
            "member_names": " | ".join(sorted({e["left_name"] for e in subset} | {e["right_name"] for e in subset})),
            "edges": len(subset), "max_views": max(e["evidence_views"] for e in subset),
            "max_recurrence": max(e["recurrence"] for e in subset),
        })
    return rows


def validation_sample(messages, labels, per_label):
    grouped = defaultdict(list)
    for message in messages:
        grouped[labels[id(message)]].append(message)
    rows = []
    for label in sorted(LABELS):
        ranked = sorted(grouped[label], key=lambda item: stable_hash(label + item.author_id + item.session + item.normalized))[:per_label]
        for item in ranked:
            rows.append({
                "proposed_label": label, "human_label": "", "reviewer_notes": "",
                "author_id": item.author_id, "author_name": item.author_name, "session": item.session,
                "source": item.source, "text": item.text, "normalized_text": item.normalized,
            })
    return rows


def safe_write(path, rows, fields):
    write_csv(path, rows, list(rows[0].keys()) if rows else fields)


def write_report(path, summary, families, candidates, temporal, names):
    c = summary["counts"]
    lines = [
        "# Análisis profundo de coincidencias y coordinación", "",
        "> Coincidencias para revisión; no prueban automatización ni intención.", "",
        "## Resumen", "",
        "- Registros válidos: {:,}".format(c["messages"]),
        "- Cuentas: {:,}".format(c["accounts"]),
        "- Familias narrativas: {:,}".format(c["narrative_families"]),
        "- Clusters léxicos: {:,}".format(c["lexical_clusters"]),
        "- Secuencias compartidas: {:,}".format(c["shared_sequences"]),
        "- Recursos compartidos: {:,}".format(c["shared_resources"]),
        "- Relaciones de respuesta: {:,}".format(c["reply_edges"]),
        "- Disponibilidad de respuestas: {}".format(summary["reply_network"]["status"]),
        "- Pares permutados: {:,}".format(c["permutation_pairs"]),
        "- Pares significativos: {:,}".format(c["permutation_significant"]),
        "- Bins observados exportados para auditoría: {:,}".format(c["temporal_overlap_evidence_rows"]),
        "- Grupos de alta confianza: {:,}".format(c["high_confidence_groups"]), "",
        "## Evaluación", "", "**{}**".format(summary["assessment"]), "",
        "## Narrativas principales", "",
        "| Narrativa | Mensajes | Cuentas | Sesiones | Ejemplo |", "|---|---:|---:|---:|---|",
    ]
    for row in families[:15]:
        lines.append("| {} | {} | {} | {} | {} |".format(row["narrative"], row["messages"], row["accounts"], row["sessions"], row["representative_text"].replace("|", "\\|")[:130]))
    significant = [row for row in temporal if row["q_value"] <= .05]
    lines += ["", "## Señales temporales significativas", ""]
    if significant:
        lines += [
            "| Cuenta A | Cuenta B | Sesiones coincidentes | Bins observados | Esperados | Lift | p | q |",
            "|---|---|---:|---:|---:|---:|---:|---:|",
        ]
        for row in significant:
            lines.append("| {} | {} | {} | {} | {} | {} | {} | {} |".format(
                names.get(row["left"], row["left"]).replace("|", "\\|"),
                names.get(row["right"], row["right"]).replace("|", "\\|"),
                row["overlap_sessions"], row["observed_overlap_bins"],
                row["permuted_expected_bins"], row["permutation_lift"],
                row["p_value"], row["q_value"],
            ))
        lines += [
            "", "Los textos y tiempos de cada bin están en `temporal_overlap_evidence.csv`; la sincronía aislada puede reflejar una reacción común al contenido transmitido.",
        ]
    else:
        lines.append("Ningún par sobrevivió la corrección por comparaciones múltiples.")
    lines += ["", "## Pares con más evidencia", "",
              "| Cuenta A | Cuenta B | Vistas | Recurrencia | Léxico | Secuencias | Recursos | Respuestas | q permutación |",
              "|---|---|---:|---:|---:|---:|---:|---:|---:|"]
    for row in candidates[:20]:
        lines.append("| {} | {} | {} | {} | {} | {} | {} | {} | {} |".format(
            row["left_name"].replace("|", "\\|"), row["right_name"].replace("|", "\\|"), row["evidence_views"], row["recurrence"], row["lexical_clusters"], row["sequence_events"], row["shared_resources"], row["reply_interactions"], row["q_value"]))
    lines += [
        "", "Alta confianza exige q≤0,05 por permutación, lift≥1,5, solapamiento en ≥2 sesiones, otra vista y recurrencia total en ≥3 sesiones.",
        "", "## Límites de interpretación", "",
        "- Una reacción simultánea a la misma intervención del video puede producir sincronía sin coordinación entre cuentas.",
        "- La afinidad política, una narrativa compartida o un emoji común no demuestran automatización ni organización.",
        "- La red de respuestas solo puede calcularse si el backend conserva `parent_id`; su disponibilidad se declara arriba.",
        "- Las etiquetas políticas requieren validación humana con `human_validation_sample.csv`.", "",
    ]
    Path(path).write_text("\n".join(lines), encoding="utf-8")


def run(args):
    messages, excluded = filter_messages_to_catalog(list(iter_messages(discover_jsonl(args.input))), args.input)
    if not messages:
        raise SystemExit("No se encontraron registros validos")
    allowed = load_catalog_video_ids(args.input) or {m.session.removesuffix("__comments") for m in messages}
    labels, _ = classify_messages(messages, load_overrides(args.labels))
    tags = {id(message): narrative_tags(message, labels[id(message)]) for message in messages}
    names = {message.author_id: message.author_name for message in messages}
    families, account_narratives, tripartite = semantic_outputs(messages, labels, tags)
    lexical_rows, cluster_authors, cluster_sessions = lexical_clusters(messages, tags)
    lexical_pairs = lexical_pair_stats(lexical_rows, cluster_authors, cluster_sessions)
    sequence_rows, sequence_pairs = shared_sequences(messages, tags, names)
    resource_rows, resource_pairs = extract_resources(messages, names)
    reply_rows, reply_pairs, reply_metadata = reply_network(args.input, allowed, names)
    candidates = combine_candidates(lexical_pairs, sequence_pairs, resource_pairs, reply_pairs, names, args.max_candidate_pairs)
    temporal = circular_permutation(messages, [(r["left"], r["right"]) for r in candidates], args.bin_seconds, args.min_account_messages)
    temporal_evidence = temporal_overlap_evidence(messages, temporal, names, args.bin_seconds)
    candidates, strong = integrate_temporal(candidates, temporal)
    groups = components(strong)
    controls = political_controls(messages, labels, args.bin_seconds)
    sample = validation_sample(messages, labels, args.validation_per_label)
    output = Path(args.output); output.mkdir(parents=True, exist_ok=True)
    safe_write(output / "narrative_clusters.csv", families, ["narrative", "messages", "accounts", "sessions"])
    safe_write(output / "lexical_variants.csv", lexical_rows, ["cluster_id", "narrative", "messages", "accounts", "sessions"])
    safe_write(output / "account_narrative_network.csv", account_narratives, ["author_id", "author_name", "narrative", "messages", "sessions"])
    trip_rows = [{"author_id": a, "author_name": names.get(a, ""), "narrative": n, "session": s, "messages": count} for (a, n, s), count in tripartite.items()]
    trip_rows.sort(key=lambda row: (-row["messages"], row["author_id"], row["narrative"], row["session"]))
    safe_write(output / "account_narrative_sessions.csv", trip_rows, ["author_id", "author_name", "narrative", "session", "messages"])
    safe_write(output / "shared_sequences.csv", sequence_rows, ["session", "sequence", "accounts", "account_names", "examples"])
    safe_write(output / "shared_resources.csv", resource_rows, ["kind", "resource", "messages", "accounts", "sessions", "rarity_weight", "account_names"])
    safe_write(output / "reply_network.csv", reply_rows, ["reply_author_id", "reply_author_name", "parent_author_id", "parent_author_name", "replies", "videos", "reciprocal_replies", "examples"])
    safe_write(output / "temporal_permutation.csv", temporal, ["left", "right", "p_value", "q_value"])
    safe_write(output / "temporal_overlap_evidence.csv", temporal_evidence, [
        "left", "left_name", "right", "right_name", "session", "bin_number",
        "bin_start_seconds", "closest_gap_seconds", "left_times_seconds",
        "right_times_seconds", "left_texts", "right_texts", "p_value",
        "q_value", "permutation_lift",
    ])
    safe_write(output / "political_controls.csv", controls, ["label", "messages", "accounts"])
    safe_write(output / "coordination_candidates.csv", candidates, ["left", "right", "evidence_views", "high_confidence"])
    safe_write(output / "high_confidence_groups.csv", groups, ["group_id", "members", "member_names", "edges", "max_views", "max_recurrence"])
    safe_write(output / "human_validation_sample.csv", sample, ["proposed_label", "human_label", "reviewer_notes", "text"])
    with (output / "candidate_evidence.jsonl").open("w", encoding="utf-8") as handle:
        for row in candidates[:250]:
            handle.write(json.dumps(row, ensure_ascii=False) + "\n")
    lexical_pairing_clusters = sum(
        row["accounts"] <= LEXICAL_MAX_ACCOUNTS_FOR_PAIRING
        and row["mean_similarity"] >= LEXICAL_MIN_MEAN_SIMILARITY
        for row in lexical_rows
    )
    permutation_significant = sum(row["q_value"] <= .05 for row in temporal)
    summary = {"tool_version": VERSION, "input": os.path.abspath(args.input), "counts": {
        "messages": len(messages), "accounts": len({m.author_id for m in messages}), "excluded_outside_catalog": excluded,
        "narrative_families": len(families), "lexical_clusters": len(lexical_rows),
        "lexical_clusters_used_for_pairing": lexical_pairing_clusters,
        "shared_sequences": len(sequence_rows),
        "shared_resources": len(resource_rows), "reply_edges": len(reply_rows), "candidate_pairs": len(candidates),
        "permutation_pairs": len(temporal), "permutation_significant": permutation_significant,
        "temporal_overlap_evidence_rows": len(temporal_evidence),
        "high_confidence_edges": len(strong), "high_confidence_groups": len(groups),
    }, "reply_network": reply_metadata,
        "parameters": {"temporal_null": "distribucion exacta de todos los desplazamientos circulares por sesion", "bin_seconds": args.bin_seconds, "min_account_messages": args.min_account_messages, "max_candidate_pairs": args.max_candidate_pairs, "lexical_jaccard": .68, "lexical_min_mean_similarity_for_pairing": LEXICAL_MIN_MEAN_SIMILARITY, "lexical_max_accounts_for_pairing": LEXICAL_MAX_ACCOUNTS_FOR_PAIRING, "sequence_length": 3, "high_confidence_rule": "temporal q<=.05, lift>=1.5, overlap>=2, otra vista, recurrencia>=3"},
        "assessment": (
            "Hay grupos candidatos de alta confianza que requieren auditoría humana."
            if groups else
            "Hay {} par(es) con sincronía temporal significativa, pero ninguno tiene otra vista independiente suficiente; no aparecieron grupos de alta confianza.".format(permutation_significant)
            if permutation_significant else
            "No aparecieron grupos que cumplan temporalidad permutada, otra vista independiente y recurrencia."
        )}
    (output / "deep_summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    write_report(output / "deep_report.md", summary, families, candidates, temporal, names)
    print(json.dumps(summary, ensure_ascii=False, indent=2)); return 0


def build_parser():
    parser = argparse.ArgumentParser(description=__doc__); parser.add_argument("input")
    parser.add_argument("--output", default="deep_analysis"); parser.add_argument("--labels")
    parser.add_argument("--simulations", type=int, default=500, help=argparse.SUPPRESS)
    parser.add_argument("--bin-seconds", type=int, default=30)
    parser.add_argument("--min-account-messages", type=int, default=10); parser.add_argument("--max-candidate-pairs", type=int, default=1500)
    parser.add_argument("--validation-per-label", type=int, default=100); return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    return run(build_parser().parse_args(argv))


if __name__ == "__main__":
    raise SystemExit(main())
