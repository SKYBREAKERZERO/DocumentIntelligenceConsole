from __future__ import annotations

from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any

from app.builder.json_builder import JsonBuilder
from app.converter.ppt_converter import PPTConverter
from app.pipeline.pptx_pipeline import PPTXPipeline
from app.storage.postgres_storage import PostgresStorage


class PPTPipeline:
    """
    Legacy .ppt ingestion pipeline.

    Flow:
        .ppt
        -> Microsoft PowerPoint COM conversion
        -> temporary .pptx
        -> existing PPTXPipeline processing
        -> restore original .ppt file identity
        -> JSON / PostgreSQL

    Notes:
        - Does NOT route .ppt directly into PPTXLoader.
        - Microsoft PowerPoint must be installed.
    """

    def __init__(
        self,
        *,
        chunk_max_length: int = 1000,
        save_json: bool = True,
        save_database: bool = True,
        project_code: str | None = None,
        **pptx_pipeline_options: Any,
    ) -> None:

        if chunk_max_length <= 0:
            raise ValueError(
                "chunk_max_length must be greater than 0."
            )

        self.save_json_enabled = bool(
            save_json
        )

        self.save_database_enabled = bool(
            save_database
        )

        self.project_code = project_code

        self.converter = PPTConverter()

        # Reuse the already-stabilized PPTX processing chain,
        # but prevent the inner pipeline from writing the temporary
        # converted file to JSON/PostgreSQL.
        self.pptx_pipeline = PPTXPipeline(
            chunk_max_length=(
                chunk_max_length
            ),
            save_json=False,
            save_database=False,
            project_code=project_code,
            **pptx_pipeline_options,
        )

        self.builder = (
            JsonBuilder()
            if self.save_json_enabled
            else None
        )

        self.storage = (
            PostgresStorage(project_code=self.project_code)
            if self.save_database_enabled
            else None
        )

    def run(
        self,
        file_path: str | Path,
        output: str | Path,
    ):

        input_path = (
            self._validate_input_path(
                file_path
            )
        )

        output_path = Path(
            output
        ).expanduser()

        with TemporaryDirectory(
            prefix="document_ingestion_ppt_"
        ) as temporary_directory:

            converted_path = (
                Path(
                    temporary_directory
                )
                / (
                    input_path.stem
                    + ".pptx"
                )
            )

            self.converter.convert(
                input_path,
                converted_path,
            )

            # output is unused because inner save_json=False,
            # but run() requires a path.
            inner_output = (
                Path(
                    temporary_directory
                )
                / "unused.json"
            )

            document = (
                self.pptx_pipeline.run(
                    file_path=converted_path,
                    output=inner_output,
                )
            )

        # Restore the actual source identity before persistence.
        document.file_name = (
            input_path.name
        )

        document.file_type = "ppt"

        document.metadata.update(
            {
                "source_format": "ppt",
                "original_source_file": (
                    input_path.name
                ),
                "legacy_ppt_converted": True,
                "conversion_method": (
                    "Microsoft PowerPoint COM"
                ),
                "pipeline": "PPTPipeline",
                "pipeline_status": "SUCCESS",
            }
        )

        if self.save_json_enabled:

            assert self.builder is not None

            output_path.parent.mkdir(
                parents=True,
                exist_ok=True,
            )

            json_data = self.builder.build(
                document
            )

            self.builder.save(
                json_data,
                str(
                    output_path
                ),
            )

        if self.save_database_enabled:

            assert self.storage is not None

            self.storage.save(
                document
            )

        return document

    @staticmethod
    def _validate_input_path(
        file_path: str | Path,
    ) -> Path:

        path = Path(
            file_path
        ).expanduser()

        if not path.exists():
            raise FileNotFoundError(
                f"PPT file not found: {path}"
            )

        if not path.is_file():
            raise IsADirectoryError(
                f"Input path is not a file: {path}"
            )

        if path.name.startswith(
            "~$"
        ):
            raise ValueError(
                "Temporary PowerPoint file is not supported: "
                f"{path.name}"
            )

        if path.suffix.lower() != ".ppt":
            raise ValueError(
                "PPTPipeline only accepts .ppt files. "
                f"Received: "
                f"{path.suffix or '<no extension>'}"
            )

        return path
