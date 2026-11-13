"""Constrain enterprise conclusions to findings tied to admitted source text."""
import json
import re

NAMED_VARIANT = re.compile(
    r"(?<![A-Za-z0-9])(?:type|class|mode|profile|variant)[ \t_-]+[A-Z][0-9]{0,3}(?![A-Za-z0-9])",
    re.I,
)
SOURCE_MARKER = re.compile(r"\[\s*E\s*(\d+)\s*\]", re.I)


def _variants(text):
    return {re.sub(r"[ \t_-]+", " ", match.group()).casefold(): match.group()
            for match in NAMED_VARIANT.finditer(text)}


def draft_from_evidence(client, *, model, instruction, request, sources, num_ctx,
                        keep_alive, max_tokens, gap_label, question=""):
    passages = {}
    for source, text in sources.items():
        spans = re.split(r"(?<=[。！？])|(?<=[.!?])\s+|\n{2,}|\n(?=[a-z_]+: )", text)
        for index, span in enumerate((span.strip() for span in spans if span.strip()), 1):
            passages[f"{source}.{index}"] = span
    fields = {
        "topic": {"type": "string"},
        "status": {"type": "string", "enum": ["supported", "unconfirmed"]},
        "conclusion": {"type": "string"},
        "basis": {"type": "string"},
        "passage": {"type": "string", "enum": ["", *passages]},
    }
    schema = {
        "type": "object",
        "properties": {
            "points": {"type": "array", "minItems": 1, "maxItems": 8, "items": {
                "type": "object", "properties": fields, "required": list(fields),
                "additionalProperties": False,
            }},
        },
        "required": ["points"],
        "additionalProperties": False,
    }
    result = client.chat_json(
        model=model, num_ctx=num_ctx, keep_alive=keep_alive, max_tokens=max_tokens,
        schema=schema, messages=[
            {"role": "system", "content": instruction},
            {"role": "user", "content": request + "\n\n" + json.dumps(passages, ensure_ascii=False)},
        ],
    )
    points = result.get("points")
    if not isinstance(points, list) or not 1 <= len(points) <= 8:
        raise ValueError("Return one point per requested topic.")
    conclusions, explanations, anchors = [], [], []
    requested = _variants(question)
    available = _variants("\n".join(sources.values()))
    for finding in points:
        if not isinstance(finding, dict) or any(
                not isinstance(finding.get(field), str) for field in fields):
            raise ValueError("Each point must match the requested fields.")
        topic = finding["topic"].strip()
        if not topic or len(topic) > 100 or re.search(r"[\n<>\[\]]", topic):
            raise ValueError("Use a short topic label for each requested point.")
        topic_variants = _variants(topic)
        if requested and topic_variants and not requested.keys() & topic_variants.keys():
            continue
        if finding["status"] == "unconfirmed" or (
                topic_variants and topic_variants.keys() <= requested.keys()
                and not topic_variants.keys() & available.keys()):
            conclusions.append(f"{gap_label} {topic}")
            continue
        if finding["status"] != "supported":
            raise ValueError("Mark each topic supported or unconfirmed.")
        passage = finding["passage"].strip()
        if passage not in passages:
            raise ValueError("Select the ID of a supplied passage that directly supports the point.")
        marker = passage.split(".", 1)[0]
        conclusion, basis = finding["conclusion"].strip(), finding["basis"].strip()
        if not conclusion or not basis:
            raise ValueError("A supported point needs both a conclusion and an explanation.")
        if (set(SOURCE_MARKER.findall(conclusion + "\n" + basis)) - {marker}
                or re.search(r"</?details\b", conclusion + basis, re.I)):
            raise ValueError("A finding may only reference its selected passage.")
        conclusion = SOURCE_MARKER.sub("", conclusion).strip()
        basis = SOURCE_MARKER.sub("", basis).strip()
        if not conclusion or not basis:
            raise ValueError("A supported point needs explanatory text in addition to its source.")
        conclusions.append(f"{conclusion} [E{marker}]")
        explanations.append(f"- {basis} [E{marker}]")
        anchors.append(f"[E{marker}] {passages[passage]}")
    covered = _variants("\n".join(conclusions))
    for normalized, label in requested.items():
        if normalized in covered:
            continue
        if normalized in available:
            raise ValueError(f"Address the requested topic {label} explicitly.")
        conclusions.append(f"{gap_label} {label}")
    conclusion = conclusions[0] if len(conclusions) == 1 else "\n".join(f"- {point}" for point in conclusions)
    return conclusion, "\n".join(explanations), anchors


def draft_from_ticket_fields(client, *, model, instruction, request, sources, num_ctx,
                             keep_alive, max_tokens, gap_label):
    fields = {key: {"type": "string"} for key in ("conclusion", "basis", "source", "quote")}
    fields["source"]["enum"] = list(sources)
    schema = {
        "type": "object",
        "properties": {
            "findings": {"type": "array", "minItems": 1, "maxItems": 6, "items": {
                "type": "object", "properties": fields, "required": list(fields),
                "additionalProperties": False,
            }},
            "gaps": {"type": "array", "maxItems": 6, "items": {"type": "string"}},
        },
        "required": ["findings", "gaps"], "additionalProperties": False,
    }
    result = client.chat_json(model=model, num_ctx=num_ctx, keep_alive=keep_alive,
        max_tokens=max_tokens, schema=schema, messages=[
            {"role": "system", "content": instruction},
            {"role": "user", "content": request + "\n\n" + json.dumps(sources, ensure_ascii=False)},
        ])
    findings, gaps = result.get("findings"), result.get("gaps")
    if not isinstance(findings, list) or not 1 <= len(findings) <= 6 or not isinstance(gaps, list) or len(gaps) > 6:
        raise ValueError("Return findings and gaps in the requested structure.")
    conclusions, explanations = [], []
    for item in findings:
        if not isinstance(item, dict) or any(
                not isinstance(item.get(field), str) or not item[field].strip() for field in fields):
            raise ValueError("Each finding needs a conclusion, basis, source and literal quote.")
        marker, quote = item["source"].strip(), " ".join(item["quote"].split())
        if marker not in sources or len(quote) < 8 or quote not in " ".join(sources[marker].split()):
            raise ValueError("Copy the relevant ticket field verbatim from the selected source.")
        conclusion, basis = item["conclusion"].strip(), item["basis"].strip()
        if set(SOURCE_MARKER.findall(conclusion + "\n" + basis)) - {marker} or re.search(
                r"</?details\b", conclusion + basis, re.I):
            raise ValueError("A finding may only reference its selected record.")
        conclusions.append(SOURCE_MARKER.sub("", conclusion).strip())
        explanations.append(f"- {SOURCE_MARKER.sub('', basis).strip()} [E{marker}]")
    for gap in gaps:
        if not isinstance(gap, str) or not gap.strip() or len(gap) > 100 or re.search(r"[\n<>\[\]]", gap):
            raise ValueError("A gap must be a short label for the unconfirmed point.")
        conclusions.append(f"{gap_label} {gap.strip()}")
    conclusion = conclusions[0] if len(conclusions) == 1 else "\n".join(f"- {point}" for point in conclusions)
    return conclusion, "\n".join(explanations), []
