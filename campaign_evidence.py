#!/usr/bin/env python3
"""Busca evidencia reproducible de una posible campaña progobierno coordinada.

La orientacion politica se estima con reglas transparentes y admite correcciones
humanas por CSV. La atribucion de coordinacion se mantiene separada: apoyo o
ataque compartido no cuenta como coordinacion si no coincide tambien con
sincronia, recurrencia o plantillas distintivas.
"""

import argparse
import csv
import json
import os
import re
from collections import Counter, defaultdict
from typing import Dict, Iterable, List, Optional, Sequence, Set, Tuple

from advanced_analysis import multiview_edges, shared_script_edges, temporal_synchrony
from bot_detector import (
    Message,
    discover_jsonl,
    filter_messages_to_catalog,
    find_coordination,
    iter_messages,
    normalize_text,
    write_csv,
)


VERSION = "0.1.0"
LABELS = {"direct_support", "ambiguous_support", "possible_criticism", "opposition_or_institution_attack", "other"}

ENTITY = r"(?:laura(?:\s+fernandez)?|(?:rodrigo\s+)?chaves|president[ea]|pilar(?:\s+cisneros)?|gobierno)"
POSITIVE = r"(?:viva|apoy\w*|gracias|excelent\w*|bendiga\w*|bendicion\w*|adelante|orgullo|valiente|querid\w*|gran\s+lider|estamos\s+con|amo|fuerza)"
SUPPORT_RE = re.compile(
    r"(?:\b{}\b.{{0,55}}\b{}\b|\b{}\b.{{0,55}}\b{}\b)".format(
        POSITIVE, ENTITY, ENTITY, POSITIVE
    )
)
AMBIGUITY_RE = re.compile(
    r"(?:me\s+.{0,25}paga\w*|rojito|cita\s+medica|impuestos?|engaño|sarcasm|trol\w*|granja|sicarios?\s+digital)",
    re.IGNORECASE,
)
GOVERNMENT_NEGATIVE_RE = re.compile(
    r"\b(?:fuera|corrupt\w*|mentir\w*|mentiros\w*|dictador\w*|ladron\w*|narcogobierno|inept\w*|renuncie|asco|vergüenza|payas\w*|patetic\w*)\b"
)
GOVERNMENT_CRITICISM_RE = re.compile(
    r"(?:\b{}\b.{{0,45}}{}|{}.{{0,45}}\b{}\b)".format(
        ENTITY, GOVERNMENT_NEGATIVE_RE.pattern,
        GOVERNMENT_NEGATIVE_RE.pattern, ENTITY,
    )
)
TARGET_RE = re.compile(
    r"\b(?:pln|liberacion\s+nacional|frente\s+amplio|poder\s+judicial|magistrad\w*|contralor\w*|la\s+nacion|crhoy)\b"
)
ATTACK_RE = re.compile(
    r"\b(?:corrupt\w*|rob\w*|ladron\w*|rata\w*|basura|canalla|fuera|narco\w*|dictadura|mafia|red\s+de\s+cuido|pericos?)\b"
)
ENTITY_RE = re.compile(r"\b{}\b".format(ENTITY))


def classify_text(normalized: str) -> Tuple[str, str]:
    """Devuelve etiqueta exploratoria y regla activada; no es verdad de terreno."""
    support = bool(SUPPORT_RE.search(normalized))
    government_negative = bool(GOVERNMENT_CRITICISM_RE.search(normalized))
    if support and AMBIGUITY_RE.search(normalized):
        return "ambiguous_support", "apoyo_aparente_con_sarcasmo_o_negacion"
    if support:
        return "direct_support", "entidad_gobierno_mas_expresion_de_apoyo"
    if TARGET_RE.search(normalized) and ATTACK_RE.search(normalized):
        return "opposition_or_institution_attack", "objetivo_politico_o_institucional_mas_ataque"
    if government_negative:
        return "possible_criticism", "entidad_gobierno_mas_expresion_negativa_cercana"
    return "other", "sin_regla"


def load_overrides(path: Optional[str]) -> Dict[str, str]:
    if not path:
        return {}
    overrides = {}
    with open(path, newline="", encoding="utf-8") as handle:
        for row in csv.DictReader(handle):
            text = normalize_text(row.get("normalized_text") or row.get("text") or "")
            label = (row.get("label") or "").strip()
            if text and label in LABELS:
                overrides[text] = label
    return overrides


def classify_messages(messages: Sequence[Message], overrides: Dict[str, str]) -> Tuple[Dict[int, str], Dict[int, str]]:
    labels = {}
    rules = {}
    for message in messages:
        label, rule = classify_text(message.normalized)
        if message.normalized in overrides:
            label, rule = overrides[message.normalized], "revision_humana"
        labels[id(message)] = label
        rules[id(message)] = rule
    return labels, rules


def account_summary(messages: Sequence[Message], labels: Dict[int, str]) -> List[dict]:
    grouped: Dict[str, List[Message]] = defaultdict(list)
    for message in messages:
        grouped[message.author_id].append(message)
    rows = []
    for author, items in grouped.items():
        counts = Counter(labels[id(item)] for item in items)
        support = [item for item in items if labels[id(item)] == "direct_support"]
        support_texts = Counter(item.normalized for item in support if item.normalized)
        names = Counter(item.author_name for item in items if item.author_name)
        rows.append({
            "author_id": author,
            "author_name": names.most_common(1)[0][0] if names else "",
            "messages": len(items),
            "sessions": len({item.session for item in items}),
            "direct_support": counts["direct_support"],
            "ambiguous_support": counts["ambiguous_support"],
            "possible_criticism": counts["possible_criticism"],
            "opposition_or_institution_attack": counts["opposition_or_institution_attack"],
            "support_sessions": len({item.session for item in support}),
            "support_sources": " | ".join(sorted({item.source for item in support})),
            "support_fraction": round(len(support) / float(max(1, len(items))), 5),
            "support_repetition_fraction": round(
                1.0 - len(support_texts) / float(max(1, len(support))), 5
            ),
            "dominant_support_count": support_texts.most_common(1)[0][1] if support_texts else 0,
            "dominant_support_text": next(
                (item.text[:300] for item in support if item.normalized == support_texts.most_common(1)[0][0]),
                "",
            ) if support_texts else "",
        })
    rows.sort(key=lambda row: (row["direct_support"], row["support_sessions"], row["messages"]), reverse=True)
    return rows


def shared_template_rows(messages: Sequence[Message], labels: Dict[int, str], allowed: Set[str]) -> List[dict]:
    grouped: Dict[str, List[Message]] = defaultdict(list)
    for message in messages:
        if labels[id(message)] in allowed and len(message.normalized) >= 12:
            grouped[message.normalized].append(message)
    rows = []
    for normalized, items in grouped.items():
        authors = {item.author_id for item in items}
        if len(authors) < 2:
            continue
        names = sorted({item.author_name for item in items if item.author_name})
        tokens = len(normalized.split())
        rows.append({
            "template": items[0].text[:500],
            "normalized_text": normalized,
            "messages": len(items),
            "authors": len(authors),
            "author_names": " | ".join(names),
            "sessions": len({item.session for item in items}),
            "sources": " | ".join(sorted({item.source for item in items})),
            "characters": len(normalized),
            "tokens": tokens,
            "distinctive": int(len(normalized) >= 30 and tokens >= 6),
        })
    rows.sort(key=lambda row: (row["distinctive"], row["authors"], row["sessions"], row["messages"]), reverse=True)
    return rows


def add_names(rows: Iterable[dict], names: Dict[str, str]) -> List[dict]:
    output = []
    for row in rows:
        copy = dict(row)
        if "left" in copy:
            copy["left_name"] = names.get(copy["left"], "")
            copy["right_name"] = names.get(copy["right"], "")
        output.append(copy)
    return output


def network_view(messages: Sequence[Message], bin_seconds: int, min_messages: int) -> Tuple[List[dict], dict]:
    content, _ = find_coordination(messages, 120, 0.84)
    temporal, metadata = temporal_synchrony(messages, bin_seconds, min_messages)
    scripts = shared_script_edges(messages, 30, 6)
    combined = multiview_edges(content, temporal, scripts)
    summary = {
        "eligible_accounts": metadata["eligible_accounts"],
        "tested_temporal_pairs": metadata["tested_pairs"],
        "fdr_significant_pairs": sum(row["q_value"] <= 0.05 for row in temporal),
        "content_lockstep_pairs": len(content),
        "distinctive_script_pairs": len(scripts),
        "multiview_pairs": sum(row["evidence_views"] >= 2 for row in combined),
        "strong_multiview_pairs": sum(row["review_level"] == "multivista_fuerte" for row in combined),
    }
    return combined, summary


def coverage(input_path: str, messages: Sequence[Message]) -> dict:
    video_ids = {message.session.removesuffix("__comments") for message in messages}
    dates = []
    catalog = os.path.join(input_path, "catalog.jsonl") if os.path.isdir(input_path) else ""
    if catalog and os.path.isfile(catalog):
        with open(catalog, encoding="utf-8") as handle:
            for line in handle:
                row = json.loads(line)
                if row.get("video_id") not in video_ids:
                    continue
                value = row.get("upload_date") or row.get("published_at")
                if value:
                    value = str(value)
                    dates.append(
                        "{}-{}-{}".format(value[:4], value[4:6], value[6:8])
                        if len(value) == 8 and value.isdigit() else value[:10]
                    )
    return {
        "videos_with_records": len(video_ids),
        "chat_sessions": len({message.session for message in messages if message.source == "chat"}),
        "comment_video_sessions": len({message.session for message in messages if message.source == "comment"}),
        "earliest_catalog_date": min(dates) if dates else None,
        "latest_catalog_date": max(dates) if dates else None,
    }


def concentration(messages: Sequence[Message]) -> dict:
    counts = Counter(message.author_id for message in messages)
    total = sum(counts.values())
    shares = [count / float(total) for count in sorted(counts.values(), reverse=True)] if total else []
    return {
        "top_1_share": round(sum(shares[:1]), 6),
        "top_5_share": round(sum(shares[:5]), 6),
        "top_10_share": round(sum(shares[:10]), 6),
        "hhi": round(sum(share * share for share in shares), 6),
    }


def write_jsonl(path: str, records: Iterable[dict]) -> None:
    with open(path, "w", encoding="utf-8") as handle:
        for record in records:
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")


def write_report(path: str, summary: dict, accounts: Sequence[dict], templates: Sequence[dict], broad_edges: Sequence[dict]) -> None:
    counts = summary["labels"]
    strict = summary["strict_support_network"]
    broad = summary["broad_aligned_network"]
    lines = [
        "# Búsqueda de evidencia de campaña progobierno coordinada", "",
        "> Resultado exploratorio: clasificar lenguaje político y atribuir coordinación son problemas distintos. Las reglas de orientación requieren auditoría humana.", "",
        "## Cobertura", "",
        "- Registros: {:,}".format(summary["messages"]),
        "- Videos con registros: {:,}".format(summary["coverage"]["videos_with_records"]),
        "- Fechas de los videos descargados: {} a {}".format(
            summary["coverage"]["earliest_catalog_date"], summary["coverage"]["latest_catalog_date"]
        ),
        "- Registros descargados fuera del catálogo exacto y excluidos: {:,}".format(
            summary["excluded_outside_catalog"]
        ),
        "- Apoyo directo preliminar: {:,} mensajes de {:,} cuentas".format(
            counts["direct_support"], summary["direct_support_accounts"]
        ),
        "- Concentración del apoyo: cuenta principal {:.1%}; diez principales {:.1%}".format(
            summary["direct_support_concentration"]["top_1_share"],
            summary["direct_support_concentration"]["top_10_share"],
        ),
        "- Apoyo aparente ambiguo o sarcástico: {:,}".format(counts["ambiguous_support"]),
        "- Ataques a oposición o instituciones (no equivalen a apoyo): {:,}".format(
            counts["opposition_or_institution_attack"]
        ), "",
        "## Prueba de la hipótesis", "",
        "| Universo | Pares temporales probados | q≤0,05 | Lockstep | Plantillas distintivas | Multivista | Fuerte |",
        "|---|---:|---:|---:|---:|---:|---:|",
        "| Apoyo directo | {tested_temporal_pairs} | {fdr_significant_pairs} | {content_lockstep_pairs} | {distinctive_script_pairs} | {multiview_pairs} | {strong_multiview_pairs} |".format(**strict),
        "| Apoyo + ataques alineables | {tested_temporal_pairs} | {fdr_significant_pairs} | {content_lockstep_pairs} | {distinctive_script_pairs} | {multiview_pairs} | {strong_multiview_pairs} |".format(**broad), "",
        "### Sensibilidad temporal", "",
        "| Bin (segundos) | q≤0,05 apoyo | Multivista apoyo | q≤0,05 universo amplio | Multivista amplio |",
        "|---:|---:|---:|---:|---:|",
    ]
    for row in summary["sensitivity"]:
        lines.append("| {} | {} | {} | {} | {} |".format(
            row["bin_seconds"], row["strict_fdr_significant_pairs"], row["strict_multiview_pairs"],
            row["broad_fdr_significant_pairs"], row["broad_multiview_pairs"],
        ))
    lines.extend([
        "",
        "## Evaluación", "",
        "**{}**".format(summary["assessment"]), "",
        "El corpus contiene apoyo progobierno y repetición, pero no apareció un par que combine dos vistas independientes de coordinación. Los ataques a oposición o instituciones se analizan solo como universo amplio: no se asume que toda crítica al PLN, al Poder Judicial o a la prensa sea apoyo al Gobierno.", "",
        "## Cuentas con más apoyo directo preliminar", "",
        "| Cuenta | Apoyo | Sesiones | Repetición | Texto dominante |",
        "|---|---:|---:|---:|---|",
    ])
    for row in accounts[:15]:
        if not row["direct_support"]:
            continue
        lines.append("| {} | {} | {} | {} | {} |".format(
            row["author_name"].replace("|", "\\|"), row["direct_support"], row["support_sessions"],
            row["support_repetition_fraction"], row["dominant_support_text"].replace("|", "\\|")[:140],
        ))
    lines.extend(["", "## Plantillas compartidas para revisión", ""])
    distinctive = [row for row in templates if row["distinctive"]]
    if not distinctive:
        lines.append("No se encontraron plantillas largas y distintivas de apoyo directo compartidas por cuentas distintas.")
    else:
        for row in distinctive[:10]:
            lines.append("- {} cuentas, {} sesiones: {}".format(row["authors"], row["sessions"], row["template"]))
    broad_scripts = [row for row in broad_edges if row.get("shared_templates", 0)]
    if broad_scripts:
        lines.extend(["", "En el universo amplio apareció esta evidencia narrativa, que aún requiere interpretación humana:", ""])
        seen = set()
        for row in broad_scripts:
            example = row.get("evidence_examples") or ""
            if example and example not in seen:
                lines.append("- {}".format(example))
                seen.add(example)
    lines.extend([
        "", "## Límites", "",
        "La búsqueda cubre todos los videos del catálogo exacto dentro de la ventana, pero no otras plataformas, cuentas privadas ni información sobre quién opera cada cuenta. Las reglas pueden confundir apoyo genuino, citas, ironía y sátira; `stance_review.csv` permite revisar y corregir etiquetas. Una campaña no puede atribuirse únicamente por afinidad política o por repetir una consigna.", "",
    ])
    with open(path, "w", encoding="utf-8") as handle:
        handle.write("\n".join(lines))


def run(args: argparse.Namespace) -> int:
    paths = discover_jsonl(args.input)
    messages, excluded_outside_catalog = filter_messages_to_catalog(list(iter_messages(paths)), args.input)
    if not messages:
        raise SystemExit("No se encontraron mensajes o comentarios compatibles")
    overrides = load_overrides(args.labels)
    labels, rules = classify_messages(messages, overrides)
    accounts = account_summary(messages, labels)
    names = {row["author_id"]: row["author_name"] for row in accounts}
    strict_messages = [message for message in messages if labels[id(message)] == "direct_support"]
    broad_messages = [
        message for message in messages
        if labels[id(message)] in {"direct_support", "opposition_or_institution_attack"}
    ]
    strict_edges, strict_summary = network_view(strict_messages, args.bin_seconds, args.min_account_messages)
    broad_edges, broad_summary = network_view(broad_messages, args.bin_seconds, args.min_account_messages)
    sensitivity = []
    for bin_seconds in sorted({int(value) for value in args.sensitivity_bins.split(",") if value.strip()}):
        _, strict_at_bin = network_view(strict_messages, bin_seconds, args.min_account_messages)
        _, broad_at_bin = network_view(broad_messages, bin_seconds, args.min_account_messages)
        sensitivity.append({
            "bin_seconds": bin_seconds,
            "strict_fdr_significant_pairs": strict_at_bin["fdr_significant_pairs"],
            "strict_multiview_pairs": strict_at_bin["multiview_pairs"],
            "broad_fdr_significant_pairs": broad_at_bin["fdr_significant_pairs"],
            "broad_multiview_pairs": broad_at_bin["multiview_pairs"],
        })
    templates = shared_template_rows(messages, labels, {"direct_support"})
    label_counts = Counter(labels.values())

    assessment = (
        "No hay evidencia suficiente en este lote para afirmar una campaña progobierno coordinada."
        if strict_summary["strong_multiview_pairs"] == 0 and broad_summary["strong_multiview_pairs"] == 0
        else "Hay relaciones candidatas con evidencia multivista; requieren auditoría humana antes de atribuir una campaña."
    )
    summary = {
        "tool_version": VERSION,
        "input": os.path.abspath(args.input),
        "messages": len(messages),
        "excluded_outside_catalog": excluded_outside_catalog,
        "coverage": coverage(args.input, messages),
        "labels": {label: label_counts[label] for label in sorted(LABELS)},
        "direct_support_accounts": len({message.author_id for message in strict_messages}),
        "direct_support_sources": dict(Counter(message.source for message in strict_messages)),
        "direct_support_concentration": concentration(strict_messages),
        "strict_support_network": strict_summary,
        "broad_aligned_network": broad_summary,
        "sensitivity": sensitivity,
        "human_overrides": len(overrides),
        "assessment": assessment,
        "parameters": {
            "bin_seconds": args.bin_seconds,
            "min_account_messages": args.min_account_messages,
            "content_window_seconds": 120,
            "near_text_threshold": 0.84,
            "fdr": 0.05,
            "sensitivity_bins": [row["bin_seconds"] for row in sensitivity],
        },
        "context_sources": [
            "https://www.tse.go.cr/juris/relevantes/1876-E11-2026.html",
            "https://tse.go.cr/comunicado1168.html",
        ],
    }

    os.makedirs(args.output, exist_ok=True)
    with open(os.path.join(args.output, "campaign_summary.json"), "w", encoding="utf-8") as handle:
        json.dump(summary, handle, ensure_ascii=False, indent=2)
    write_csv(os.path.join(args.output, "stance_accounts.csv"), accounts, list(accounts[0].keys()))
    template_fields = list(templates[0].keys()) if templates else [
        "template", "normalized_text", "messages", "authors", "author_names", "sessions",
        "sources", "characters", "tokens", "distinctive",
    ]
    write_csv(os.path.join(args.output, "support_templates.csv"), templates, template_fields)
    strict_named = add_names(strict_edges, names)
    broad_named = add_names(broad_edges, names)
    edge_fields = list((strict_named or broad_named)[0].keys()) if (strict_named or broad_named) else [
        "left", "right", "content_score", "temporal_score", "script_score", "evidence_views",
        "multiview_score", "review_level", "left_name", "right_name",
    ]
    write_csv(os.path.join(args.output, "strict_support_edges.csv"), strict_named, edge_fields)
    write_csv(os.path.join(args.output, "broad_aligned_edges.csv"), broad_named, edge_fields)

    classified = []
    for message in messages:
        label = labels[id(message)]
        if label == "other":
            continue
        classified.append({
            "session": message.session,
            "source": message.source,
            "time_ms": message.time_ms,
            "time_precision": message.time_precision,
            "author_id": message.author_id,
            "author_name": message.author_name,
            "label": label,
            "rule": rules[id(message)],
            "text": message.text,
            "normalized_text": message.normalized,
        })
    write_jsonl(os.path.join(args.output, "classified_evidence.jsonl"), classified)

    review_groups: Dict[Tuple[str, str], List[Message]] = defaultdict(list)
    for message in messages:
        label = labels[id(message)]
        if label != "other":
            review_groups[(label, message.normalized)].append(message)
    review = []
    for (label, normalized), items in review_groups.items():
        review.append({
            "label": label,
            "messages": len(items),
            "accounts": len({item.author_id for item in items}),
            "sessions": len({item.session for item in items}),
            "text": items[0].text[:500],
            "normalized_text": normalized,
        })
    review.sort(key=lambda row: (row["messages"], row["accounts"], row["sessions"]), reverse=True)
    write_csv(os.path.join(args.output, "stance_review.csv"), review, list(review[0].keys()))
    write_report(os.path.join(args.output, "campaign_report.md"), summary, accounts, templates, broad_named)
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("input", help="Directorio o JSONL con chats y comentarios")
    parser.add_argument("--output", default="campaign_analysis")
    parser.add_argument("--labels", help="CSV opcional con normalized_text,label para correcciones humanas")
    parser.add_argument("--bin-seconds", type=int, default=30)
    parser.add_argument("--min-account-messages", type=int, default=5)
    parser.add_argument("--sensitivity-bins", default="20,30,45,60")
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    return run(build_parser().parse_args(argv))


if __name__ == "__main__":
    raise SystemExit(main())
