#!/usr/bin/env python3
"""Analisis avanzado, reproducible y no supervisado de chats de YouTube.

Esta segunda capa complementa ``bot_detector.py`` con cuatro ideas tomadas de
la literatura: huellas temporales individuales, sincronizacion estadistica,
lockstep textual y redes multivista. No produce probabilidades de que una
cuenta sea un bot; produce evidencia priorizada para revision humana.

Las referencias metodologicas y sus URL se escriben tambien en cada salida
``methodology.json`` para que una ejecucion futura conserve su procedencia.
"""

import argparse
import csv
import hashlib
import json
import math
import os
import statistics
import zlib
from collections import Counter, defaultdict
from itertools import combinations
from typing import Dict, Iterable, List, Optional, Sequence, Set, Tuple

from bot_detector import (
    Message,
    analyze as base_analyze,
    clamp,
    discover_jsonl,
    filter_messages_to_catalog,
    iter_messages,
    normalize_text,
    write_csv,
)


VERSION = "0.3.0"

# Fuentes primarias. El metodo de este archivo es una adaptacion transparente,
# no una reproduccion exacta de los modelos (varios requieren datos o etiquetas
# que YouTube Chat no ofrece).
REFERENCES = [
    {
        "key": "copycatch",
        "citation": "Beutel et al. (WWW 2013), CopyCatch: Stopping Group Attacks by Spotting Lockstep Behavior in Social Networks",
        "url": "https://alexbeutel.com/papers/www2013_copycatch.pdf",
        "used_for": "Repeticion de acciones compartidas y redes bipartitas de comportamiento lockstep.",
    },
    {
        "key": "debot",
        "citation": "Chavoshi, Hamooni & Mueen (ICDM 2016), DeBot: Twitter Bot Detection via Warped Correlation",
        "url": "https://doi.org/10.1109/ICDM.2016.0096",
        "used_for": "Correlacion temporal entre cuentas sin etiquetas supervisadas.",
    },
    {
        "key": "chat_entropy",
        "citation": "Gianvecchio et al. (USENIX Security 2008), Measurement and Classification of Humans and Bots in Internet Chat",
        "url": "https://www.usenix.org/event/sec08/tech/full_papers/gianvecchio/gianvecchio_html/",
        "used_for": "Entropia de intervalos entre mensajes y de longitudes en chat.",
    },
    {
        "key": "coordination_networks",
        "citation": "Pacheco et al. (ICWSM 2021), Uncovering Coordinated Networks on Social Media: Methods and Case Studies",
        "url": "https://arxiv.org/abs/2001.05658",
        "used_for": "Redes basadas en trazas de comportamiento compartidas.",
    },
    {
        "key": "synchronized_action",
        "citation": "Magelinski, Ng & Carley (2021), A Synchronized Action Framework for Responsible Detection of Coordination",
        "url": "https://arxiv.org/abs/2105.07454",
        "used_for": "Separacion por vistas y exigencia de evidencia multivista.",
    },
    {
        "key": "multiplex_time",
        "citation": "Iannucci et al. (ICWSM 2026), Detecting Coordinated Activities Through Temporal, Multiplex, and Collaborative Analysis",
        "url": "https://doi.org/10.1609/icwsm.v20i1.42682",
        "used_for": "Agregacion temporal de evidencia repetida en multiples modalidades.",
    },
    {
        "key": "fdr",
        "citation": "Benjamini & Hochberg (1995), Controlling the False Discovery Rate",
        "url": "https://doi.org/10.1111/j.2517-6161.1995.tb02031.x",
        "used_for": "Correccion de pruebas simultaneas por tasa de falsos descubrimientos.",
    },
]


def shannon_entropy_normalized(values: Sequence[object]) -> float:
    """Entropia de Shannon dividida por log2(n); 0=constante, 1=todos unicos."""
    n = len(values)
    if n <= 1:
        return 0.0
    counts = Counter(values)
    entropy = -sum((count / n) * math.log2(count / n) for count in counts.values())
    return entropy / math.log2(n)


def cosine(left: Sequence[float], right: Sequence[float]) -> float:
    numerator = sum(a * b for a, b in zip(left, right))
    denominator = math.sqrt(sum(a * a for a in left) * sum(b * b for b in right))
    return numerator / denominator if denominator else 0.0


def pairwise_mean(values: Sequence[Sequence[float]]) -> Optional[float]:
    similarities = [cosine(left, right) for left, right in combinations(values, 2)]
    return statistics.mean(similarities) if similarities else None


def log_interval_bin(seconds: float) -> int:
    """Bins log2 estables: <1s, 1-2s, 2-4s, ..."""
    if seconds < 1:
        return 0
    return 1 + int(math.log(seconds, 2))


def session_shape(items: Sequence[Message], segments: int = 12) -> List[float]:
    if not items:
        return [0.0] * segments
    start = min(item.time_ms for item in items)
    end = max(item.time_ms for item in items)
    span = max(1, end - start)
    histogram = [0.0] * segments
    for item in items:
        position = min(segments - 1, int((item.time_ms - start) * segments / span))
        histogram[position] += 1.0
    total = sum(histogram)
    return [value / total for value in histogram]


def account_fingerprints(messages: Sequence[Message]) -> List[dict]:
    by_author: Dict[str, List[Message]] = defaultdict(list)
    for message in messages:
        by_author[message.author_id].append(message)
    rows = []
    for author, items in by_author.items():
        items.sort(key=lambda item: (item.session, item.time_ms))
        source_counts = Counter(item.source for item in items)
        usable_texts = [item.normalized for item in items if item.normalized]
        raw = "\n".join(usable_texts).encode("utf-8")
        compression_ratio = len(zlib.compress(raw, 9)) / float(len(raw)) if len(raw) >= 100 else None
        lengths = [len(item.normalized) for item in items if item.normalized]
        quantized_lengths = [length // 5 for length in lengths]
        timed_items = [item for item in items if item.time_precision == "exact"]
        session_items: Dict[str, List[Message]] = defaultdict(list)
        for item in timed_items:
            session_items[item.session].append(item)

        intervals = []
        consecutive_duplicates = 0
        possible_consecutive = 0
        for session_messages in session_items.values():
            session_messages.sort(key=lambda item: item.time_ms)
            for prior, current in zip(session_messages, session_messages[1:]):
                intervals.append(max(0.0, (current.time_ms - prior.time_ms) / 1000.0))
                possible_consecutive += 1
                if prior.normalized and prior.normalized == current.normalized:
                    consecutive_duplicates += 1

        interval_bins = [log_interval_bin(value) for value in intervals]
        rounded_intervals = [round(value) for value in intervals]
        if intervals:
            mean_interval = statistics.mean(intervals)
            stdev_interval = statistics.pstdev(intervals)
            burstiness = (
                (stdev_interval - mean_interval) / (stdev_interval + mean_interval)
                if stdev_interval + mean_interval else -1.0
            )
            median_interval = statistics.median(intervals)
            deviations = [abs(value - median_interval) for value in intervals]
            robust_cv = statistics.median(deviations) / max(1.0, median_interval)
        else:
            burstiness = None
            median_interval = None
            robust_cv = None
        shapes = [session_shape(session_messages) for session_messages in session_items.values()]
        shape_similarity = pairwise_mean(shapes)
        text_counts = Counter(usable_texts)
        repeated_messages = sum(count for count in text_counts.values() if count >= 2)
        author_names = Counter(item.author_name for item in items if item.author_name)
        row = {
            "author_id": author,
            "author_name": author_names.most_common(1)[0][0] if author_names else "",
            "messages": len(items),
            "sessions": len({item.session for item in items}),
            "chat_messages": source_counts.get("chat", 0),
            "comment_messages": source_counts.get("comment", 0),
            "exact_time_messages": len(timed_items),
            "source_types": len(source_counts),
            "text_entropy": round(shannon_entropy_normalized(usable_texts), 5),
            "message_length_entropy": round(shannon_entropy_normalized(quantized_lengths), 5),
            "compression_ratio": round(compression_ratio, 5) if compression_ratio is not None else "",
            "repeated_message_fraction": round(repeated_messages / float(max(1, len(usable_texts))), 5),
            "consecutive_duplicate_fraction": round(consecutive_duplicates / float(max(1, possible_consecutive)), 5),
            "interval_entropy": round(shannon_entropy_normalized(interval_bins), 5),
            "interval_repeat_fraction": round(1.0 - len(set(rounded_intervals)) / float(max(1, len(rounded_intervals))), 5),
            "median_interval_seconds": round(median_interval, 3) if median_interval is not None else "",
            "robust_interval_cv": round(robust_cv, 5) if robust_cv is not None else "",
            "burstiness": round(burstiness, 5) if burstiness is not None else "",
            "session_shape_similarity": round(shape_similarity, 5) if shape_similarity is not None else "",
        }
        rows.append(row)
    return rows


def log_comb(n: int, k: int) -> float:
    if k < 0 or k > n:
        return float("-inf")
    return math.lgamma(n + 1) - math.lgamma(k + 1) - math.lgamma(n - k + 1)


def hypergeom_survival(observed: int, population: int, success_states: int, draws: int) -> float:
    """P[X >= observed] para una hipergeometrica, calculada en espacio log."""
    lower = max(0, draws - (population - success_states))
    upper = min(draws, success_states)
    if observed <= lower:
        return 1.0
    if observed > upper:
        return 0.0
    denominator = log_comb(population, draws)
    logs = [
        log_comb(success_states, value)
        + log_comb(population - success_states, draws - value)
        - denominator
        for value in range(observed, upper + 1)
    ]
    maximum = max(logs)
    return min(1.0, math.exp(maximum) * sum(math.exp(value - maximum) for value in logs))


def fisher_combined_p(p_values: Sequence[float]) -> float:
    """Cola chi-cuadrado de Fisher; df=2k permite una forma cerrada."""
    if not p_values:
        return 1.0
    half_statistic = -sum(math.log(max(1e-300, min(1.0, value))) for value in p_values)
    if half_statistic > 745:
        return 1e-300
    term = 1.0
    series = 1.0
    for index in range(1, len(p_values)):
        term *= half_statistic / index
        series += term
    return max(1e-300, min(1.0, math.exp(-half_statistic) * series))


def benjamini_hochberg(p_values: Sequence[float]) -> List[float]:
    """Q-values BH manteniendo el orden original."""
    count = len(p_values)
    if not count:
        return []
    ordered = sorted(enumerate(p_values), key=lambda pair: pair[1])
    adjusted = [1.0] * count
    running = 1.0
    for reverse_index in range(count - 1, -1, -1):
        original_index, value = ordered[reverse_index]
        rank = reverse_index + 1
        running = min(running, value * count / rank)
        adjusted[original_index] = min(1.0, running)
    return adjusted


def temporal_synchrony(
    messages: Sequence[Message], bin_seconds: int = 30, min_account_messages: int = 10
) -> Tuple[List[dict], dict]:
    """Prueba sincronizacion de pares contra actividad uniforme en bins activos.

    La unidad de tiempo se comprime a bins en los que hubo algun chat. Esto evita
    que recesos largos vuelvan artificialmente significativa una coincidencia.
    Los q-values son exploratorios porque las reacciones a un mismo debate no son
    completamente independientes.
    """
    messages = [message for message in messages if message.time_precision == "exact"]
    counts = Counter(message.author_id for message in messages)
    eligible = {author for author, count in counts.items() if count >= min_account_messages}
    raw_bins_by_session: Dict[str, Dict[str, Set[int]]] = defaultdict(lambda: defaultdict(set))
    occupancy_raw: Dict[str, Dict[int, Set[str]]] = defaultdict(lambda: defaultdict(set))
    divisor = bin_seconds * 1000
    for message in messages:
        if message.author_id not in eligible:
            continue
        raw_bin = message.time_ms // divisor
        raw_bins_by_session[message.session][message.author_id].add(raw_bin)
        occupancy_raw[message.session][raw_bin].add(message.author_id)

    pair_parts: Dict[Tuple[str, str], List[dict]] = defaultdict(list)
    for session, profiles in raw_bins_by_session.items():
        active_raw_bins = sorted(occupancy_raw[session])
        ordinal = {raw_bin: index for index, raw_bin in enumerate(active_raw_bins)}
        converted = {
            author: {ordinal[value] for value in bins}
            for author, bins in profiles.items()
        }
        population = len(active_raw_bins)
        if population < 2:
            continue
        authors = sorted(converted)
        for left, right in combinations(authors, 2):
            left_bins = converted[left]
            right_bins = converted[right]
            overlap_bins = left_bins & right_bins
            observed = len(overlap_bins)
            expected = len(left_bins) * len(right_bins) / float(population)
            p_value = hypergeom_survival(observed, population, len(left_bins), len(right_bins))
            busy_weight = sum(
                1.0 / math.log2(2.0 + len(occupancy_raw[session][active_raw_bins[index]]))
                for index in overlap_bins
            )
            pair_parts[(left, right)].append({
                "session": session,
                "observed": observed,
                "expected": expected,
                "union": len(left_bins | right_bins),
                "p": p_value,
                "busy_weight": busy_weight,
            })

    rows = []
    all_p_values = []
    for (left, right), parts in pair_parts.items():
        observed = sum(part["observed"] for part in parts)
        expected = sum(part["expected"] for part in parts)
        overlap_sessions = sum(part["observed"] > 0 for part in parts)
        combined_p = fisher_combined_p([part["p"] for part in parts])
        all_p_values.append(combined_p)
        rows.append({
            "left": left,
            "right": right,
            "p_value": combined_p,
            "q_value": 1.0,
            "shared_sessions": len(parts),
            "overlap_sessions": overlap_sessions,
            "shared_active_bins": observed,
            "expected_shared_bins": expected,
            "synchrony_lift": (observed + 0.5) / (expected + 0.5),
            "temporal_jaccard": observed / float(max(1, sum(part["union"] for part in parts))),
            "busy_adjusted_overlap": sum(part["busy_weight"] for part in parts),
        })
    q_values = benjamini_hochberg(all_p_values)
    for row, q_value in zip(rows, q_values):
        row["q_value"] = q_value
        significance = clamp((-math.log10(max(q_value, 1e-300)) - 1.301) / 4.0)
        effect = clamp((row["synchrony_lift"] - 1.0) / 2.0)
        recurrence = clamp(row["overlap_sessions"] / 4.0)
        support = clamp(row["shared_active_bins"] / 12.0)
        row["temporal_score"] = 100.0 * (
            0.40 * significance + 0.30 * effect + 0.20 * recurrence + 0.10 * support
        )
        for field in (
            "p_value", "q_value", "expected_shared_bins", "synchrony_lift",
            "temporal_jaccard", "busy_adjusted_overlap", "temporal_score",
        ):
            row[field] = float("{:.12g}".format(row[field]))
    rows.sort(key=lambda row: (row["temporal_score"], row["shared_active_bins"]), reverse=True)
    metadata = {
        "eligible_accounts": len(eligible),
        "tested_pairs": len(rows),
        "null_model": "Seleccion uniforme e independiente entre bins de chat activos, por sesion.",
        "multiple_testing": "Benjamini-Hochberg sobre todos los pares elegibles que comparten sesion.",
        "excluded_coarse_timestamps": "Los registros con fecha sin hora exacta no participan en sincronizacion temporal.",
    }
    return rows, metadata


def shared_script_edges(messages: Sequence[Message], min_characters: int = 30, min_tokens: int = 6) -> List[dict]:
    by_text: Dict[str, List[Message]] = defaultdict(list)
    all_authors = {message.author_id for message in messages}
    for message in messages:
        if len(message.normalized) >= min_characters and len(message.normalized.split()) >= min_tokens:
            by_text[message.normalized].append(message)
    pairs: Dict[Tuple[str, str], dict] = {}
    denominator = math.log1p(max(2, len(all_authors)))
    for text, occurrences in by_text.items():
        by_author: Dict[str, List[Message]] = defaultdict(list)
        for occurrence in occurrences:
            by_author[occurrence.author_id].append(occurrence)
        if len(by_author) < 2:
            continue
        idf = math.log((1.0 + len(all_authors)) / (1.0 + len(by_author))) / denominator
        for left, right in combinations(sorted(by_author), 2):
            left_items = by_author[left]
            right_items = by_author[right]
            key = (left, right)
            row = pairs.setdefault(key, {
                "left": left,
                "right": right,
                "shared_templates": 0,
                "weighted_templates": 0.0,
                "template_sessions": set(),
                "examples": [],
            })
            recurrence = min(len(left_items), len(right_items))
            row["shared_templates"] += 1
            row["weighted_templates"] += idf * (1.0 + math.log1p(recurrence))
            row["template_sessions"].update(item.session for item in left_items + right_items)
            if len(row["examples"]) < 3:
                row["examples"].append(occurrences[0].text[:240])
    rows = []
    for row in pairs.values():
        row["script_score"] = round(100.0 * (1.0 - math.exp(-row["weighted_templates"] / 2.5)), 8)
        row["weighted_templates"] = round(row["weighted_templates"], 8)
        row["template_sessions"] = len(row["template_sessions"])
        row["examples"] = " | ".join(row["examples"])
        rows.append(row)
    rows.sort(key=lambda row: (row["script_score"], row["shared_templates"]), reverse=True)
    return rows


def multiview_edges(base_edges: Sequence[dict], temporal_rows: Sequence[dict], script_rows: Sequence[dict]) -> List[dict]:
    merged: Dict[Tuple[str, str], dict] = {}

    def get(left: str, right: str) -> dict:
        key = tuple(sorted((left, right)))
        return merged.setdefault(key, {
            "left": key[0], "right": key[1],
            "content_score": 0.0, "content_events": 0,
            "temporal_score": 0.0, "temporal_q_value": 1.0,
            "synchrony_lift": 0.0, "shared_active_bins": 0, "temporal_overlap_sessions": 0,
            "script_score": 0.0, "shared_templates": 0,
            "evidence_examples": "",
        })

    for edge in base_edges:
        row = get(edge["left"], edge["right"])
        row["content_score"] = edge["coordination_score"]
        row["content_events"] = edge["evidence_events"]
        row["evidence_examples"] = edge["examples"]
    for edge in temporal_rows:
        # Se conservan solamente pares con algun solapamiento; todos participaron
        # en el calculo FDR antes de este filtro.
        if edge["shared_active_bins"] == 0:
            continue
        row = get(edge["left"], edge["right"])
        row["temporal_score"] = edge["temporal_score"]
        row["temporal_q_value"] = edge["q_value"]
        row["synchrony_lift"] = edge["synchrony_lift"]
        row["shared_active_bins"] = edge["shared_active_bins"]
        row["temporal_overlap_sessions"] = edge["overlap_sessions"]
    for edge in script_rows:
        row = get(edge["left"], edge["right"])
        row["script_score"] = edge["script_score"]
        row["shared_templates"] = edge["shared_templates"]
        if not row["evidence_examples"]:
            row["evidence_examples"] = edge["examples"]

    rows = []
    for row in merged.values():
        content_lockstep = row["content_score"] >= 10.0 and row["content_events"] >= 2
        temporal_view = row["temporal_q_value"] <= 0.05 and row["temporal_score"] >= 25.0
        # Lockstep textual y plantillas son evidencia relacionada, no dos vistas
        # independientes. Se agrupan para impedir doble conteo.
        content_family_view = content_lockstep or row["script_score"] >= 25.0
        view_count = sum((content_family_view, temporal_view))
        combined = (
            0.40 * row["content_score"]
            + 0.40 * row["temporal_score"]
            + 0.20 * row["script_score"]
        )
        if view_count >= 2:
            combined = min(100.0, combined + 12.0)
        row["evidence_views"] = view_count
        row["multiview_score"] = round(combined, 4)
        if view_count >= 2 and row["temporal_overlap_sessions"] >= 2 and combined >= 45:
            row["review_level"] = "multivista_fuerte"
        elif view_count >= 2:
            row["review_level"] = "multivista_una_sesion"
        elif max(row["content_score"], row["temporal_score"], row["script_score"]) >= 45:
            row["review_level"] = "una_vista_fuerte"
        else:
            row["review_level"] = "debil"
        rows.append(row)
    rows.sort(key=lambda row: (row["multiview_score"], row["evidence_views"]), reverse=True)
    return rows


def score_fingerprints(fingerprints: List[dict], base_accounts: Sequence[dict], edges: Sequence[dict]) -> List[dict]:
    base = {row["author_id"]: row for row in base_accounts}
    edge_scores: Dict[str, float] = defaultdict(float)
    edge_views: Dict[str, int] = defaultdict(int)
    for edge in edges:
        for author in (edge["left"], edge["right"]):
            edge_scores[author] = max(edge_scores[author], edge["multiview_score"])
            edge_views[author] = max(edge_views[author], edge["evidence_views"])
    for row in fingerprints:
        n = row["messages"]
        text_signal = 1.0 - float(row["text_entropy"])
        timing_signal = 1.0 - float(row["interval_entropy"]) if n >= 10 else 0.0
        repeat_timing = float(row["interval_repeat_fraction"]) if row["interval_repeat_fraction"] != "" else 0.0
        robust_cv = float(row["robust_interval_cv"]) if row["robust_interval_cv"] != "" else 1.0
        periodic_signal = clamp((0.50 - robust_cv) / 0.50) * clamp((repeat_timing - 0.15) / 0.70)
        compression = float(row["compression_ratio"]) if row["compression_ratio"] != "" else 1.0
        compression_signal = clamp((0.65 - compression) / 0.50)
        shape = float(row["session_shape_similarity"]) if row["session_shape_similarity"] != "" else 0.0
        shape_signal = clamp((shape - 0.55) / 0.40) if row["sessions"] >= 3 else 0.0
        base_signal = base.get(row["author_id"], {}).get("signal_score", 0.0) / 100.0
        group_signal = edge_scores[row["author_id"]] / 100.0
        raw_score = 100.0 * (
            0.35 * base_signal
            + 0.20 * text_signal
            + 0.10 * timing_signal
            + 0.10 * periodic_signal
            + 0.10 * compression_signal
            + 0.05 * shape_signal
            + 0.10 * group_signal
        )
        confidence = clamp(n / 30.0) ** 0.5
        score = raw_score * (0.55 + 0.45 * confidence)
        row["base_behavior_score"] = round(100 * base_signal, 4)
        row["text_automation_component"] = round(100 * text_signal, 4)
        row["timing_automation_component"] = round(100 * timing_signal, 4)
        row["periodicity_component"] = round(100 * periodic_signal, 4)
        row["compression_component"] = round(100 * compression_signal, 4)
        row["session_stability_component"] = round(100 * shape_signal, 4)
        row["group_component"] = round(100 * group_signal, 4)
        row["multiview_partners"] = sum(
            edge["evidence_views"] >= 2 and row["author_id"] in (edge["left"], edge["right"])
            for edge in edges
        )
        row["advanced_signal_score"] = round(score, 4)
        if n < 10:
            row["review_level"] = "evidencia_insuficiente"
        elif score >= 60:
            row["review_level"] = "muy_alta_revision"
        elif score >= 45:
            row["review_level"] = "alta_revision"
        elif score >= 30:
            row["review_level"] = "revision"
        else:
            row["review_level"] = "baja"
    fingerprints.sort(key=lambda row: (row["advanced_signal_score"], row["messages"]), reverse=True)
    return fingerprints


def network_components(edges: Sequence[dict], min_score: float = 30.0) -> List[dict]:
    selected = [
        edge for edge in edges
        if edge["evidence_views"] >= 2 and edge["multiview_score"] >= min_score
    ]
    adjacency: Dict[str, Set[str]] = defaultdict(set)
    for edge in selected:
        adjacency[edge["left"]].add(edge["right"])
        adjacency[edge["right"]].add(edge["left"])
    visited: Set[str] = set()
    rows = []
    for start in sorted(adjacency):
        if start in visited:
            continue
        stack = [start]
        members = set()
        visited.add(start)
        while stack:
            node = stack.pop()
            members.add(node)
            for neighbor in adjacency[node]:
                if neighbor not in visited:
                    visited.add(neighbor)
                    stack.append(neighbor)
        internal = [edge for edge in selected if edge["left"] in members and edge["right"] in members]
        possible = len(members) * (len(members) - 1) / 2.0
        rows.append({
            "component_id": 0,
            "members": len(members),
            "member_ids": " | ".join(sorted(members)),
            "edges": len(internal),
            "density": round(len(internal) / possible, 5) if possible else 0.0,
            "max_multiview_score": max(edge["multiview_score"] for edge in internal),
            "mean_multiview_score": round(statistics.mean(edge["multiview_score"] for edge in internal), 5),
        })
    rows.sort(key=lambda row: (row["max_multiview_score"], row["density"]), reverse=True)
    for index, row in enumerate(rows, 1):
        row["component_id"] = index
    return rows


def safe_json_number(value: object) -> object:
    if isinstance(value, float) and (math.isnan(value) or math.isinf(value)):
        return None
    return value


def write_methodology(path: str, parameters: dict, temporal_metadata: dict) -> None:
    methodology = {
        "tool_version": VERSION,
        "principle": "Evidencia conductual interpretable; no inferencia de identidad, intencion ni postura politica.",
        "parameters": parameters,
        "temporal_test": temporal_metadata,
        "views": {
            "individual": "Entropia textual y temporal, repeticion, compresibilidad, periodicidad y estabilidad entre sesiones.",
            "content_lockstep": "Mensajes exactos o casi iguales de cuentas distintas dentro de una ventana corta.",
            "temporal_synchrony": "Solapamiento de bins activos con prueba hipergeometrica por sesion, Fisher y FDR-BH.",
            "shared_scripts": "Plantillas largas e identicas reutilizadas por cuentas distintas, ponderadas por rareza.",
            "multiview": "Texto/plantillas forman una vista y sincronizacion otra. 'Fuerte' exige ambas y recurrencia temporal en dos o mas sesiones.",
        },
        "modalities": {
            "chat": "Aporta texto y marcas de tiempo exactas para todas las vistas.",
            "comment": "Aporta texto, repeticion y plantillas. Solo aporta temporalidad si la fuente conserva hora exacta.",
            "yt_dlp_comment_guardrail": "Las fechas diarias de comentarios yt-dlp se excluyen de cadencia y sincronizacion fina.",
        },
        "known_limitations": [
            "Una reaccion organica al mismo evento puede producir sincronizacion significativa.",
            "Copiar una consigna o cita no demuestra coordinacion intencional.",
            "No hay etiquetas humanas de bot/no-bot para estimar precision o recall en este corpus.",
            "No se dispone de red de suscripciones, metadatos de perfil ni origen del cliente.",
            "Los q-values dependen del modelo nulo y deben tratarse como exploratorios.",
            "Los comentarios de yt-dlp tienen precision diaria: aportan evidencia textual, no sincronizacion fina ni cadencia.",
        ],
        "references": REFERENCES,
    }
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(methodology, handle, ensure_ascii=False, indent=2)


def write_report(path: str, summary: dict, accounts: Sequence[dict], edges: Sequence[dict]) -> None:
    lines = [
        "# Informe avanzado de señales de automatización y coordinación",
        "",
        "> Este informe prioriza revisión humana. No prueba que una cuenta sea un bot ni que exista coordinación intencional.",
        "",
        "## Resumen",
        "",
        "- Mensajes: {:,}".format(summary["counts"]["messages"]),
        "- Mensajes de chat: {:,}".format(summary["counts"]["source_records"].get("chat", 0)),
        "- Comentarios y respuestas: {:,}".format(summary["counts"]["source_records"].get("comment", 0)),
        "- Cuentas en chat: {:,}".format(summary["counts"]["source_accounts"].get("chat", 0)),
        "- Cuentas en comentarios: {:,}".format(summary["counts"]["source_accounts"].get("comment", 0)),
        "- Cuentas presentes en ambos formatos: {:,}".format(summary["counts"]["cross_source_accounts"]),
        "- Cuentas: {:,}".format(summary["counts"]["accounts"]),
        "- Pares sometidos a prueba temporal: {:,}".format(summary["counts"]["tested_temporal_pairs"]),
        "- Pares con q ≤ 0,05: {:,}".format(summary["counts"]["temporally_significant_pairs"]),
        "- Pares temporales significativos recurrentes en ≥2 sesiones: {:,}".format(summary["counts"]["repeated_temporal_pairs"]),
        "- Enlaces con dos o más vistas: {:,}".format(summary["counts"]["multiview_edges"]),
        "- Componentes multivista: {:,}".format(summary["counts"]["network_components"]),
        "",
        "## Cuentas con mayor prioridad",
        "",
        "| Cuenta | Puntaje | Chat | Comentarios | Sesiones | Entropía texto | Entropía intervalos | Nivel |",
        "|---|---:|---:|---:|---:|---:|---:|---|",
    ]
    for row in accounts[:25]:
        lines.append("| {} | {} | {} | {} | {} | {} | {} | {} |".format(
            row["author_name"].replace("|", "\\|"), row["advanced_signal_score"],
            row["chat_messages"], row["comment_messages"], row["sessions"], row["text_entropy"],
            row["interval_entropy"], row["review_level"],
        ))
    lines.extend(["", "## Lectura automática del corpus", ""])
    if summary["counts"]["strong_multiview_edges"] == 0:
        lines.append(
            "Con los umbrales actuales no apareció ningún par con evidencia multivista fuerte. "
            "La sincronía temporal o la repetición aisladas son candidatos para inspección, no una atribución de coordinación."
        )
    else:
        lines.append(
            "Aparecieron {:,} pares con evidencia multivista fuerte; deben auditarse contra los mensajes originales y explicaciones alternativas.".format(
                summary["counts"]["strong_multiview_edges"]
            )
        )
    lines.append(
        "Hay {:,} pares temporales con q≤0,05, de los cuales {:,} se repiten en dos o más sesiones; "
        "la estabilidad cambia con el tamaño del bin y está detallada abajo.".format(
            summary["counts"]["temporally_significant_pairs"],
            summary["counts"]["repeated_temporal_pairs"],
        )
    )
    lines.append(
        "Los comentarios de yt-dlp ({:,} registros con fecha diaria) se usaron para contenido y actividad entre videos, "
        "pero no para sincronía de segundos.".format(summary["counts"]["coarse_time_records"])
    )
    lines.extend([
        "", "## Relaciones con mayor prioridad", "",
        "| Cuenta A | Cuenta B | Puntaje | Vistas | Contenido | Temporal | Plantillas | Nivel |",
        "|---|---|---:|---:|---:|---:|---:|---|",
    ])
    names = {row["author_id"]: row["author_name"] for row in accounts}
    for row in edges[:25]:
        lines.append("| {} | {} | {} | {} | {} | {} | {} | {} |".format(
            names.get(row["left"], row["left"]).replace("|", "\\|"),
            names.get(row["right"], row["right"]).replace("|", "\\|"),
            row["multiview_score"], row["evidence_views"], row["content_score"],
            row["temporal_score"], row["script_score"], row["review_level"],
        ))
    lines.extend([
        "", "## Sensibilidad al tamaño del intervalo", "",
        "| Segundos por bin | Pares q≤0,05 | Recurrentes ≥2 sesiones | Multivista |",
        "|---:|---:|---:|---:|",
    ])
    for row in summary.get("sensitivity", []):
        lines.append("| {} | {} | {} | {} |".format(
            row["bin_seconds"], row["significant_pairs"],
            row["repeated_significant_pairs"], row["multiview_edges"],
        ))
    lines.extend([
        "", "## Cómo interpretar", "",
        "La entropía baja y la alta compresibilidad son compatibles con automatización simple, pero también con una persona copiando y pegando. La sincronización temporal se contrasta con un modelo nulo y se corrige por pruebas múltiples, pero puede reflejar una reacción legítima al debate. Por eso la categoría multivista exige al menos dos tipos de evidencia.",
        "", "Consulte `methodology.json` para parámetros, limitaciones y referencias completas.", "",
    ])
    with open(path, "w", encoding="utf-8") as handle:
        handle.write("\n".join(lines))


def add_account_names(rows: Sequence[dict], names: Dict[str, str]) -> List[dict]:
    named = []
    for row in rows:
        copy = dict(row)
        if "left" in copy:
            copy["left_name"] = names.get(copy["left"], "")
            copy["right_name"] = names.get(copy["right"], "")
        named.append(copy)
    return named


def write_pair_evidence(
    path: str,
    messages: Sequence[Message],
    temporal_rows: Sequence[dict],
    multiview_rows: Sequence[dict],
    bin_seconds: int,
    fdr: float,
    max_pairs: int = 100,
    max_bins_per_pair: int = 20,
) -> None:
    """Exporta texto original alrededor de solapamientos para auditoria."""
    priority = []
    seen = set()
    for row in multiview_rows:
        if row["evidence_views"] < 2:
            continue
        pair = (row["left"], row["right"])
        priority.append(pair)
        seen.add(pair)
    for row in temporal_rows:
        pair = (row["left"], row["right"])
        if pair in seen:
            continue
        if row["q_value"] <= fdr and row["overlap_sessions"] >= 2:
            priority.append(pair)
            seen.add(pair)
        if len(priority) >= max_pairs:
            break
    priority = priority[:max_pairs]
    wanted = set(priority)
    divisor = bin_seconds * 1000
    grouped: Dict[Tuple[str, str, int], List[Message]] = defaultdict(list)
    bins_by_session_author: Dict[Tuple[str, str], Set[int]] = defaultdict(set)
    relevant_authors = {author for pair in wanted for author in pair}
    for message in messages:
        if message.author_id in relevant_authors and message.time_precision == "exact":
            bin_index = message.time_ms // divisor
            grouped[(message.session, message.author_id, bin_index)].append(message)
            bins_by_session_author[(message.session, message.author_id)].add(bin_index)
    evidence_by_pair: Dict[Tuple[str, str], List[dict]] = defaultdict(list)
    sessions = sorted({message.session for message in messages})
    for left, right in priority:
        for session in sessions:
            left_bins = bins_by_session_author[(session, left)]
            right_bins = bins_by_session_author[(session, right)]
            for bin_index in sorted(left_bins & right_bins):
                evidence_by_pair[(left, right)].append({
                    "session": session,
                    "bin_start_seconds": bin_index * bin_seconds,
                    "left_messages": [
                        {"second": round(item.time_ms / 1000.0, 3), "text": item.text}
                        for item in grouped[(session, left, bin_index)]
                    ],
                    "right_messages": [
                        {"second": round(item.time_ms / 1000.0, 3), "text": item.text}
                        for item in grouped[(session, right, bin_index)]
                    ],
                })
    with open(path, "w", encoding="utf-8") as handle:
        for left, right in priority:
            record = {
                "left": left,
                "right": right,
                "overlapping_bins": len(evidence_by_pair[(left, right)]),
                "evidence": evidence_by_pair[(left, right)][:max_bins_per_pair],
            }
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")


def sensitivity_analysis(
    messages: Sequence[Message],
    bin_sizes: Sequence[int],
    min_pair_messages: int,
    fdr: float,
    base_edges: Sequence[dict],
    script_rows: Sequence[dict],
    cached_bin: int,
    cached_temporal: Sequence[dict],
) -> Tuple[List[dict], List[dict]]:
    summaries = []
    stability: Dict[Tuple[str, str], List[Tuple[int, float]]] = defaultdict(list)
    for bin_seconds in sorted(set(bin_sizes)):
        if bin_seconds == cached_bin:
            temporal_rows = list(cached_temporal)
        else:
            temporal_rows, _ = temporal_synchrony(messages, bin_seconds, min_pair_messages)
        edges = multiview_edges(base_edges, temporal_rows, script_rows)
        candidates = [edge for edge in edges if edge["evidence_views"] >= 2]
        summaries.append({
            "bin_seconds": bin_seconds,
            "tested_pairs": len(temporal_rows),
            "significant_pairs": sum(edge["q_value"] <= fdr for edge in temporal_rows),
            "repeated_significant_pairs": sum(
                edge["q_value"] <= fdr and edge["overlap_sessions"] >= 2
                for edge in temporal_rows
            ),
            "multiview_edges": len(candidates),
        })
        for edge in candidates:
            stability[(edge["left"], edge["right"])].append((bin_seconds, edge["multiview_score"]))
    stability_rows = []
    total_windows = len(set(bin_sizes))
    for (left, right), observations in stability.items():
        scores = [score for _, score in observations]
        stability_rows.append({
            "left": left,
            "right": right,
            "windows_detected": len(observations),
            "total_windows": total_windows,
            "bin_seconds_detected": " | ".join(str(size) for size, _ in observations),
            "min_multiview_score": round(min(scores), 5),
            "mean_multiview_score": round(statistics.mean(scores), 5),
            "max_multiview_score": round(max(scores), 5),
        })
    stability_rows.sort(
        key=lambda row: (row["windows_detected"], row["mean_multiview_score"]), reverse=True
    )
    return summaries, stability_rows


def command_run(args: argparse.Namespace) -> int:
    paths = discover_jsonl(args.input)
    if not paths:
        raise SystemExit("No se encontraron archivos JSONL")
    messages, excluded_outside_catalog = filter_messages_to_catalog(list(iter_messages(paths)), args.input)
    base_accounts, base_edges, _ = base_analyze(messages, args.sync_window, args.near_threshold)
    fingerprints = account_fingerprints(messages)
    temporal_rows, temporal_metadata = temporal_synchrony(messages, args.bin_seconds, args.min_pair_messages)
    script_rows = shared_script_edges(messages, args.min_script_characters, args.min_script_tokens)
    edges = multiview_edges(base_edges, temporal_rows, script_rows)
    sensitivity_bins = [int(value) for value in args.sensitivity_bins.split(",") if value.strip()]
    sensitivity, stable_edges = sensitivity_analysis(
        messages, sensitivity_bins, args.min_pair_messages, args.fdr,
        base_edges, script_rows, args.bin_seconds, temporal_rows,
    )
    fingerprints = score_fingerprints(fingerprints, base_accounts, edges)
    components = network_components(edges, args.component_threshold)

    os.makedirs(args.output, exist_ok=True)
    write_csv(os.path.join(args.output, "account_fingerprints.csv"), fingerprints, list(fingerprints[0].keys()))
    activity_fields = [
        "author_id", "author_name", "messages", "chat_messages", "comment_messages",
        "sessions", "exact_time_messages", "source_types",
    ]
    most_active = sorted(
        ({field: row[field] for field in activity_fields} for row in fingerprints),
        key=lambda row: (row["messages"], row["sessions"]),
        reverse=True,
    )
    write_csv(os.path.join(args.output, "most_active_users.csv"), most_active, activity_fields)
    names = {row["author_id"]: row["author_name"] for row in fingerprints}
    temporal_export = [
        row for row in temporal_rows
        if row["shared_active_bins"] >= 3 or row["q_value"] <= args.fdr
    ]
    named_temporal = add_account_names(temporal_export, names)
    temporal_fields = list(named_temporal[0].keys()) if named_temporal else [
        "left", "right", "p_value", "q_value", "shared_sessions", "overlap_sessions",
        "shared_active_bins", "expected_shared_bins", "synchrony_lift", "temporal_jaccard",
        "busy_adjusted_overlap", "temporal_score", "left_name", "right_name",
    ]
    write_csv(os.path.join(args.output, "temporal_coordination.csv"), named_temporal, temporal_fields)
    script_fields = list(script_rows[0].keys()) if script_rows else [
        "left", "right", "shared_templates", "weighted_templates", "template_sessions", "examples", "script_score"
    ]
    named_scripts = add_account_names(script_rows, names)
    script_fields = list(named_scripts[0].keys()) if named_scripts else script_fields
    write_csv(os.path.join(args.output, "shared_scripts.csv"), named_scripts, script_fields)
    named_edges = add_account_names(edges, names)
    edge_fields = list(named_edges[0].keys()) if named_edges else [
        "left", "right", "content_score", "content_events", "temporal_score",
        "temporal_q_value", "synchrony_lift", "shared_active_bins",
        "temporal_overlap_sessions", "script_score", "shared_templates",
        "evidence_examples", "evidence_views", "multiview_score", "review_level",
        "left_name", "right_name",
    ]
    write_csv(os.path.join(args.output, "multiview_edges.csv"), named_edges, edge_fields)
    component_fields = list(components[0].keys()) if components else [
        "component_id", "members", "member_ids", "edges", "density", "max_multiview_score", "mean_multiview_score"
    ]
    write_csv(os.path.join(args.output, "network_components.csv"), components, component_fields)
    write_csv(os.path.join(args.output, "sensitivity.csv"), sensitivity, list(sensitivity[0].keys()))
    named_stable_edges = add_account_names(stable_edges, names)
    stable_fields = list(named_stable_edges[0].keys()) if named_stable_edges else [
        "left", "right", "windows_detected", "total_windows", "bin_seconds_detected",
        "min_multiview_score", "mean_multiview_score", "max_multiview_score", "left_name", "right_name",
    ]
    write_csv(os.path.join(args.output, "sensitivity_edges.csv"), named_stable_edges, stable_fields)
    write_pair_evidence(
        os.path.join(args.output, "pair_evidence.jsonl"), messages, temporal_rows,
        edges, args.bin_seconds, args.fdr,
    )

    source_records = Counter(message.source for message in messages)
    source_authors: Dict[str, Set[str]] = defaultdict(set)
    sources_by_author: Dict[str, Set[str]] = defaultdict(set)
    for message in messages:
        source_authors[message.source].add(message.author_id)
        sources_by_author[message.author_id].add(message.source)
    summary = {
        "tool_version": VERSION,
        "input": os.path.abspath(args.input),
        "counts": {
            "messages": len(messages),
            "source_records": dict(source_records),
            "source_accounts": {
                source: len(authors) for source, authors in sorted(source_authors.items())
            },
            "cross_source_accounts": sum(len(sources) >= 2 for sources in sources_by_author.values()),
            "exact_time_records": sum(message.time_precision == "exact" for message in messages),
            "coarse_time_records": sum(message.time_precision != "exact" for message in messages),
            "excluded_outside_catalog": excluded_outside_catalog,
            "accounts": len(fingerprints),
            "sessions": len({message.session for message in messages}),
            "tested_temporal_pairs": temporal_metadata["tested_pairs"],
            "temporally_significant_pairs": sum(row["q_value"] <= args.fdr for row in temporal_rows),
            "repeated_temporal_pairs": sum(
                row["q_value"] <= args.fdr and row["overlap_sessions"] >= 2
                for row in temporal_rows
            ),
            "shared_script_edges": len(script_rows),
            "multiview_edges": sum(row["evidence_views"] >= 2 for row in edges),
            "strong_multiview_edges": sum(row["review_level"] == "multivista_fuerte" for row in edges),
            "network_components": len(components),
        },
        "account_review_levels": dict(Counter(row["review_level"] for row in fingerprints)),
        "parameters": {
            "bin_seconds": args.bin_seconds,
            "min_pair_messages": args.min_pair_messages,
            "fdr": args.fdr,
            "sync_window": args.sync_window,
            "near_threshold": args.near_threshold,
            "min_script_characters": args.min_script_characters,
            "min_script_tokens": args.min_script_tokens,
            "component_threshold": args.component_threshold,
            "sensitivity_bins": sensitivity_bins,
        },
        "sensitivity": sensitivity,
    }
    with open(os.path.join(args.output, "advanced_summary.json"), "w", encoding="utf-8") as handle:
        json.dump(summary, handle, ensure_ascii=False, indent=2, default=safe_json_number)
    write_methodology(os.path.join(args.output, "methodology.json"), summary["parameters"], temporal_metadata)
    write_report(os.path.join(args.output, "advanced_report.md"), summary, fingerprints, edges)
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("input", help="Archivo o directorio con chats JSONL")
    parser.add_argument("--output", default="advanced_results")
    parser.add_argument("--bin-seconds", type=int, default=30)
    parser.add_argument("--min-pair-messages", type=int, default=10)
    parser.add_argument("--fdr", type=float, default=0.05)
    parser.add_argument("--sync-window", type=int, default=120)
    parser.add_argument("--near-threshold", type=float, default=0.84)
    parser.add_argument("--min-script-characters", type=int, default=30)
    parser.add_argument("--min-script-tokens", type=int, default=6)
    parser.add_argument("--component-threshold", type=float, default=30.0)
    parser.add_argument("--sensitivity-bins", default="20,30,45,60", help="Bins separados por coma; use el bin principal para una corrida rapida")
    parser.set_defaults(func=command_run)
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.bin_seconds <= 0 or args.min_pair_messages <= 1:
        parser.error("Los tamaños y mínimos deben ser positivos")
    if not 0 < args.fdr < 1 or not 0.5 <= args.near_threshold <= 1:
        parser.error("--fdr o --near-threshold fuera de rango")
    try:
        sensitivity_bins = [int(value) for value in args.sensitivity_bins.split(",") if value.strip()]
    except ValueError:
        parser.error("--sensitivity-bins debe contener enteros separados por coma")
    if not sensitivity_bins or any(value <= 0 for value in sensitivity_bins):
        parser.error("--sensitivity-bins debe contener al menos un entero positivo")
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
