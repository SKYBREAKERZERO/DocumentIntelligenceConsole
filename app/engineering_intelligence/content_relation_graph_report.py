"""Stage 2 local, read-only, two-ended content relation graph snapshots.

No inference, vector matching, write access or implicit approval is performed.
The HTML is entirely static: full source evidence is HTML-escaped, with no JS,
remote fonts, CDN, or network requests. Treat the export as sensitive data.
"""
from __future__ import annotations

import hashlib
import html
import json
import os
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Iterable

from .content_relation_review import (
    ContentRelationReviewCase,
    ContentRelationReviewError,
    list_relation_queue,
    load_review_case,
)
from .requirements import normalize_text
from .relations import find_reference_anchor

GRAPH_FORMAT = "dic-content-relation-graph-v1"
ALLOWED_STATUSES = frozenset({"REVIEW_REQUIRED", "CONFIRMED", "REJECTED", "STALE"})
MAX_EXPORT = 200


def _canonical(data: Any) -> bytes:
    return (json.dumps(data, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")


def _hash(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def build_graph_snapshot(
    cur: Any, *, project: str, statuses: Iterable[str] = ("REVIEW_REQUIRED",),
    limit: int = 50, exported_at: str | None = None,
) -> dict[str, Any]:
    """Collect candidates under the CALLER'S repeatable-read, read-only transaction.

    Invalid candidates are retained as quarantine diagnostics without exporting
    their unverified content. The caller must validate DB project_info first.
    """
    status_set = tuple(dict.fromkeys(statuses))
    if (not project or project.upper() in {"QABUG", "RAG_QABUG"}
            or not status_set or any(s not in ALLOWED_STATUSES for s in status_set)
            or not 1 <= limit <= MAX_EXPORT):
        raise ValueError("An explicit engineering project, valid statuses and 1..200 limit are required")
    cases: list[dict[str, Any]] = []
    quarantined: list[dict[str, Any]] = []
    seen: set[int] = set()
    for status in status_set:
        remaining = limit - len(seen)
        if not remaining:
            break
        queue = list_relation_queue(cur, project=project, status=status, limit=remaining)
        for entry in queue:
            candidate_id = int(entry["candidate_id"])
            if candidate_id in seen:
                continue
            seen.add(candidate_id)
            try:
                case = load_review_case(cur, candidate_id=candidate_id, project=project)
                # Even if the database changes after this transaction, this
                # historical snapshot never authorizes a new RAG answer.
                record = asdict(case)
                record["rag_eligible_at_snapshot"] = (
                    case.status == "CONFIRMED"
                    and case.review_identity_provenance == "DB_SESSION_USER"
                )
                cases.append(record)
            except ContentRelationReviewError as exc:
                quarantined.append({
                    "candidate_id": candidate_id,
                    "reported_status": str(entry.get("status") or status),
                    "reason": str(exc),
                    "evidence_excluded": True,
                })
    cases.sort(key=lambda r: r["candidate_id"])
    quarantined.sort(key=lambda r: r["candidate_id"])
    return {
        "format": GRAPH_FORMAT,
        "project": project,
        "snapshot_utc": exported_at or datetime.now(UTC).isoformat(),
        "requested_statuses": list(status_set),
        "limit": limit,
        "counts": {"validated": len(cases), "quarantined": len(quarantined), "examined": len(seen)},
        "relations": cases,
        "quarantined": quarantined,
        "note": "Read-only historical review snapshot; not a live authorization or a database audit signature.",
    }


def _esc(value: Any) -> str:
    return html.escape(str(value if value is not None else ""), quote=True)


def _endpoint_card(endpoint: dict[str, Any], side: str) -> str:
    title = (f"{endpoint['specification_code']} · {endpoint['version']} · "
             f"§{endpoint['section_id']} / p.{endpoint['page_number']}")
    provenance = (
        f"文件: {endpoint['file_name']}\n"
        f"document_id: {endpoint['document_id']}\n"
        f"unit_key: {endpoint['unit_key']}\n"
        f"content_id: {endpoint['content_id']} · "
        f"Unicode offsets: [{endpoint['start_offset']}, {endpoint['end_offset']})\n"
        f"SHA-256(normalize_text): {endpoint['text_hash']}"
    )
    return (f'<section class="node"><h4>{_esc(side)} · {_esc(title)}</h4>'
            f'<pre class="source">{_esc(endpoint["raw_text"])}</pre>'
            f'<pre class="provenance">{_esc(provenance)}</pre></section>')




def _render_topology(snapshot: dict[str, Any]) -> str:
    """SVG adjacency preview of *content units* rather than document-only nodes.

    The same content unit may be shown in both source and target lanes; this
    bipartite view preserves direction and avoids a misleading auto-layout.
    Every rendered edge links to its complete two-ended evidence below.
    """
    rows = snapshot["relations"][:40]
    if not rows:
        return "<p>当前无可展示的已验证内容关系。</p>"
    source_nodes: dict[tuple[str, str], dict[str, Any]] = {}
    target_nodes: dict[tuple[str, str], dict[str, Any]] = {}
    for r in rows:
        for lane, catalog in (("source", source_nodes), ("target", target_nodes)):
            ep = r[lane]
            catalog.setdefault((ep["document_id"], ep["unit_key"]), ep)
    left_pos = {key: 62 + 74 * idx for idx, key in enumerate(source_nodes)}
    right_pos = {key: 62 + 74 * idx for idx, key in enumerate(target_nodes)}
    height = max(200, 124 + 74 * max(len(left_pos), len(right_pos)))
    parts = [f'<svg class="topology" xmlns="http://www.w3.org/2000/svg" viewBox="0 0 1000 {height}" '
             'role="img" aria-label="已验证的双端内容单元关联拓扑">',
             '<text x="30" y="24" class="lane-title">SOURCE 单元</text>',
             '<text x="626" y="24" class="lane-title">TARGET 单元</text>']
    for r in rows:
        s, t = r["source"], r["target"]
        sy = left_pos[(s["document_id"], s["unit_key"])]
        ty = right_pos[(t["document_id"], t["unit_key"])]
        title = _esc(f'#{r["candidate_id"]} {r["status"]}: {r["relation_type"]}')
        parts.append(f'<a href="#relation-{int(r["candidate_id"])}">'
                     f'<title>{title}</title><path d="M390 {sy} C495 {sy}, 520 {ty}, 626 {ty}" '
                     'fill="none" stroke="#648ba8" stroke-width="3"/></a>')
    for catalog, positions, x in ((source_nodes, left_pos, 24), (target_nodes, right_pos, 626)):
        for key, ep in catalog.items():
            y = positions[key]
            full = f'{ep["specification_code"]} {ep["version"]} §{ep["section_id"]} [{ep["unit_key"]}]'
            small = full if len(full) < 42 else full[:39] + "…"
            parts.append(f'<g><title>{_esc(full)}</title>'
                         f'<rect x="{x}" y="{y-23}" rx="6" width="364" height="46" '
                         'fill="#edf5fa" stroke="#618bab"/>'
                         f'<text x="{x+9}" y="{y+5}" class="node-label">{_esc(small)}</text></g>')
    parts.append('</svg>')
    return (f'<p class="meta">内容单元拓扑预览：{len(rows)}/{len(snapshot["relations"])} 条关系；'
            '点击连线进入证据详情。图按 source/target 分区，同一单元可能出现在两侧。</p>'
            '<div class="topology-scroll">' + "".join(parts) + '</div>')


def render_graph_html(snapshot: dict[str, Any]) -> bytes:
    """Render an offline content-edge workspace using inert escaped text only."""
    if snapshot.get("format") != GRAPH_FORMAT:
        raise ValueError("Unrecognized graph format")
    relations = snapshot["relations"]
    summary = []
    details = []
    for row in relations:
        candidate_id = int(row["candidate_id"])
        src, dst = row["source"], row["target"]
        left = f"{src['specification_code']} {src['version']} §{src['section_id']}"
        right = f"{dst['specification_code']} {dst['version']} §{dst['section_id']}"
        status = str(row["status"])
        summary.append(
            f'<tr><td><a href="#relation-{candidate_id}">#{candidate_id}</a></td>'
            f'<td>{_esc(left)}</td><td>→</td><td>{_esc(right)}</td>'
            f'<td>{_esc(row["relation_type"])}</td><td>{_esc(status)}</td></tr>'
        )
        details.append(
            f'<details id="relation-{candidate_id}" class="relation">'
            f'<summary>#{candidate_id} · {_esc(status)} · {_esc(left)} → {_esc(right)}</summary>'
            f'<p><b>关系类型:</b> {_esc(row["relation_type"])} · <b>候选分数:</b> {_esc(row["score"])}'
            f' · <b>快照时允许进入 RAG:</b> {"是" if row["rag_eligible_at_snapshot"] else "否"}</p>'
            f'<p><b>关联原因:</b> {_esc(row["rationale"])}</p>'
            f'<p><b>源文本引用定位:</b> {_esc((row.get("reference_anchor") or {}).get("raw_text") or "未核验")}</p>'
            f'<p><b>解释边界:</b> 仅已核验的跨式样书章节引用；不是已证实的功能因果关系或变更影响。</p>'
            f'<div class="edge">{_endpoint_card(src, "SOURCE")}'
            f'<div class="arrow" aria-label="方向：source 到 target">→</div>'
            f'{_endpoint_card(dst, "TARGET")}</div></details>'
        )
    quarantine = "".join(
        f'<tr><td>{int(q["candidate_id"])}</td><td>{_esc(q["reported_status"])}</td>'
        f'<td>{_esc(q["reason"])}</td></tr>' for q in snapshot["quarantined"]
    )
    # No script tags and no dynamic resource loads; CSP also blocks all network
    # and browser-active content from source paragraphs.
    page = f'''<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8"/>
<meta http-equiv="Content-Security-Policy" content="default-src 'none'; style-src 'unsafe-inline'; img-src 'none'; form-action 'none'; base-uri 'none'"/>
<meta name="referrer" content="no-referrer"/>
<meta name="viewport" content="width=device-width,initial-scale=1"/>
<title>Content Relation Graph · {_esc(snapshot['project'])}</title>
<style>
:root{{font-family:system-ui,'Microsoft YaHei',sans-serif;color:#1c2936;background:#f4f7fa}}
body{{max-width:1440px;margin:auto;padding:22px;line-height:1.52}}h1{{margin-bottom:6px}}
.notice{{border-left:4px solid #cd732d;background:#fff5e9;padding:12px 16px}}
.meta{{color:#526778}}table{{border-collapse:collapse;width:100%;background:white}}
th,td{{border:1px solid #d8e1ea;padding:9px;text-align:left;overflow-wrap:anywhere}}
th{{background:#eaf0f5}}a{{color:#145b9f}}.relation{{background:white;border:1px solid #d8e1ea;border-radius:8px;margin:12px 0;padding:14px}}
summary{{cursor:pointer;font-weight:bold;overflow-wrap:anywhere}}.edge{{display:flex;align-items:stretch;gap:12px;overflow-wrap:anywhere}}
.node{{border:2px solid #9bb9cf;border-radius:8px;min-width:0;flex:1;padding:10px;background:#f9fcff}}
.node h4{{margin:0 0 12px}}.topology-scroll{{max-height:1100px;overflow:auto;background:white;border:1px solid #d8e1ea}}.topology{{width:100%;min-width:710px;height:auto}}.lane-title{{font-size:15px;font-weight:bold}}.node-label{{font-size:13px}}pre{{white-space:pre-wrap;overflow-wrap:anywhere;word-break:break-word}}
.source{{border-left:3px solid #367d9d;padding:10px;background:white;font-size:14px;max-height:340px;overflow:auto}}
.provenance{{font-size:12px;color:#526274}}.arrow{{align-self:center;font-size:30px;color:#476b7b}}
@media(max-width:790px){{.edge{{flex-direction:column}}.arrow{{transform:rotate(90deg)}}}}
</style></head><body>
<h1>Stage 2 · Content Relation Graph</h1>
<p class="meta">工程项目: {_esc(snapshot['project'])} · 数据库一致性快照时间 UTC: {_esc(snapshot['snapshot_utc'])}</p>
<div class="notice"><b>本报告不是审核授权。</b> 显示的是导出时的只读快照；未审核、已拒绝关系不得进入 RAG。
不合格的来源已隔离且原文不展示。文件包含内部技术内容，不得公开传播或上传外部网站。
SHA-256 只检测导出后修改，不能证明审核人身份或防止整体文件被重签。</div>
<p>已检验: {_esc(snapshot['counts']['examined'])} · 证据完整: {_esc(snapshot['counts']['validated'])} · 隔离: {_esc(snapshot['counts']['quarantined'])}</p>
<h2>内容单元拓扑</h2>{_render_topology(snapshot)}
<h2>关系索引</h2><table><thead><tr><th>ID</th><th>来源</th><th></th><th>目标</th><th>关系类型</th><th>审核状态</th></tr></thead>
<tbody>{''.join(summary)}</tbody></table>
<h2>双端原文与内容级边</h2>{''.join(details) or '<p>此状态/范围无已验证的内容关系。</p>'}
<h2>证据隔离清单</h2><table><thead><tr><th>候选 ID</th><th>原状态</th><th>隔离原因（不含原文）</th></tr></thead>
<tbody>{quarantine}</tbody></table>
<p class="meta">静态只读 HTML；无外部 CDN、脚本、网络请求或审核写入操作。</p></body></html>'''
    return page.encode("utf-8")


def _write_private(path: Path, payload: bytes) -> None:
    # Exclusive creation avoids silently replacing an earlier reviewed export.
    with path.open("xb") as stream:
        os.chmod(path, 0o600)
        stream.write(payload)
        stream.flush()
        os.fsync(stream.fileno())


def write_graph_bundle(snapshot: dict[str, Any], output: Path) -> dict[str, str]:
    """Publish a newly named private directory; refuse overwrite/symlink reuse."""
    if snapshot.get("format") != GRAPH_FORMAT:
        raise ValueError("Unrecognized graph format")
    output = Path(output)
    if not output.parent.is_dir() or output.exists() or output.is_symlink():
        raise FileExistsError("Output must be a new directory under an existing parent")
    output.mkdir(mode=0o700, parents=False, exist_ok=False)
    report = {"graph.json": _canonical(snapshot), "index.html": render_graph_html(snapshot)}
    hashes = {name: _hash(payload) for name, payload in report.items()}
    manifest = {
        "format": GRAPH_FORMAT,
        "project": snapshot["project"],
        "snapshot_utc": snapshot["snapshot_utc"],
        "sha256": hashes,
        "warning": "Tamper-evident local hashes, not a trusted digital signature.",
    }
    for name, payload in report.items():
        _write_private(output / name, payload)
    _write_private(output / "manifest.json", _canonical(manifest))
    return hashes


def verify_graph_bundle(directory: Path) -> dict[str, Any]:
    """Offline file checks, source-span digest checks and HTML regeneration.

    This is not proof against coordinated replacement of the entire bundle.
    """
    directory = Path(directory)
    manifest = json.loads((directory / "manifest.json").read_text(encoding="utf-8"))
    if manifest.get("format") != GRAPH_FORMAT or set(manifest.get("sha256", {})) != {"graph.json", "index.html"}:
        raise ValueError("Unexpected manifest format or files")
    raw = (directory / "graph.json").read_bytes()
    page = (directory / "index.html").read_bytes()
    if (_hash(raw) != manifest["sha256"]["graph.json"] or
            _hash(page) != manifest["sha256"]["index.html"]):
        raise ValueError("Bundle hash mismatch")
    graph = json.loads(raw)
    if graph.get("format") != GRAPH_FORMAT or graph.get("project") != manifest.get("project") or graph.get("snapshot_utc") != manifest.get("snapshot_utc"):
        raise ValueError("Graph/manifest identity mismatch")
    if _canonical(graph) != raw or render_graph_html(graph) != page:
        raise ValueError("Noncanonical graph or HTML does not match its graph data")
    if graph.get("counts") != {
        "validated": len(graph["relations"]), "quarantined": len(graph["quarantined"]),
        "examined": len(graph["relations"]) + len(graph["quarantined"]),
    }:
        raise ValueError("Graph counts inconsistent")
    ids: set[int] = set()
    for r in graph["relations"]:
        rid = r["candidate_id"]
        anchor = r.get("reference_anchor")
        if anchor is not None:
            matched = find_reference_anchor(
                r["source"]["raw_text"], r["target"]["specification_code"],
                r["target"]["section_id"],
            )
            if matched is None or asdict(matched) != anchor:
                raise ValueError("Exported reference anchor does not match original evidence")
        if rid in ids or r["rag_eligible_at_snapshot"] != (
            r["status"] == "CONFIRMED" and r.get("review_identity_provenance") == "DB_SESSION_USER"
        ):
            raise ValueError("Repeated relation ID or inconsistent review status")
        ids.add(rid)
        for side in ("source", "target"):
            endpoint = r[side]
            evidence = endpoint["raw_text"]
            if (not evidence or endpoint["end_offset"] <= endpoint["start_offset"] or
                    endpoint["end_offset"] - endpoint["start_offset"] != len(evidence) or
                    _hash(normalize_text(evidence).encode("utf-8")) != endpoint["text_hash"]):
                raise ValueError(f"{rid}/{side}: source evidence hash or offsets invalid")
    for q in graph["quarantined"]:
        if q["candidate_id"] in ids or q.get("evidence_excluded") is not True:
            raise ValueError("Quarantine duplicated or not marked excluded")
        ids.add(q["candidate_id"])
    return {"verified": True, "project": graph["project"], "counts": graph["counts"],
            "note": "Locally self-consistent, not a signed provenance or live database attestation."}
