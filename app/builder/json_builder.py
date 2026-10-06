from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from app.model.document import Document
from app.normalizer.postgres_safe_sanitizer import PostgresSafeSanitizer


class JsonBuilder:
    """
    Document JSON 构建器。

    输出内容：

        document
        metadata
        chapters
        sections
        contents
        vector_records

    vector_records 用于：

        pgvector
        Qdrant
        Milvus
        Pinecone
        Weaviate
        Azure AI Search

    注意：
        本模块只构建向量记录，不生成 embedding。
    """

    def __init__(
        self,
        *,
        include_vector_records: bool = True,
        include_empty_contents: bool = False,
    ) -> None:

        self.include_vector_records = (
            include_vector_records
        )

        self.include_empty_contents = (
            include_empty_contents
        )

    def build(
        self,
        document: Document,
    ) -> dict[str, Any]:

        self._validate_document(
            document
        )

        # Keep exported JSON compatible with PostgreSQL jsonb as well. This
        # removes PDF metadata NUL bytes before they can become \u0000 in JSON.
        PostgresSafeSanitizer.sanitize_document(document)

        document_id = self._resolve_document_id(
            document
        )

        chapter_map = {
            str(chapter.id): chapter
            for chapter in document.chapters
        }

        section_map = {
            str(section.id): section
            for section in document.sections
        }

        result: dict[str, Any] = {
            "document": {
                "document_id": document_id,
                "file_name": document.file_name,
                "file_type": document.file_type,
                "project_code": document.metadata.get("project_code"),
                "project_name": document.metadata.get("project_name"),
                "project_assignment_source": document.metadata.get(
                    "project_assignment_source"
                ),
                "series": document.metadata.get("series"),
                "region_scope": document.metadata.get("region_scope"),
                "spec_type": document.metadata.get("spec_type"),
                "spec_subtype": document.metadata.get("spec_subtype"),
                "specification_code": document.metadata.get("specification_code"),
                "specification_key": document.metadata.get("specification_key"),
                "specification_name": document.metadata.get("specification_name"),
                "specification_version": document.metadata.get("specification_version"),
                "specification_version_status": document.metadata.get(
                    "specification_version_status"
                ),
                "created_at": datetime.now(
                    timezone.utc
                ).isoformat(),
            },
            "metadata": document.metadata,
            "chapters": [
                chapter.model_dump(
                    mode="json"
                )
                for chapter in document.chapters
            ],
            "sections": [
                section.model_dump(
                    mode="json"
                )
                for section in document.sections
            ],
            "contents": [
                content.model_dump(
                    mode="json"
                )
                for content in document.contents
                if (
                    self.include_empty_contents
                    or str(
                        content.text
                        or ""
                    ).strip()
                )
            ],
        }

        if self.include_vector_records:
            result[
                "vector_records"
            ] = self._build_vector_records(
                document=document,
                document_id=document_id,
                chapter_map=chapter_map,
                section_map=section_map,
            )

        return result

    def save(
        self,
        data: dict[str, Any],
        output: str | Path,
    ) -> None:

        output_path = Path(
            output
        ).expanduser()

        output_path.parent.mkdir(
            parents=True,
            exist_ok=True,
        )

        temporary_path = (
            output_path.with_suffix(
                output_path.suffix + ".tmp"
            )
        )

        try:
            with temporary_path.open(
                "w",
                encoding="utf-8",
                newline="\n",
            ) as file:
                json.dump(
                    data,
                    file,
                    ensure_ascii=False,
                    indent=2,
                )

                file.write(
                    "\n"
                )

            temporary_path.replace(
                output_path
            )

        except Exception:
            if temporary_path.exists():
                temporary_path.unlink()

            raise

    def _build_vector_records(
        self,
        *,
        document: Document,
        document_id: str,
        chapter_map: dict[str, Any],
        section_map: dict[str, Any],
    ) -> list[dict[str, Any]]:

        records: list[
            dict[str, Any]
        ] = []

        used_ids: set[str] = set()

        for content in document.contents:

            text = str(
                content.text
                or ""
            ).strip()

            if not text:
                continue

            chapter_id = self._normalize_optional_id(
                getattr(
                    content,
                    "chapter_id",
                    None,
                )
            )

            section_id = self._normalize_optional_id(
                getattr(
                    content,
                    "section_id",
                    None,
                )
            )

            chunk_index = int(
                getattr(
                    content,
                    "chunk_index",
                    0,
                )
                or 0
            )

            chapter = (
                chapter_map.get(
                    chapter_id
                )
                if chapter_id
                else None
            )

            section = (
                section_map.get(
                    section_id
                )
                if section_id
                else None
            )

            chapter_title = (
                getattr(
                    chapter,
                    "title_jp",
                    None,
                )
                if chapter
                else None
            )

            section_title = (
                getattr(
                    section,
                    "title_jp",
                    None,
                )
                if section
                else None
            )

            page_number = getattr(
                content,
                "page_number",
                None,
            )

            token_count = int(
                getattr(
                    content,
                    "token_count",
                    0,
                )
                or 0
            )

            record_id = self._build_record_id(
                document_id=document_id,
                chapter_id=chapter_id,
                section_id=section_id,
                chunk_index=chunk_index,
                text=text,
            )

            if record_id in used_ids:
                record_id = self._build_duplicate_safe_id(
                    base_id=record_id,
                    used_ids=used_ids,
                )

            used_ids.add(
                record_id
            )

            records.append(
                {
                    "id": record_id,
                    "text": text,
                    "metadata": {
                        "document_id": document_id,
                        "file_name": document.file_name,
                        "document_type": document.file_type,
                        "project_code": document.metadata.get("project_code"),
                        "project_name": document.metadata.get("project_name"),
                        "series": document.metadata.get("series"),
                        "region_scope": document.metadata.get("region_scope"),
                        "spec_type": document.metadata.get("spec_type"),
                        "spec_subtype": document.metadata.get("spec_subtype"),
                        "specification_code": document.metadata.get("specification_code"),
                        "specification_key": document.metadata.get("specification_key"),
                        "specification_name": document.metadata.get("specification_name"),
                        "specification_version": document.metadata.get(
                            "specification_version"
                        ),
                        "chapter_id": chapter_id,
                        "chapter_title": chapter_title,
                        "section_id": section_id,
                        "section_title": section_title,
                        "page_number": page_number,
                        "chunk_index": chunk_index,
                        "token_count": token_count,
                    },
                }
            )

        return records

    @staticmethod
    def _build_record_id(
        *,
        document_id: str,
        chapter_id: str | None,
        section_id: str | None,
        chunk_index: int,
        text: str,
    ) -> str:

        identity = "|".join(
            [
                document_id,
                chapter_id or "",
                section_id or "",
                str(chunk_index),
                text,
            ]
        )

        digest = hashlib.sha256(
            identity.encode(
                "utf-8"
            )
        ).hexdigest()[:24]

        return (
            f"chunk-{digest}"
        )

    @staticmethod
    def _build_duplicate_safe_id(
        *,
        base_id: str,
        used_ids: set[str],
    ) -> str:

        suffix = 1

        while True:
            candidate = (
                f"{base_id}-{suffix}"
            )

            if candidate not in used_ids:
                return candidate

            suffix += 1

    @staticmethod
    def _resolve_document_id(
        document: Document,
    ) -> str:

        metadata_document_id = (
            document.metadata.get(
                "document_id"
            )
            if document.metadata
            else None
        )

        document_id = str(
            metadata_document_id
            or document.file_name
        ).strip()

        if not document_id:
            raise ValueError(
                "document_id cannot be empty."
            )

        return document_id

    @staticmethod
    def _normalize_optional_id(
        value: Any,
    ) -> str | None:

        if value is None:
            return None

        normalized = str(
            value
        ).strip()

        return normalized or None

    @staticmethod
    def _validate_document(
        document: Document,
    ) -> None:

        if document is None:
            raise ValueError(
                "Document cannot be None."
            )

        if not isinstance(
            document,
            Document,
        ):
            raise TypeError(
                "JsonBuilder expects an "
                "app.model.document.Document instance."
            )

        if not document.file_name.strip():
            raise ValueError(
                "Document file_name cannot be empty."
            )

        if not document.file_type.strip():
            raise ValueError(
                "Document file_type cannot be empty."
            )