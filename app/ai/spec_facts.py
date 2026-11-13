"""Deterministic SQL-backed specification facts for CPU-only enterprise RAG.

No Ollama or embedding calls. A query alias helps search only: it never proves
that a name, number, title or version refers to a document until PostgreSQL
returns the matching SPEC_BODY version. No cover sheet fallback is permitted.
"""
from __future__ import annotations

import re
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from app.ai.retrieval import EnterpriseRetriever, EvidenceItem, RetrievalResult


_VERSION = re.compile(r"(?<![A-Za-z0-9])(?:version|ver|v)\.?\s*(\d+(?:\.\d+){1,3})", re.I)
_SPEC_NEAR = re.compile(
    r"(?<![\dA-Za-z])(?!(?:19|20)\d\d\b)(\d{3,6})(?=\s*(?:BT|Bluetooth|蓝牙|藍牙|ブルートゥース|式样|式樣|仕様|规格|規格|spec))",
    re.I,
)


def requested_version(question: str) -> str | None:
    match = _VERSION.search(str(question or ""))
    return match.group(1).casefold() if match else None


def lookup_keywords(question: str) -> tuple[str, ...]:
    """Lookup candidates, NOT authoritative identifier mappings."""
    q = str(question or "")
    if re.search(r"(?<![A-Za-z0-9])BT(?![A-Za-z0-9])|bluetooth|蓝牙|藍牙|ブルートゥース", q, re.I):
        return ("Bluetooth Audio", "Bluetooth")
    # Extract the requested title, independent of its language.  Project names,
    # request verbs and the generic "specification" noun describe the operation,
    # not the registered document name.  Keeping the remaining phrase intact is
    # important: splitting a title into OR terms makes common words such as
    # "User" hide the intended body among unrelated rows.
    stripped = _VERSION.sub(" ", q)
    stripped = re.sub(r"(?<![A-Za-z0-9])(?:21MM|24MM|COMMON)(?![A-Za-z0-9])", " ", stripped, flags=re.I)
    stripped = re.sub(
        r"(?:帮我|幫我|请|請|麻烦|麻煩|给我|給我)?\s*"
        r"(?:查一下|查询|查詢|查找|搜索|搜一下|找一下|找|打开|打開|看看|定位|"
        r"find|search|lookup|open)\s*",
        " ", stripped, flags=re.I,
    )
    stripped = re.sub(r"(?:を)?(?:探して|検索して|調べて|見せて)\s*", " ", stripped, flags=re.I)
    stripped = re.sub(
        r"(?:式样书|式樣書|试样书|試樣書|仕様書|规格书|規格書|"
        r"specifications?|specs?|pdf|版本|版|version|ver)",
        " ", stripped, flags=re.I,
    )
    stripped = re.sub(r"^[\s的の]+|[\sをについて有关相關相关]+$", " ", stripped)
    stripped = re.sub(r"[\s,，。.!！?？:：/\\|]+", " ", stripped).strip(" -_()（）[]【】")
    if stripped and len(stripped) <= 120 and re.search(r"[A-Za-z\u3040-\u30ff\u3400-\u9fff]", stripped):
        return (stripped,)

    # Fallback for punctuation-heavy Latin identifiers.
    words = re.findall(r"[A-Za-z][A-Za-z0-9_-]{2,}", stripped)
    useful = [w for w in words if w.casefold() not in {
        "bug", "jira", "issue", "what", "this", "find", "search", "lookup", "open",
    }]
    # Preserve an adjacent Latin product/function name as one SQL phrase.
    # Splitting "User Profile" into independent OR terms makes "User" match
    # unrelated documents and can hide the intended body among the first rows.
    if 2 <= len(useful) <= 6:
        return (" ".join(useful),)
    return tuple(dict.fromkeys(useful))[:3]


def render_spec_summary(result: "RetrievalResult") -> str:
    """Return a fast, bounded and position-backed digest for CPU-only hosts."""
    if not result.evidence:
        return "没有取得可用于分析的式样正文片段。"
    item = result.evidence[0]
    positioned = []
    for row in tuple((item.metadata or {}).get("excerpts") or ())[:32]:
        text = re.sub(r"\s+", " ", str(row.get("excerpt") or "")).strip()
        if text:
            positioned.append((row, text))
    identity = (
        f"{item.scope} · {item.file_name} · version={item.specification_version or '-'}"
        f" · document_id={item.document_id or '-'}"
    )
    if not positioned:
        return identity + "\n\n已定位文档，但当前正文表中没有可归纳的内容单元。"

    theme_rules = (
        ("式样质量/成熟度检查", r"レベルチェック|level\s*check|チェック項目|检查|檢查"),
        ("功能与要求完整性", r"機能要件|功能要件|要求性能|ユースケース|use\s*case|performance requirements"),
        ("变更点与跨式样一致性", r"変更点|変化点|変更|参照先仕様書|整合性|change|reference"),
        ("评价结果与达标门槛", r"評価結果|必達|基準点|目標|score|evaluation"),
        ("DRBFM/风险分析", r"DRBFM|問題の芽|新規問題|risk"),
        ("版本/修订履历", r"Rev(?:ision)?\s*history|変更履歴|改訂|version"),
        ("用户/Profile 行为", r"User\s*Profile|ユーザープロファイル|個人特定|profile"),
        ("Bluetooth/设备连接", r"Bluetooth|ブルートゥース|BT携帯|デバイス|device"),
    )
    themes = []
    for label, pattern in theme_rules:
        for row, text in positioned:
            if re.search(pattern, text, re.I):
                themes.append((label, row, text))
                break

    title = f"{item.specification_name or ''} {item.file_name or ''}".lower()
    if "チェック" in title or "check" in title:
        purpose = "这是一份检查/评价型式样资料，重点是用检查项、评价条件和达标门槛审查式样或设计成熟度，而不是单一功能的操作说明。"
    elif "profile" in title:
        purpose = "这是一份 User Profile 相关功能式样，正文用于定义用户识别、切换、保存/恢复及关联条件。"
    else:
        purpose = "该文档用于记录工程要求、条件或评价内容；以下只归纳当前已入库且带位置的片段。"
    first = positioned[0][0]
    lines = [identity, "", "分析摘要（由数据库正文位置确定性归纳，不经过模型引用格式校验）："]
    lines.append(
        f"- 文档用途：{purpose} "
        f"(section={first.get('section_id') or '-'}, content_id={first.get('content_id')}, page={first.get('page_number') or '-'})"
    )
    if themes:
        lines.append("- 主要内容领域：")
        for label, row, text in themes[:6]:
            snippet = text[:150] + ("…" if len(text) > 150 else "")
            lines.append(
                f"  - {label}：{snippet} "
                f"(section={row.get('section_id') or '-'}, content_id={row.get('content_id')}, page={row.get('page_number') or '-'})"
            )
    else:
        lines.append("- 主要内容领域：当前有限片段未命中预设工程主题，建议按章节继续查看。")
    lines.extend((
        "- 实际使用方式：先按检查项/功能域定位，再回到对应 content_id 和页码（Excel 时同时核对 Sheet/行列）确认完整条件、例外和评价结果。",
        "- 边界：这是有限入库片段的可追溯归纳，不代表整本式样的全部要求，也不据此判断某现象一定是 BUG 或式样通。",
    ))
    return "\n".join(lines)


def extra_spec_codes(question: str) -> tuple[str, ...]:
    return tuple(dict.fromkeys(m.group(1) for m in _SPEC_NEAR.finditer(str(question or ""))))[:6]


def render_sql_facts(result: "RetrievalResult", question: str) -> str:
    """Returns only SQL-backed facts, never synthesized claims or faux citations."""
    route = result.route
    requested_identity = "550" if re.search(r"(?<!\d)550(?!\d)", question) else "所询式样编号"
    if not result.evidence:
        if route == "SPEC_RELATIONS":
            return (
                "目前没有找到这本式样已整理好的引用关系。"
                "这不代表正文里一定没有引用；我还可以继续从正文中查找式样编号和章节引用。"
            )
        if route == "SPEC_CHAPTER_CONTENT" and not lookup_keywords(question):
            return "我还不能确定你指的是哪一本式样。请告诉我式样编号或名称；如果有多个版本，也可以顺便指定版本。"
        if result.warnings:
            return "这次查询没有完成，暂时无法确认结果。你可以稍后重试；这不表示文件不存在。"
        version = requested_version(question)
        qualifier = f"、版本 {version}" if version else ""
        return (f"目前没有找到匹配名称或编号{qualifier}的式样正文。"
                "封面或检查表不会被当作正文替代；如果你提供工程名或完整文件名，我可以继续缩小范围。")
    lines: list[str] = []
    if route == "SPEC_CATALOG":
        for item in result.evidence:
            lines.append(item.content)
        return "\n\n".join(lines)
    if route == "SPEC_LOOKUP":
        trusted = any(item.source_type == 'SPEC_LOOKUP' for item in result.evidence)
        if trusted:
            lines.append("找到了：")
        else:
            if any(item.source_type == 'SPEC_IDENTITY_CANDIDATE' for item in result.evidence):
                lines.append("找到了可能相关的正文，但编号或地域还不能完全对应：")
            else:
                lines.append("找到了已导入的文件，但还不能确认它就是你要找的式样正文：")
            if result.warnings:
                lines.append("注意：版本目录查询出现异常，不能据此判断数据库中不存在版本关联。")
        for item in result.evidence[:30]:
            md = item.metadata or {}
            if item.source_type == 'SPEC_SCOPE_CONFLICT':
                markers=', '.join(md.get('filename_project_markers') or ()) or '其他工程'
                lines.append(
                    f"- 工程归属冲突：数据库把 {item.file_name} 登记在 {item.scope}，"
                    f"但文件名明确标记为 {markers}。该文件已被排除，不能作为 {item.scope} 的当前正文。"
                )
                continue
            if item.source_type == 'SPEC_UNBOUND_FILE':
                lines.append(f"- 只确认文件已导入 PostgreSQL：{item.file_name} · document_id={item.document_id}。尚无可验证的 SPEC_BODY 版本关联，不能认定为当前可信式样版本。")
                continue
            if item.source_type == 'SPEC_IDENTITY_CANDIDATE':
                lines.append(
                    f"- 已登记 MAIN 本体文件：{item.file_name} · document_id={item.document_id} · "
                    f"version_id={md.get('linked_version_id') or '未知'} · 版本={item.specification_version or '未知'}。"
                    f"数据库关联 specification_id={md.get('linked_specification_id') or '未知'}、"
                    f"spec_code={md.get('linked_spec_code') or 'NULL'}、地域={md.get('linked_region') or 'UNSPECIFIED'}。"
                    f"与用户要求的式样编号是否同一身份尚未核准；不得因此断言该 PDF 已正式归属 {requested_identity}。"
                )
                continue
            lines.extend((
                f"- {item.scope} 的 {item.specification_code or '未编号'}《{item.specification_name or '名称未登记'}》",
                f"  - 当前正文：{item.file_name or '文件名未登记'}",
                f"  - 版本：{item.specification_version or '未知'}（已登记 {md.get('version_count', '未知')} 个正文版本）",
            ))
        return "\n".join(lines)
    if route == "SPEC_RELATIONS":
        lines.append("我在正文中找到的引用关系如下：")
        for item in result.evidence:
            if item.source_type == "REFERENCE_TARGET_CANDIDATE":
                md = item.metadata or {}
                lines.append(
                    "语义目标内容候选（不是正式引用关系，未经人工确认）：\n"
                    f"- 目标式样：{item.specification_code or '-'} {item.specification_name or ''}\n"
                    f"- 目标位置：section={item.section_id or '-'} / content_id={item.content_id or '-'} / "
                    f"page={item.page_number if item.page_number is not None else '-'}\n"
                    f"- 相似度（非概率）：{item.score}\n"
                    f"- 来源位置：{md.get('source_reference_locations') or '-'}\n"
                    f"- 目标片段：{item.content}"
                )
            else:
                md = item.metadata or {}
                requested_targets = tuple(md.get("requested_target_codes") or ())
                wants_source_text = bool(re.search(
                    r"(?:哪部分|哪段|哪些内容|哪些內容|什么内容|什麼內容|内容.{0,8}(?:有关|有關|关联|關聯|对应|對應)|"
                    r"那部分|该部分|該部分|内容给我|內容給我|原文|片段)",
                    question, re.I,
                ))
                if requested_targets and item.source_type == "DOCUMENT_REFERENCE":
                    sources = dict(md.get("reference_sources") or {})
                    for target in requested_targets:
                        source = dict(sources.get(target) or {})
                        samples = tuple(source.get("samples") or ())
                        if not source:
                            continue
                        lines.append(
                            f"- 关系类型：正文明确写出目标式样编号（文档级明确引用；不是语义候选）。\n"
                            f"- 来源：{item.specification_code}《{item.specification_name or item.file_name or '式样'}》\n"
                            f"- 目标：{target}\n"
                            f"- 来源位置："
                            f"section={','.join(source.get('sections') or ()) or '-'}；"
                            f"content_id={','.join(str(x) for x in (source.get('content_ids') or ())) or '-'}。"
                        )
                        if "target name not registered" in item.content:
                            lines.append("- 目标正文状态：当前工程尚未登记或未解析到该编号的目标正文，因此不能确定目标章节或目标content_id。")
                        if wants_source_text:
                            for sample in samples:
                                lines.append(f"来源原文：\n{sample}")
                else:
                    sources = dict(md.get('reference_sources') or {})
                    if item.source_type == 'DOCUMENT_REFERENCE' and sources:
                        lines.append(f"### {item.specification_code}《{item.specification_name or item.file_name}》引用清单")
                        table = ['| 引用式样 | 来源章节 | 内容ID |', '| --- | --- | --- |']
                        for target, source in sources.items():
                            sections = '、'.join(str(s).replace('.ROOT', '') for s in source.get('sections', ())) or '未记录'
                            content_ids = '、'.join(str(c) for c in source.get('content_ids', ())) or '未记录'
                            table.append(f"| {str(target).replace('|', '/')} | {sections} | {content_ids} |")
                        lines.append('\n'.join(table))
                        lines.append('以上为来源正文明确写出的文档级引用。目标式样的具体对应章节、适用版本及内容关系需要继续核对，不能仅凭编号认定。')
                    else:
                        lines.append(item.content)
        return "\n\n".join(lines)
    if route == "SPEC_OUTLINE":
        for item in result.evidence:
            if item.source_type == 'SPEC_UNBOUND_FILE':
                lines.append(f"{item.scope} 文件已导入：{item.file_name} · document_id={item.document_id}；但未核实对应式样版本，不能将其章节视为已认证的本体目录。")
                continue
            md = item.metadata
            if item.source_type == 'SPEC_OUTLINE_CANDIDATE':
                lines.append(
                    f"{item.scope} 已导入的正文 PDF 章节（编号归属未核准）：{item.file_name} · "
                    f"document_id={item.document_id} · linked_version_id={md.get('version_id') or '-'}。"
                    f"以下只证明此 PDF 中登记的章节，不代表已批准其归属 {requested_identity}。"
                )
            else:
                lines.append(f"这是 {item.scope} 的 {item.specification_code}《{item.specification_name or item.file_name}》目录（版本 {item.specification_version}）：")
            entries = md.get('outline', ())
            if not entries:
                lines.append("已找到本体文档，但没有可验证的章节目录记录。不能从 Cover Sheet 或模型记忆编造目录。")
            else:
                lines.append("目录章节：")
                visible = {}
                for entry in entries:
                    key = str(entry.get('section_id') or entry.get('chapter_id') or '')
                    if not key or key.endswith('.ROOT'):
                        continue
                    visible.setdefault(key, entry)
                def outline_order(pair):
                    return tuple(int(n) for n in re.findall(r'\d+', pair[0]))
                for key, entry in sorted(visible.items(), key=outline_order):
                    depth = key.count('.')
                    title = entry.get('title') or '标题未登记'
                    page = f"（页 {entry['page']}）" if entry.get('page') else ''
                    lines.append(f"{'  ' * depth}- **{key} {title}**{page}" if depth == 0 else f"{'  ' * depth}- {key} {title}{page}")
                if any(not entry.get('page') for entry in visible.values()):
                    lines.append("\n部分章节没有页码记录，可按章节号定位原文件。")
                if md.get('truncated'):
                    lines.append("目录显示数量已到安全上限，以上不是完整目录。")
        return "\n".join(lines)
    if route == "SPEC_CHAPTER_CONTENT":
        for item in result.evidence:
            if item.source_type == 'SPEC_UNBOUND_FILE':
                lines.append(f"仅确认已导入文件：{item.file_name}；未核实对应本体版本，不可据此归纳工程要求。")
                continue
            md = item.metadata or {}
            lines.append(f"{item.scope} · {item.specification_code or ''}《{item.specification_name or item.file_name}》· 版本 {item.specification_version or '未知'}")
            if not md.get('identity_verified'):
                lines.append(f"身份限制：正文 PDF 已登记，但 {requested_identity} 对应的编号/地域正式归属尚未核准。")
            lines.append("下面是对应章节的原文片段；需要作工程判断时，还应查看该条款的完整上下文和例外条件。")
            examples = md.get('excerpts', ())
            if not examples:
                lines.append("正文片段尚未入库或未查到，不能仅凭目录标题编造章节技术内容。")
                continue
            current_chapter = None
            for row in examples:
                chapter = str(row.get('chapter_key') or 'UNASSIGNED')
                if chapter != current_chapter:
                    current_chapter = chapter
                    lines.append(f"\n### 章节 {chapter} · 原文节选")
                text = str(row.get('excerpt') or '').strip()
                if not text:
                    continue
                lines.append(
                    f"- section={row.get('section_id') or '未知'} · PDF页={row.get('page_number') if row.get('page_number') is not None else '未知'}"
                    f" · content_id={row.get('content_id')}: {text}"
                    + (" [原文片段未完，请查看原始条款]" if row.get('excerpt_truncated') else '')
                )
            lines.append("以上是当前已定位到的内容；如果你要总结，我可以基于这些正文继续用自然语言归纳。")
        return "\n".join(lines)
    if route == "SPEC_VERSION_FACTS":
        for item in result.evidence:
            md = item.metadata
            versions = md.get('versions', ())
            if md.get('identity_verified') is False:
                lines.append(f"找到了{item.scope}的正文版本，但还不能确认它属于所询式样编号，因此暂不比较。")
            else:
                lines.append(f"## {item.specification_code}《{item.specification_name or '式样'}》" + ("版本清单" if md.get('inventory_only') else "版本变化"))
                lines.append("\n### 已登记正文版本")
            for version in versions:
                lines.append(f"- {version['version_label']} · {version['file_name']}")
            if md.get('inventory_only'):
                lines.append(f"本次列出 {len(versions)} 个已登记正文版本。" + ("最多展示50条。" if len(versions) == 50 else ""))
                continue
            if len(versions) < 2:
                lines.append("目前不足两个已登记的正文版本，无法比较；封面不会被用来补充正文版本。")
                continue
            if not md.get('adjacent'):
                lines.append("当前两个版本不是可验证的连续正文版本，因此不推断中间发生的变化。")
                continue
            if not md.get('change_set_ready'):
                lines.append("已找到连续正文版本，但尚未生成可核对的差异记录；这不代表两个版本没有变化。")
                continue
            # The query order is not a reliable chronology across imported
            # sources. Prefer the persisted change-set endpoints when present;
            # otherwise compare numeric version labels oldest -> newest.
            def version_key(label):
                nums=tuple(int(x) for x in re.findall(r"\d+",str(label or "")))
                return nums or (0,)
            ordered=sorted(versions,key=lambda v:version_key(v.get('version_label')))
            from_version=ordered[0]['version_label'];to_version=ordered[-1]['version_label']
            lines.append(f"\n### {from_version} → {to_version}")
            lines.append(f"对比方向：旧版 **{from_version}** → 新版 **{to_version}**。")
            lines.append(f"共记录 **{md.get('change_count', 0)}** 个变更单元，本次展示 **{len(md.get('changes', ()))}** 个；章节或表格单元内可能包含多处细节变化。")
            names = {'MODIFIED':'修改', 'ADDED':'新增', 'REMOVED':'删除', 'MOVED':'移动'}
            for index, ch in enumerate(md.get('changes', ()), 1):
                lines.append(f"\n#### {index}. {names.get(ch['change_type'], ch['change_type'])}｜{ch.get('section_title') or ch.get('section_id') or '正文内容'}")
                lines.append(f"位置：章节 `{ch.get('section_id') or '未记录'}`；旧页 {ch.get('old_page_number') or '未记录'} → 新页 {ch.get('new_page_number') or '未记录'}。")
                for label, field in (("旧原文", "old_text"), ("新原文", "new_text")):
                    value = str(ch.get(field) or '').strip()
                    if value:
                        if len(value) <= 2000:
                            lines.append(f"\n**{label}**\n\n{value}")
                if max(len(str(ch.get('old_text') or '')),len(str(ch.get('new_text') or '')))>2000:
                    from app.ai.change_preview import change_preview
                    lines.extend(change_preview(str(ch.get('old_text') or ''),str(ch.get('new_text') or '')))
            lines.append("\n依据：已保存的正文版本差异记录，与Tool版本对比使用同一差异表。")
        return "\n".join(lines)
    raise ValueError(f"Unexpected structured route: {route}")


def lookup_outline(retriever: "EnterpriseRetriever", question: str, scopes: tuple[str, ...]) -> tuple[list["EvidenceItem"], list[str]]:
    from app.ai.retrieval import EvidenceItem
    lookups, warnings = retriever._specification_lookup(question, scopes)
    rows: list[EvidenceItem] = []
    for item in lookups[:6]:
        if item.source_type == 'SPEC_UNBOUND_FILE':
            rows.append(EvidenceItem(
                evidence_id='', scope=item.scope, source_type='SPEC_UNBOUND_FILE',
                content='PDF file imported; verified specification version linkage is absent',
                document_id=item.document_id, file_name=item.file_name,
                metadata={'version_id': None, 'outline': (), 'linkage_verified': False},
            ))
            continue
        md = item.metadata or {}
        is_candidate = item.source_type == 'SPEC_IDENTITY_CANDIDATE'
        version_id = int((md.get('linked_version_id') if is_candidate else md.get('current_version_id')) or 0)
        try:
            with retriever._connect(item.scope) as conn, conn.cursor() as cur:
                cur.execute("""SELECT chapter_id, title_jp, title_en, sort_order
                    FROM public.chapters WHERE document_id=%s ORDER BY sort_order NULLS LAST, chapter_id LIMIT 100""", (item.document_id,))
                chapters = [dict(x) for x in cur.fetchall()]
                cur.execute("""SELECT chapter_id, section_id, title_jp, title_en, page_number, sort_order
                    FROM public.sections WHERE document_id=%s
                    ORDER BY sort_order NULLS LAST, section_id LIMIT 150""", (item.document_id,))
                sections = [dict(x) for x in cur.fetchall()]
            outline = [dict(chapter_id=x['chapter_id'], title=x.get('title_jp') or x.get('title_en'), page=None) for x in chapters]
            outline.extend(dict(chapter_id=x['chapter_id'], section_id=x['section_id'],
                                title=x.get('title_jp') or x.get('title_en'), page=x.get('page_number')) for x in sections)
            if not outline:
                # The parsed content index can be populated when the optional
                # chapter/section metadata tables are not; use IDs only.
                with retriever._connect(item.scope) as conn, conn.cursor() as cur:
                    cur.execute("""SELECT section_id, MIN(page_number) AS page
                        FROM public.contents WHERE document_id=%s GROUP BY section_id
                        ORDER BY section_id LIMIT 151""", (item.document_id,))
                    fallback = [dict(x) for x in cur.fetchall()]
                outline = [dict(section_id=x['section_id'], title=None, page=x['page']) for x in fallback[:150]]
            rows.append(EvidenceItem(evidence_id='', scope=item.scope,
                source_type=('SPEC_OUTLINE_CANDIDATE' if is_candidate else 'SPEC_OUTLINE'),
                content='SQL-verified physical body document section directory',
                document_id=item.document_id, file_name=item.file_name,
                specification_code=item.specification_code, specification_name=item.specification_name,
                specification_version=item.specification_version,
                metadata={'version_id': version_id, 'outline': tuple(outline[:150]), 'truncated': len(outline) > 150 or len(chapters) == 100 or len(sections) == 150}))
        except Exception as exc:
            warnings.append(f"{item.scope} outline: {type(exc).__name__}")
    return rows, warnings



def lookup_chapter_content(retriever: "EnterpriseRetriever", question: str,
                           scopes: tuple[str, ...], *, focus_terms: tuple[str, ...] = ()) -> tuple[list["EvidenceItem"], list[str]]:
    """Read bounded, verbatim physical-PDF content from PostgreSQL.

    The result is labelled extractive evidence, *not* an LLM-created abstract.
    A document's requested numeric spec identity remains unverified where the
    underlying MAIN version is registered under a distinct specification_id.
    """
    from app.ai.retrieval import EvidenceItem
    requested_section = requested_section_selector(question)
    outlines, warnings = lookup_outline(retriever, question, scopes)
    result: list[EvidenceItem] = []
    for item in outlines[:2]:
        if item.source_type == 'SPEC_UNBOUND_FILE':
            result.append(item)
            continue
        try:
            with retriever._connect(item.scope) as conn, conn.cursor() as cur:
                cur.execute("SET LOCAL statement_timeout = '8000ms'")
                section_filter = ""
                section_params: list[object] = []
                if requested_section:
                    section_filter = """
                      AND (
                        COALESCE(NULLIF(c.chapter_id,''), NULLIF(s.chapter_id,''),
                                 substring(c.section_id FROM '^[0-9]+'), 'UNASSIGNED')=%s
                        OR c.section_id=%s OR c.section_id LIKE %s
                      )
                    """
                    section_params = [requested_section, requested_section, requested_section + '.%']
                if focus_terms:
                    matches = " OR ".join("strpos(lower(c.content), lower(%s)) > 0" for _ in focus_terms)
                    section_filter += f" AND ({matches})"
                    section_params.extend(focus_terms)
                cur.execute(f"""
                    WITH ranked AS (
                        SELECT c.chapter_id, c.section_id, c.page_number,
                               c.id AS content_id, c.content,
                               COALESCE(NULLIF(s.title_jp,''),NULLIF(s.title_en,''),'') AS title,
                               COALESCE(NULLIF(c.chapter_id,''), NULLIF(s.chapter_id,''),
                                        substring(c.section_id FROM '^[0-9]+'), 'UNASSIGNED') AS chapter_key,
                               row_number() OVER (
                                   PARTITION BY COALESCE(NULLIF(c.chapter_id,''), NULLIF(s.chapter_id,''),
                                                         substring(c.section_id FROM '^[0-9]+'), 'UNASSIGNED')
                                   ORDER BY c.sort_order NULLS LAST, c.page_number NULLS LAST,
                                            c.chunk_index, c.id
                               ) AS chapter_rank
                        FROM public.contents c
                        LEFT JOIN public.sections s ON s.document_id=c.document_id
                            AND s.section_id=c.section_id
                        WHERE c.document_id=%s AND length(btrim(c.content)) > 0
                        {section_filter}
                    )
                    SELECT chapter_key, section_id, page_number, content_id,
                           content AS excerpt, title,
                           FALSE AS excerpt_truncated
                    FROM ranked WHERE chapter_rank <= %s
                    ORDER BY chapter_key, chapter_rank LIMIT %s
                """, (item.document_id, *section_params, 40, 32 if focus_terms else 300))
                excerpts = [dict(r) for r in cur.fetchall()]
            result.append(EvidenceItem(
                evidence_id='', scope=item.scope, source_type='SPEC_CHAPTER_CONTENT',
                content='Verified physical SPEC_BODY PDF chapter content excerpts',
                document_id=item.document_id, file_name=item.file_name,
                specification_code=item.specification_code,
                specification_name=item.specification_name,
                specification_version=item.specification_version,
                metadata={'version_id': item.metadata.get('version_id'),
                          'identity_verified': item.source_type == 'SPEC_OUTLINE',
                          'excerpts': tuple(excerpts),
                          'outline': tuple(item.metadata.get('outline', ())),
                          'requested_section': requested_section,
                          'outline_items': len(item.metadata.get('outline', ())),
                          'extractive_only': True},
            ))
        except Exception as exc:
            warnings.append(f'{item.scope} chapter content: {type(exc).__name__}')
    return result, warnings


_CN_NUMBER_VALUES = {
    "零": 0, "〇": 0, "一": 1, "二": 2, "三": 3, "四": 4, "五": 5,
    "六": 6, "七": 7, "八": 8, "九": 9,
}


def _chinese_number(value: str) -> str | None:
    value = str(value or "").strip()
    if value.isdigit():
        return str(int(value))
    if value == "十":
        return "10"
    if "十" in value:
        left, right = value.split("十", 1)
        tens = _CN_NUMBER_VALUES.get(left, 1) if left else 1
        ones = _CN_NUMBER_VALUES.get(right, 0) if right else 0
        return str(tens * 10 + ones)
    if value in _CN_NUMBER_VALUES:
        return str(_CN_NUMBER_VALUES[value])
    return None


def requested_section_selector(question: str) -> str | None:
    """Parse common chapter/section wording without depending on the LLM."""
    value = str(question or "")
    match = re.search(r"第\s*([一二三四五六七八九十零〇\d]+)\s*(?:章|章节|章節)", value, re.I)
    if match:
        return _chinese_number(match.group(1))
    match = re.search(r"(?:chapter|chap(?:ter)?)\s*([0-9]+(?:\.[0-9]+)*)", value, re.I)
    if match:
        return match.group(1)
    match = re.search(r"(?<![\d.])([0-9]+(?:\.[0-9]+)*)\s*(?:章|章节|章節|节|節)", value, re.I)
    return match.group(1) if match else None


def lookup_adjacent_versions(retriever: "EnterpriseRetriever", question: str, scopes: tuple[str, ...]) -> tuple[list["EvidenceItem"], list[str]]:
    from app.ai.retrieval import EvidenceItem
    from app.storage.primary_document_policy import PrimarySpecificationDocumentPolicy
    # Identity lookup must not pick a cover sheet to satisfy the version count.
    lookups, warnings = retriever._specification_lookup(question, scopes)
    result: list[EvidenceItem] = []
    inventory_only = not bool(re.search(r"改了|修改|变动|變動|改动|改動|区别|區別|变更|變更|变化|變化|差异|差異|对比|對比|比较|比較|diff|change", question, re.I))
    requested_versions = tuple(dict.fromkeys(re.findall(r"\bv(?:er\.?)?\s*(\d+(?:\.\d+)+)", question, re.I)))
    primary = PrimarySpecificationDocumentPolicy.sql_primary_predicate('d')
    for item in lookups[:4]:
        sid = int((item.metadata or {}).get('specification_id') or 0)
        if not sid:
            if item.source_type in {'SPEC_UNBOUND_FILE', 'SPEC_IDENTITY_CANDIDATE'}:
                md = item.metadata or {}
                body_version = ({'version_label': item.specification_version or md.get('file_version') or 'unknown',
                                 'id': md.get('linked_version_id'),
                                 'document_id': item.document_id, 'file_name': item.file_name}
                                if item.source_type == 'SPEC_IDENTITY_CANDIDATE' else None)
                result.append(EvidenceItem(
                    evidence_id='', scope=item.scope, source_type='SPEC_VERSION_FACTS',
                    content='Imported/linked physical body, numeric identity not verified',
                    file_name=item.file_name, document_id=item.document_id,
                    metadata={'versions': (body_version,) if body_version else (),
                              'adjacent': False, 'change_set_ready': False,
                              'identity_verified': False},
                ))
            continue
        try:
            with retriever._connect(item.scope) as conn, conn.cursor() as cur:
                cur.execute(f"""SELECT sv.id, sv.document_id, sv.version_label, d.file_name
                    FROM public.specification_versions sv JOIN public.documents d ON d.document_id=sv.document_id
                    WHERE sv.specification_id=%s AND sv.branch_name='MAIN' AND {primary}
                    ORDER BY sv.version_sort_key DESC NULLS LAST, sv.version_seq DESC, sv.id DESC LIMIT 50""", (sid,))
                versions = [dict(x) for x in cur.fetchall()]
                if not inventory_only and len(requested_versions) >= 2:
                    matched = [v for v in versions if re.sub(r"^(?:ver\.?|v)\s*", "", str(v['version_label']), flags=re.I) in requested_versions]
                    if len(matched) != len(requested_versions):
                        missing = [v for v in requested_versions if not any(re.sub(r"^(?:ver\.?|v)\s*", "", str(row['version_label']), flags=re.I) == v for row in matched)]
                        warnings.append("所选式样未登记请求版本：" + ", ".join(missing))
                        continue
                    versions = matched
                selected = versions if inventory_only else (list(reversed(versions[:2])) if len(versions) >= 2 else versions)
                adjacent = not inventory_only and len(selected) == 2
                change_set = None
                changes = []
                total = 0
                if adjacent:
                    cur.execute("""SELECT id FROM public.specification_change_sets
                        WHERE specification_id=%s AND from_version_id=%s AND to_version_id=%s
                        AND branch_name='MAIN' AND is_adjacent IS TRUE AND status='READY'
                        ORDER BY id DESC LIMIT 1""", (sid, selected[0]['id'], selected[1]['id']))
                    change_set = cur.fetchone()
                    if change_set:
                        cur.execute("""SELECT COUNT(*) AS total FROM public.specification_changes
                            WHERE change_set_id=%s""", (change_set['id'],))
                        total = int(cur.fetchone()['total'])
                        cur.execute("""SELECT change_type, section_id, section_title,
                            old_page_number, new_page_number, old_text, new_text
                            FROM public.specification_changes WHERE change_set_id=%s ORDER BY id LIMIT 15""", (change_set['id'],))
                        changes = [dict(x) for x in cur.fetchall()]
            result.append(EvidenceItem(evidence_id='', scope=item.scope, source_type='SPEC_VERSION_FACTS',
                content='PostgreSQL SPEC_BODY version/diff inventory', specification_code=item.specification_code,
                specification_name=item.specification_name,
                metadata={'versions': tuple(selected), 'adjacent': adjacent, 'inventory_only': inventory_only,
                          'change_set_ready': bool(change_set), 'change_set_id': (change_set or {}).get('id'),
                          'changes': tuple(changes), 'change_count': total}))
        except Exception as exc:
            warnings.append(f"{item.scope} adjacent versions: {type(exc).__name__}")
    return result, warnings
