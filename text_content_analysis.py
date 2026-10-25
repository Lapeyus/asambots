#!/usr/bin/env python3
"""Clasificación multietiqueta y estadísticas de contenido para chats de YouTube.

Las reglas son transparentes y producen candidatos para validación humana. No
determinan intención, veracidad, afiliación ni identidad de una cuenta.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
import re
from collections import Counter, defaultdict
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Sequence, Set, Tuple

from bot_detector import (
    Message, discover_jsonl, filter_messages_to_catalog, iter_messages,
    normalize_text, write_csv,
)
VERSION = "0.2.0"
HANDLE_RE = re.compile(r"(?<!\w)@[\w.-]+", re.UNICODE)
URL_TOKEN_RE = re.compile(r"\bhttps?\s+url\b|<url>")

GOVERNMENT_RE = re.compile(
    r"\b(?:gobierno|oficialismo|ppso|pueblo\s+soberano|laura\s+fern[aá]ndez|presidenta\s+laura|"
    r"rodrigo\s+ch[aá]v(?:es|ez)|ch[aá]v(?:es|ez)|pilar\s+cisneros|president[ea]|"
    r"poder\s+ejecutivo|nogui\s+acosta)\b"
)
TITLE_RE = re.compile(r"\bpresident[ea]\b")
NEGATIVE_RE = re.compile(
    r"\b(?:corrupt\w*|ladron\w*|mentiros\w*|dictador\w*|inept\w*|"
    r"vergüenza|sinvergüenza|narco\w*|acosador\w*|abusador\w*|traidor\w*|"
    r"vendid\w*|desastre|asco|renuncie|payas\w*|criminal\w*|est[uú]pid\w*|idiota\w*)\b"
)
STANCE_TARGET = (
    r"(?:gobierno\s+(?:de\s+)?(?:ch[aá]ves|laura\s+fern[aá]ndez)|(?:este|esta|actual|nuestro|nuestra|su)\s+gobierno|"
    r"oficialismo|ppso|pueblo\s+soberano|laura\s+fern[aá]ndez|president[ea]\s+laura|"
    r"president[ea]\s+rodrigo\s+ch[aá]v(?:es|ez)|"
    r"rodrigo\s+ch[aá]v(?:es|ez)|ch[aá]v(?:es|ez)|pilar\s+cisneros|nogui(?:\s+acosta)?)"
)
NAMED_GOVERNMENT_RE = re.compile(r"\b" + STANCE_TARGET + r"\b")
DIRECT_PRO_RE = re.compile(
    r"\b(?:viva|excelent\w*|gracias(?:\s+a)?|felicit\w*|adelante|te\s+amamos|"
    r"estamos\s+con|(?<!no\s)apoy(?:o|amos)(?:\s+total(?:mente)?)?(?:\s+a|\s+al)?)"
    r"\s+(?:(?:el|la|mi|mis|nuestro|nuestra|señor|señora|don|doña)\s+){0,2}" + STANCE_TARGET
)
TITLE_PRAISE_RE = re.compile(
    r"\b(?:el\s+)?mejor\s+president[ea]\b|\bmi\s+president[ea]\s+gracias\b"
)
HYPOTHETICAL_TITLE_RE = re.compile(
    r"\b(?:hubiera|habria|seria|fuera)\s+(?:sido\s+)?(?:un\s+|el\s+|la\s+)?(?:mejor\s+)?president[ea]\b"
)
AMBIGUITY_RE = re.compile(r"\b(?:sarcasm\w*|iron[ií]a|rojito|trol\w*|me\s+pagan|supuestamente)\b")
ADMIN_RE = re.compile(
    r"\b(?:politicas?\s+internas?\s+del\s+canal|mensaje\s+eliminado|"
    r"lenguaje\s+ofensivo|moderadores?\s+del\s+canal)\b"
)
GENERIC_GREETING_RE = re.compile(
    r"^(?:buen(?:os|as)\s+(?:dias|tardes|noches)(?:\s+a\s+todos)?|saludos(?:\s+a\s+todos)?|"
    r"hola(?:\s+a\s+todos)?|muchas\s+gracias|gracias)[.! ]*$"
)

THEME_RULES = {
    "gobierno_ejecutivo": GOVERNMENT_RE,
    "ice_energia": re.compile(r"\b(?:ice|recope|electric\w*|energ[ií]a|tarifa\w*|apag[oó]n|canon\w*)\b"),
    "proyecto_23414": re.compile(r"\b(?:23[ .]?414|23414|armonizaci[oó]n)\b"),
    "elecciones_politica": re.compile(r"\b(?:elecci[oó]n\w*|vot\w*|2030|tse|candidat\w*|partido\w*)\b"),
    "asamblea_legislacion": re.compile(r"\b(?:diputad\w*|asamblea|plenario|comisi[oó]n|proyecto\s+de\s+ley|expediente|moci[oó]n|debate)\b"),
    "partidos_oposicion": re.compile(r"\b(?:pln|liberaci[oó]n\s+nacional|frente\s+amplio|fa|pac|pusc|oficialismo|ppso)\b"),
    "poder_judicial": re.compile(r"\b(?:poder\s+judicial|magistrad\w*|fiscal[ií]a|oij|corte\s+suprema|sala\s+(?:cuarta|constitucional))\b"),
    "corrupcion_transparencia": re.compile(r"\b(?:corrupt\w*|contralor\w*|robo|roban|ladron\w*|mafia|impunidad|transparencia|tr[aá]fico\s+de\s+influencias)\b"),
    "seguridad_narcotrafico": re.compile(r"\b(?:seguridad|narco\w*|sicari\w*|delincuen\w*|polic[ií]a|c[aá]rcel|crimen|extradici[oó]n)\b"),
    "economia_impuestos": re.compile(r"\b(?:econom[ií]a|impuest\w*|iva|presupuesto|deuda|empleo|salari\w*|pensi[oó]n|precio\w*|hacienda)\b"),
    "salud_seguridad_social": re.compile(r"\b(?:salud|caja|ccss|hospital\w*|m[eé]dic\w*|ebais|enfermedad|seguro\s+social)\b"),
    "educacion": re.compile(r"\b(?:educaci[oó]n|escuela\w*|colegio\w*|universidad\w*|u[.]?c[.]?r|fees|estudiante\w*)\b"),
    "ambiente_territorio": re.compile(r"\b(?:ambiente|ambiental\w*|agua|bosque\w*|mar\w*|oc[eé]ano\w*|contamin\w*|guanacaste|lim[oó]n)\b"),
    "infraestructura_transporte": re.compile(r"\b(?:carretera\w*|puente\w*|ruta\s+27|infraestructura|tren|aeropuerto\w*|puerto\w*|transporte)\b"),
    "prensa_medios": re.compile(r"\b(?:prensa|periodist\w*|naci[oó]n|crhoy|teletica|canal\w*|medio\w*)\b"),
    "religion": re.compile(r"\b(?:dios|cristo|bendiga\w*|bendici[oó]n\w*|oraci[oó]n|biblia\w*)\b"),
    "genero_derechos_humanos": re.compile(r"\b(?:mujer\w*|femin\w*|derechos?\s+humanos|discapacidad|adulto\s+mayor|niñez|adolescen\w*)\b"),
}

INSULT_RULES = {
    "insulto_personal": re.compile(r"\b(?:idiota\w*|imb[eé]cil\w*|est[uú]pid\w*|brut\w*|payas\w*|rid[ií]cul\w*|mediocre\w*|in[uú]til\w*|babos\w*|tarad\w*)\b"),
    "deshumanizacion": re.compile(r"\b(?:ratas?|basura|escoria|cerd\w*|par[aá]sit\w*|lacras?|animales?)\b"),
    "acusacion_corrupcion_crimen": re.compile(r"\b(?:corrupt\w*|ladron\w*|mafios\w*|narco\w*|criminal\w*|vendid\w*|traidor\w*|sicari\w*)\b"),
    "acusacion_abuso_sexual": re.compile(r"\b(?:acosador\w*|abusador\w*|violador\w*|ped[oó]fil\w*|depredador\w*|pdfs?)\b"),
    "peyorativo_ideologico": re.compile(r"\b(?:chancletud\w*|chavel\w*|zurdo\w*|fach\w*|peric\w*|comunista\w*|trol\w*)\b"),
    "profanidad_agresion": re.compile(r"\b(?:mierda|carajo|maldit\w*|desgraciad\w*|hij[oa]\s+de\s+puta|hpts?|carepicha)\b"),
}

TARGET_RULES = [
    ("laura_fernandez", "persona", re.compile(r"\b(?:laura\s+fern[aá]ndez|presidenta\s+laura)\b")),
    ("rodrigo_chaves", "persona", re.compile(r"\b(?:rodrigo\s+ch[aá]v(?:es|ez)|don\s+rodrigo|ch[aá]v(?:es|ez))\b")),
    ("pilar_cisneros", "persona", re.compile(r"\b(?:pilar\s+cisneros|doña\s+pilar)\b")),
    ("jose_maria_villalta", "persona", re.compile(r"\b(?:jose\s+maria\s+villalta|villalta)\b")),
    ("sigrid_segura", "persona", re.compile(r"\b(?:sigrid\s+segura|sigrid)\b")),
    ("nogui_acosta", "persona", re.compile(r"\b(?:nogui\s+acosta|nogui)\b")),
    ("fabricio_alvarado", "persona", re.compile(r"\b(?:fabricio\s+alvarado|fabricio)\b")),
    ("gloria_navas", "persona", re.compile(r"\b(?:gloria\s+navas|gloria\s+navas\s+montero)\b")),
    ("diputados_colectivo", "colectivo", re.compile(r"\b(?:diputad\w*|legislador\w*)\b")),
    ("frente_amplio", "organizacion", re.compile(r"\b(?:frente\s+amplio|fraude\s+amplio|fa)\b")),
    ("pln", "organizacion", re.compile(r"\b(?:pln|liberaci[oó]n\s+nacional|pericos?)\b")),
    ("ppso", "organizacion", re.compile(r"\b(?:ppso|pueblo\s+soberano|oficialismo|chavel\w*|chavist\w*)\b")),
    ("poder_judicial", "institucion", re.compile(r"\b(?:poder\s+judicial|fiscal[ií]a|oij|corte\s+suprema)\b")),
    ("tse", "institucion", re.compile(r"\b(?:tse|tribunal\s+supremo\s+de\s+elecciones)\b")),
    ("contraloria", "institucion", re.compile(r"\b(?:contralor[ií]a|contralor\w*)\b")),
    ("ice", "institucion", re.compile(r"\bice\b")),
    ("prensa", "sector", re.compile(r"\b(?:prensa|periodist\w*|medios?\s+de\s+comunicaci[oó]n)\b")),
]


def span_distance(left: Tuple[int, int], right: Tuple[int, int]) -> int:
    if left[1] < right[0]:
        return right[0] - left[1]
    if right[1] < left[0]:
        return left[0] - right[1]
    return 0


def patterns_close(left: re.Pattern, right: re.Pattern, text: str, distance: int = 100) -> bool:
    a = [match.span() for match in left.finditer(text)]
    b = [match.span() for match in right.finditer(text)]
    return any(span_distance(x, y) <= distance for x in a for y in b)


def classify_stance(text: str) -> str:
    # Los títulos aislados ("si fuera presidente") y "fuera del gobierno"
    # producen muchos falsos positivos. Para un nombre/partido exigimos cercanía
    # moderada; para el título genérico, cercanía léxica mucho más estricta.
    pro = bool(DIRECT_PRO_RE.search(text))
    generic_title_pro = bool(TITLE_PRAISE_RE.search(text) and not HYPOTHETICAL_TITLE_RE.search(text))
    generic_title_contra = patterns_close(TITLE_RE, NEGATIVE_RE, text, distance=8)
    contra = (
        patterns_close(NAMED_GOVERNMENT_RE, NEGATIVE_RE, text, distance=45)
        or bool(re.search(
            r"(?<!si )(?<!que )\bfuera\s+(?:el\s+|la\s+)?(?:gobierno|oficialismo|ppso|pueblo\s+soberano|"
            r"laura\s+fern[aá]ndez|rodrigo\s+ch[aá]v(?:es|ez)|ch[aá]v(?:es|ez))\b", text
        ))
        or bool(re.search(
            r"\bno\s+(?:los?\s+|las?\s+)?apoy(?:o|amos)(?:\s+(?:a|al))?\s+(?:este\s+|esta\s+)?"
            r"(?:gobierno|oficialismo|ppso|pueblo\s+soberano|laura\s+fern[aá]ndez|"
            r"rodrigo\s+ch[aá]v(?:es|ez)|ch[aá]v(?:es|ez))\b", text
        ))
    )
    ambiguous = bool(AMBIGUITY_RE.search(text))
    if pro and contra:
        # "Usted apoya las estupideces del oficialismo" contiene "apoya",
        # pero no expresa respaldo. Solo llamamos mixto al texto con una señal
        # positiva fuerte además de la crítica.
        return "mixto"
    if ambiguous and (pro or contra):
        return "ambiguo"
    if pro:
        return "pro_gobierno"
    if contra:
        return "contra_gobierno"
    if generic_title_pro or generic_title_contra:
        return "ambiguo"
    return "sin_postura_clara"


def classify_content(message: Message) -> dict:
    text = message.normalized
    stance = classify_stance(text)
    themes = sorted(name for name, pattern in THEME_RULES.items() if pattern.search(text))
    insult_matches = {
        name: [match for match in pattern.finditer(text)]
        for name, pattern in INSULT_RULES.items()
    }
    insult_matches = {name: matches for name, matches in insult_matches.items() if matches}
    insult_spans = [match.span() for matches in insult_matches.values() for match in matches]
    insult_terms = sorted({match.group(0) for matches in insult_matches.values() for match in matches})
    targets, target_kinds, target_spans = [], {}, defaultdict(list)
    for name, kind, pattern in TARGET_RULES:
        matches = list(pattern.finditer(text))
        if matches:
            targets.append(name); target_kinds[name] = kind
            target_spans[name].extend(match.span() for match in matches)
    for handle in HANDLE_RE.findall(message.text):
        name = "handle:" + handle.casefold()
        targets.append(name); target_kinds[name] = "cuenta_youtube"
        normalized_handle = normalize_text(handle)
        start = text.find(normalized_handle)
        if start >= 0:
            target_spans[name].append((start, start + len(normalized_handle)))
    targets = sorted(set(targets))
    # Vincule cada insulto con el blanco textual más cercano. Esto evita, por
    # ejemplo, atribuir a Villalta un insulto dirigido a "los chavelos" dentro
    # del mismo mensaje corto.
    attacked_set: Set[str] = set()
    for insult_span in insult_spans:
        distances = {
            target: min(span_distance(target_span, insult_span) for target_span in target_spans[target])
            for target in targets if target_spans[target]
        }
        nearby = {target: distance for target, distance in distances.items() if distance <= 80}
        if nearby:
            closest = min(nearby.values())
            attacked_set.update(target for target, distance in nearby.items() if distance <= closest + 4)
    attacked = sorted(attacked_set)
    severe = bool(set(insult_matches) & {"deshumanizacion", "acusacion_abuso_sexual", "profanidad_agresion"})
    insult_level = "alta" if severe else "media" if insult_matches else "ninguna"
    return {
        "stance": stance, "themes": themes,
        "insult_categories": sorted(insult_matches), "insult_terms": insult_terms,
        "insult_level": insult_level, "targets": targets,
        "target_kinds": target_kinds, "attacked_targets": attacked,
    }


def slogan_key(text: str) -> str:
    value = normalize_text(HANDLE_RE.sub("", text))
    value = URL_TOKEN_RE.sub("", value)
    value = re.sub(r"(.)\1{2,}", r"\1\1", value)
    return re.sub(r"\s+", " ", value).strip()


def is_slogan_candidate(text: str) -> bool:
    key = slogan_key(text)
    return bool(
        len(key) >= 12 and len(key.split()) >= 3
        and not ADMIN_RE.search(key)
        and not GENERIC_GREETING_RE.fullmatch(key)
    )


def catalog_metadata(input_path: str) -> Dict[str, dict]:
    path = Path(input_path) / "catalog.jsonl"
    rows = {}
    if path.is_file():
        with path.open(encoding="utf-8") as handle:
            for line in handle:
                row = json.loads(line); value = str(row.get("upload_date") or row.get("published_at") or "")
                day = "{}-{}-{}".format(value[:4], value[4:6], value[6:8]) if len(value) == 8 and value.isdigit() else value[:10]
                rows[row.get("video_id")] = {**row, "analysis_date": day}
    return rows


def stable_id(*values: object) -> str:
    return hashlib.blake2b("|".join(map(str, values)).encode(), digest_size=10).hexdigest()


def join(values: Iterable[str]) -> str:
    return " | ".join(sorted(set(values)))


def safe_share(value: int, total: int) -> float:
    return round(value / float(total), 6) if total else 0.0


def write_report(path: Path, summary: dict, stance_rows: list, theme_rows: list, insult_rows: list, slogans: list, targets: list, accounts: list) -> None:
    c = summary["counts"]
    lines = [
        "# Análisis estadístico de contenido", "",
        "> Clasificación automática para auditoría; no determina intención ni verifica acusaciones.", "",
        "## Cobertura", "",
        "- Mensajes: {:,}".format(c["messages"]),
        "- Cuentas: {:,}".format(c["accounts"]),
        "- Sesiones: {:,}".format(c["sessions"]),
        "- Mensajes con insultos o acusaciones: {:,} ({:.2%})".format(c["messages_with_insults"], c["messages_with_insults"] / c["messages"]),
        "- Mensajes con blanco de ataque explícito: {:,} ({:.2%})".format(c["messages_with_attacked_targets"], c["messages_with_attacked_targets"] / c["messages"]),
        "- Consignas o textos repetidos: {:,}".format(c["slogans"]), "",
        "## Postura frente al Gobierno", "",
        "| Postura | Mensajes | Porcentaje | Cuentas | Sesiones |", "|---|---:|---:|---:|---:|",
    ]
    for row in stance_rows:
        lines.append("| {} | {} | {:.2%} | {} | {} |".format(row["stance"], row["messages"], row["share"], row["accounts"], row["sessions"]))
    lines += ["", "## Temas principales", "", "| Tema | Mensajes | Porcentaje | Cuentas | Sesiones |", "|---|---:|---:|---:|---:|"]
    for row in theme_rows[:15]:
        lines.append("| {} | {} | {:.2%} | {} | {} |".format(row["theme"], row["messages"], row["share"], row["accounts"], row["sessions"]))
    lines += ["", "## Insultos y acusaciones por categoría", "", "| Categoría | Mensajes | Porcentaje del corpus |", "|---|---:|---:|"]
    for row in insult_rows:
        lines.append("| {} | {} | {:.2%} |".format(row["category"], row["messages"], row["share"]))
    lines += ["", "## Consignas principales", "", "| Texto | Mensajes | Cuentas | Sesiones | Cuenta dominante |", "|---|---:|---:|---:|---:|"]
    for row in slogans[:15]:
        lines.append("| {} | {} | {} | {} | {:.1%} |".format(row["representative_text"].replace("|", "\\|")[:150], row["messages"], row["accounts"], row["sessions"], row["dominant_account_share"]))
    lines += ["", "## Blancos de ataque principales", "", "| Blanco | Tipo | Menciones | Ataques | Cuentas atacantes |", "|---|---|---:|---:|---:|"]
    for row in targets[:15]:
        lines.append("| {} | {} | {} | {} | {} |".format(row["target"], row["kind"], row["mentions"], row["attack_messages"], row["attacking_accounts"]))
    lines += ["", "### Personas nombradas", "", "| Persona | Menciones | Ataques candidatos | Cuentas atacantes |", "|---|---:|---:|---:|"]
    for row in sorted((row for row in targets if row["kind"] == "persona"), key=lambda item: (-item["attack_messages"], -item["mentions"])):
        lines.append("| {} | {} | {} | {} |".format(row["target"], row["mentions"], row["attack_messages"], row["attacking_accounts"]))
    lines += ["", "## Cuentas con mayor volumen de insultos o acusaciones", "", "| Cuenta | Mensajes | Insultos/acusaciones | Ataques explícitos | Sesiones |", "|---|---:|---:|---:|---:|"]
    for row in sorted(accounts, key=lambda item: (item["insult_messages"], item["attack_messages"]), reverse=True)[:15]:
        lines.append("| {} | {} | {} | {} | {} |".format(row["author_name"].replace("|", "\\|"), row["messages"], row["insult_messages"], row["attack_messages"], row["sessions"]))
    concentrated = [row for row in accounts if row["messages"] >= 20 and row["insult_messages"] >= 3]
    lines += ["", "### Mayor proporción de mensajes con insultos (mínimo 20 mensajes y 3 coincidencias)", "", "| Cuenta | Mensajes | Coincidencias | Tasa | Sesiones |", "|---|---:|---:|---:|---:|"]
    for row in sorted(concentrated, key=lambda item: (-item["insult_share"], -item["insult_messages"]))[:15]:
        lines.append("| {} | {} | {} | {:.1%} | {} |".format(row["author_name"].replace("|", "\\|"), row["messages"], row["insult_messages"], row["insult_share"], row["sessions"]))
    lines += ["", "Revise `human_validation_sample.csv` antes de publicar porcentajes o atribuciones.", ""]
    path.write_text("\n".join(lines), encoding="utf-8")


def run(args: argparse.Namespace) -> int:
    messages, excluded = filter_messages_to_catalog(list(iter_messages(discover_jsonl(args.input))), args.input)
    if not messages:
        raise SystemExit("No se encontraron mensajes válidos")
    catalog = catalog_metadata(args.input)
    analyses = {id(message): classify_content(message) for message in messages}

    slogan_groups = defaultdict(list)
    for message in messages:
        key = slogan_key(message.text)
        if is_slogan_candidate(message.text):
            slogan_groups[key].append(message)
    qualified = []
    for key, items in slogan_groups.items():
        authors = Counter(item.author_id for item in items)
        sessions = {item.session for item in items}
        if len(items) < 3 or not (len(authors) >= 2 or max(authors.values()) >= 5):
            continue
        representative = Counter(item.text for item in items).most_common(1)[0][0]
        slogan_score = len(items) * (1 + min(len(authors), 10) / 10.0) * (1 + min(len(sessions), 10) / 20.0)
        qualified.append({
            "slogan_id": "S" + stable_id(key)[:8], "slogan_key": key,
            "representative_text": representative, "messages": len(items),
            "accounts": len(authors), "sessions": len(sessions),
            "cross_account": len(authors) >= 2, "cross_session": len(sessions) >= 2,
            "dominant_account_share": round(max(authors.values()) / float(len(items)), 6),
            "top_accounts": " | ".join(author for author, _ in authors.most_common(10)),
            "stances": join(analyses[id(item)]["stance"] for item in items),
            "themes": join(theme for item in items for theme in analyses[id(item)]["themes"]),
            "score": round(slogan_score, 4),
        })
    qualified.sort(key=lambda row: (-row["score"], row["slogan_key"]))
    slogan_map = {row["slogan_key"]: row["slogan_id"] for row in qualified}

    classified_rows = []
    for message in messages:
        result = analyses[id(message)]; metadata = catalog.get(message.session.removesuffix("__comments"), {})
        classified_rows.append({
            "record_id": stable_id(message.session, message.time_ms, message.author_id, message.text),
            "session": message.session, "date": metadata.get("analysis_date", ""),
            "video_title": metadata.get("title", ""), "source": message.source,
            "time_ms": message.time_ms, "author_id": message.author_id,
            "author_name": message.author_name, "text": message.text,
            "normalized_text": message.normalized, "stance": result["stance"],
            "themes": join(result["themes"]), "insult_level": result["insult_level"],
            "insult_categories": join(result["insult_categories"]),
            "insult_terms": join(result["insult_terms"]), "targets": join(result["targets"]),
            "attacked_targets": join(result["attacked_targets"]),
            "slogan_id": slogan_map.get(slogan_key(message.text), ""),
        })

    def aggregate(label: str, values: Iterable[str]) -> list:
        grouped = defaultdict(list)
        for message, value in zip(messages, values):
            if value:
                grouped[value].append(message)
        rows = [{
            label: value, "messages": len(items), "share": safe_share(len(items), len(messages)),
            "accounts": len({item.author_id for item in items}), "sessions": len({item.session for item in items}),
        } for value, items in grouped.items()]
        rows.sort(key=lambda row: (-row["messages"], row[label])); return rows

    stance_rows = aggregate("stance", (analyses[id(message)]["stance"] for message in messages))
    theme_items = [(message, theme) for message in messages for theme in analyses[id(message)]["themes"]]
    theme_grouped = defaultdict(list)
    for message, theme in theme_items: theme_grouped[theme].append(message)
    theme_rows = [{"theme": theme, "messages": len(items), "share": safe_share(len(items), len(messages)), "accounts": len({item.author_id for item in items}), "sessions": len({item.session for item in items})} for theme, items in theme_grouped.items()]
    theme_rows.sort(key=lambda row: (-row["messages"], row["theme"]))

    matrix = Counter()
    for message in messages:
        result = analyses[id(message)]
        for theme in result["themes"] or ["sin_tema"]:
            matrix[(theme, result["stance"])] += 1
    matrix_rows = [{"theme": theme, "stance": stance, "messages": count} for (theme, stance), count in matrix.items()]
    matrix_rows.sort(key=lambda row: (row["theme"], -row["messages"], row["stance"]))

    target_stats = {}
    for message in messages:
        result = analyses[id(message)]
        for target in result["targets"]:
            stat = target_stats.setdefault(target, {"mentions": 0, "attack_messages": 0, "authors": set(), "attackers": set(), "kind": result["target_kinds"].get(target, "")})
            stat["mentions"] += 1; stat["authors"].add(message.author_id)
            if target in result["attacked_targets"]:
                stat["attack_messages"] += 1; stat["attackers"].add(message.author_id)
    target_rows = [{"target": target, "kind": stat["kind"], "mentions": stat["mentions"], "attack_messages": stat["attack_messages"], "attack_share_of_mentions": safe_share(stat["attack_messages"], stat["mentions"]), "mentioning_accounts": len(stat["authors"]), "attacking_accounts": len(stat["attackers"])} for target, stat in target_stats.items()]
    target_rows.sort(key=lambda row: (-row["attack_messages"], -row["mentions"], row["target"]))

    insult_counter = Counter()
    for message in messages:
        for category in analyses[id(message)]["insult_categories"]:
            insult_counter[category] += 1
    insult_rows = [{"category": category, "messages": count, "share": safe_share(count, len(messages))} for category, count in insult_counter.most_common()]

    account_grouped = defaultdict(list)
    for message in messages: account_grouped[message.author_id].append(message)
    account_rows = []
    for author, items in account_grouped.items():
        results = [analyses[id(item)] for item in items]
        stances = Counter(result["stance"] for result in results)
        themes = Counter(theme for result in results for theme in result["themes"])
        account_rows.append({
            "author_id": author, "author_name": items[-1].author_name, "messages": len(items),
            "sessions": len({item.session for item in items}),
            "pro_government": stances["pro_gobierno"], "contra_government": stances["contra_gobierno"],
            "mixed_or_ambiguous": stances["mixto"] + stances["ambiguo"],
            "insult_messages": sum(bool(result["insult_categories"]) for result in results),
            "attack_messages": sum(bool(result["attacked_targets"]) for result in results),
            "slogan_messages": sum(slogan_key(item.text) in slogan_map for item in items),
            "top_themes": " | ".join(theme for theme, _ in themes.most_common(5)),
        })
        account_rows[-1].update({
            "pro_share": safe_share(account_rows[-1]["pro_government"], len(items)),
            "contra_share": safe_share(account_rows[-1]["contra_government"], len(items)),
            "insult_share": safe_share(account_rows[-1]["insult_messages"], len(items)),
            "attack_share": safe_share(account_rows[-1]["attack_messages"], len(items)),
            "slogan_share": safe_share(account_rows[-1]["slogan_messages"], len(items)),
        })
    account_rows.sort(key=lambda row: (-row["messages"], row["author_id"]))

    session_grouped = defaultdict(list)
    for message in messages: session_grouped[message.session].append(message)
    session_rows = []
    for session, items in session_grouped.items():
        results = [analyses[id(item)] for item in items]; metadata = catalog.get(session.removesuffix("__comments"), {})
        session_rows.append({
            "session": session, "date": metadata.get("analysis_date", ""), "title": metadata.get("title", ""),
            "messages": len(items), "accounts": len({item.author_id for item in items}),
            "pro_government": sum(result["stance"] == "pro_gobierno" for result in results),
            "contra_government": sum(result["stance"] == "contra_gobierno" for result in results),
            "insult_messages": sum(bool(result["insult_categories"]) for result in results),
            "attack_messages": sum(bool(result["attacked_targets"]) for result in results),
        })
        session_rows[-1].update({
            "pro_share": safe_share(session_rows[-1]["pro_government"], len(items)),
            "contra_share": safe_share(session_rows[-1]["contra_government"], len(items)),
            "insult_share": safe_share(session_rows[-1]["insult_messages"], len(items)),
            "attack_share": safe_share(session_rows[-1]["attack_messages"], len(items)),
        })
    session_rows.sort(key=lambda row: (row["date"], row["session"]))

    review_candidates = defaultdict(list)
    for message in messages:
        result = analyses[id(message)]
        review_candidates["stance:" + result["stance"]].append(message)
        if result["insult_categories"]: review_candidates["insulto"].append(message)
        if result["attacked_targets"]: review_candidates["ataque_dirigido"].append(message)
        if slogan_key(message.text) in slogan_map: review_candidates["consigna"].append(message)
    selected, sample = set(), []
    for stratum in sorted(review_candidates):
        ranked = sorted(review_candidates[stratum], key=lambda item: stable_id(stratum, item.author_id, item.session, item.text))
        for message in ranked:
            key = id(message)
            if key in selected: continue
            selected.add(key); result = analyses[key]
            sample.append({"stratum": stratum, "human_stance": "", "human_insult": "", "human_targets": "", "reviewer_notes": "", "author_name": message.author_name, "session": message.session, "text": message.text, "proposed_stance": result["stance"], "proposed_insults": join(result["insult_categories"]), "proposed_targets": join(result["attacked_targets"])})
            if sum(row["stratum"] == stratum for row in sample) >= args.validation_per_stratum: break

    output = Path(args.output); output.mkdir(parents=True, exist_ok=True)
    write_csv(output / "message_classifications.csv", classified_rows, list(classified_rows[0]))
    write_csv(output / "stance_stats.csv", stance_rows, ["stance", "messages", "share", "accounts", "sessions"])
    write_csv(output / "theme_stats.csv", theme_rows, ["theme", "messages", "share", "accounts", "sessions"])
    write_csv(output / "theme_stance_matrix.csv", matrix_rows, ["theme", "stance", "messages"])
    write_csv(output / "insult_stats.csv", insult_rows, ["category", "messages", "share"])
    write_csv(output / "target_stats.csv", target_rows, ["target", "kind", "mentions", "attack_messages", "attack_share_of_mentions", "mentioning_accounts", "attacking_accounts"])
    write_csv(output / "slogans.csv", qualified, list(qualified[0]) if qualified else ["slogan_id", "representative_text", "messages", "accounts", "sessions"])
    write_csv(output / "account_content_profiles.csv", account_rows, list(account_rows[0]))
    write_csv(output / "session_content_stats.csv", session_rows, list(session_rows[0]))
    write_csv(output / "human_validation_sample.csv", sample, list(sample[0]) if sample else ["stratum", "text"])

    counts = {
        "messages": len(messages), "accounts": len(account_grouped), "sessions": len(session_grouped),
        "excluded_outside_catalog": excluded,
        "messages_with_insults": sum(bool(analyses[id(message)]["insult_categories"]) for message in messages),
        "messages_with_attacked_targets": sum(bool(analyses[id(message)]["attacked_targets"]) for message in messages),
        "messages_with_any_target": sum(bool(analyses[id(message)]["targets"]) for message in messages),
        "slogans": len(qualified), "slogan_messages": sum(slogan_key(message.text) in slogan_map for message in messages),
        "cross_account_slogans": sum(row["cross_account"] for row in qualified),
        "cross_session_slogans": sum(row["cross_session"] for row in qualified),
        "validation_sample": len(sample),
    }
    summary = {
        "tool_version": VERSION, "input": os.path.abspath(args.input), "counts": counts,
        "stance": {row["stance"]: row["messages"] for row in stance_rows},
        "top_themes": theme_rows[:15], "insult_categories": insult_rows,
        "parameters": {"stance_method": "apoyo explícito y crítica próxima a entidades del Ejecutivo; títulos sin nombre se marcan ambiguos", "attack_distance_characters": 80, "attack_nearest_tolerance_characters": 4, "slogan_min_messages": 3, "validation_per_stratum": args.validation_per_stratum},
        "warning": "Resultados automáticos para validación humana; una coincidencia no verifica intención ni acusaciones.",
    }
    (output / "content_summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    write_report(output / "content_report.md", summary, stance_rows, theme_rows, insult_rows, qualified, target_rows, account_rows)
    print(json.dumps(summary, ensure_ascii=False, indent=2)); return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("input")
    parser.add_argument("--output", default="text_analysis")
    parser.add_argument("--validation-per-stratum", type=int, default=100)
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    if args.validation_per_stratum <= 0:
        raise SystemExit("--validation-per-stratum debe ser positivo")
    return run(args)


if __name__ == "__main__":
    raise SystemExit(main())
