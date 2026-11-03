from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from app.pipeline.pipeline_factory import PipelineFactory
from app.router.format_router import (
    FormatRoute,
    FormatRouter,
)


class PipelineRouterError(RuntimeError):
    """Pipeline 路由基础异常。"""


class PipelineExecutionError(
    PipelineRouterError
):
    """Pipeline 执行异常。"""


@dataclass(frozen=True)
class PipelineRouteResult:
    """
    Pipeline 路由执行结果。

    attributes:
        input_path:
            输入文件路径。

        output_path:
            规划的 JSON 输出路径。

            如果当前 Pipeline 设置：

                save_json=False

            此路径不会实际创建或写入。

        format_route:
            文件格式路由信息。

        pipeline_name:
            实际执行的 Pipeline 类名。

        document:
            Pipeline 处理后的 Document。
    """

    input_path: Path

    output_path: Path

    format_route: FormatRoute

    pipeline_name: str

    document: Any


@dataclass(frozen=True)
class PipelineFailureResult:
    """
    Pipeline 批处理失败结果。

    attributes:
        input_path:
            失败文件路径。

        error:
            捕获到的异常对象。
    """

    input_path: Path

    error: Exception


class PipelineRouter:
    """
    文档 Pipeline 路由与执行器。

    职责：
        1. 验证输入文件或目录
        2. 使用 FormatRouter 识别文件格式
        3. 使用 PipelineFactory 创建对应 Pipeline
        4. 生成 JSON 输出路径
        5. 执行单文件 Pipeline
        6. 批量处理目录中的受支持文件
        7. 汇总成功和失败结果

    不负责：
        - 文件内容解析
        - 格式专用清洗
        - Chapter / Section 建模
        - JSON 构建
        - PostgreSQL 保存
    """

    def __init__(
        self,
        *,
        output_directory: str | Path = "./output",
        overwrite_output: bool = True,
        pipeline_kwargs: dict[str, Any] | None = None,
    ) -> None:

        if output_directory is None:
            raise ValueError(
                "output_directory cannot be None."
            )

        if not str(
            output_directory
        ).strip():
            raise ValueError(
                "output_directory cannot be empty."
            )

        self.output_directory = Path(
            output_directory
        ).expanduser()

        self.overwrite_output = bool(
            overwrite_output
        )

        self.pipeline_kwargs = dict(
            pipeline_kwargs
            or {}
        )

    # ==================================================
    # Route
    # ==================================================

    def route(
        self,
        file_path: str | Path,
        *,
        output_path: str | Path | None = None,
        pipeline_kwargs: dict[str, Any] | None = None,
    ) -> tuple[
        Path,
        Path,
        FormatRoute,
        Any,
    ]:
        """
        根据文件格式创建对应 Pipeline，但不执行。

        Returns:
            (
                input_path,
                output_path,
                format_route,
                pipeline,
            )
        """

        input_path = (
            self._validate_input_path(
                file_path
            )
        )

        format_route = FormatRouter.route(
            input_path
        )

        resolved_output_path = (
            self._resolve_output_path(
                input_path=input_path,
                output_path=output_path,
            )
        )

        merged_pipeline_kwargs = {
            **self.pipeline_kwargs,
            **(
                pipeline_kwargs
                or {}
            ),
        }

        pipeline = (
            PipelineFactory.create_by_route(
                format_route=format_route,
                file_path=input_path,
                **merged_pipeline_kwargs,
            )
        )

        return (
            input_path,
            resolved_output_path,
            format_route,
            pipeline,
        )

    # ==================================================
    # Execute Single File
    # ==================================================

    def execute(
        self,
        file_path: str | Path,
        *,
        output_path: str | Path | None = None,
        pipeline_kwargs: dict[str, Any] | None = None,
    ) -> PipelineRouteResult:
        """
        路由并执行单个文件。

        Args:
            file_path:
                输入文件路径。

            output_path:
                可选 JSON 输出路径。

                未指定时：

                    output/<原文件 stem>.json

            pipeline_kwargs:
                本次执行传递给 Pipeline
                构造函数的参数。

        Returns:
            PipelineRouteResult
        """

        (
            input_path,
            resolved_output_path,
            format_route,
            pipeline,
        ) = self.route(
            file_path,
            output_path=output_path,
            pipeline_kwargs=pipeline_kwargs,
        )

        # 只有 Pipeline 真正启用 JSON 时，
        # 才创建目录、检查覆盖。
        #
        # Database-only:
        #
        #     save_json=False
        #
        # 不应该触发任何 JSON Path 副作用。
        if self._is_json_output_enabled(
            pipeline
        ):
            self._prepare_output_path(
                resolved_output_path
            )

        pipeline_name = (
            pipeline.__class__.__name__
        )

        try:
            document = pipeline.run(
                file_path=input_path,
                output=resolved_output_path,
            )

        except Exception as exc:
            raise PipelineExecutionError(
                f"Pipeline '{pipeline_name}' "
                f"failed for "
                f"'{input_path.name}': {exc}"
            ) from exc

        return PipelineRouteResult(
            input_path=input_path,
            output_path=resolved_output_path,
            format_route=format_route,
            pipeline_name=pipeline_name,
            document=document,
        )

    # ==================================================
    # Execute Directory
    # ==================================================

    def execute_directory(
        self,
        input_directory: str | Path,
        *,
        recursive: bool = False,
        continue_on_error: bool = True,
        pipeline_kwargs: dict[str, Any] | None = None,
    ) -> tuple[
        list[PipelineRouteResult],
        list[PipelineFailureResult],
    ]:
        """
        批量处理目录中的受支持文件。

        Args:
            input_directory:
                输入目录。

            recursive:
                是否递归扫描子目录。

            continue_on_error:
                单个文件失败后，
                是否继续处理其他文件。

            pipeline_kwargs:
                传递给每个 Pipeline
                构造函数的参数。

        Returns:
            (
                success_results,
                failed_results,
            )

        批处理输出路径：

            input/report.pdf
                ->
            output/report.pdf.json

        递归时：

            input/a/report.pdf
                ->
            output/a/report.pdf.json

            input/b/report.pdf
                ->
            output/b/report.pdf.json

        从而避免同名文件覆盖。
        """

        directory = (
            self._validate_input_directory(
                input_directory
            )
        )

        files = self._collect_supported_files(
            directory=directory,
            recursive=recursive,
        )

        success_results: list[
            PipelineRouteResult
        ] = []

        failed_results: list[
            PipelineFailureResult
        ] = []

        for file_path in files:

            try:
                output_path = (
                    self._resolve_directory_output_path(
                        input_directory=directory,
                        input_path=file_path,
                    )
                )

                result = self.execute(
                    file_path,
                    output_path=output_path,
                    pipeline_kwargs=pipeline_kwargs,
                )

                success_results.append(
                    result
                )

            except Exception as exc:
                failed_results.append(
                    PipelineFailureResult(
                        input_path=file_path,
                        error=exc,
                    )
                )

                if not continue_on_error:
                    raise

        return (
            success_results,
            failed_results,
        )

    # ==================================================
    # File Collection
    # ==================================================

    @staticmethod
    def _collect_supported_files(
        *,
        directory: Path,
        recursive: bool,
    ) -> list[Path]:
        """
        收集目录中的正式支持文件。

        当前最终可执行格式由：

            PipelineFactory

        决定。
        """

        candidates = (
            directory.rglob("*")
            if recursive
            else directory.iterdir()
        )

        files = [
            path
            for path in candidates
            if (
                path.is_file()
                and not path.name.startswith(
                    "~$"
                )
                and PipelineFactory.supports(
                    path
                )
            )
        ]

        return sorted(
            files,
            key=lambda path: (
                path.suffix.lower(),
                str(path).lower(),
            ),
        )

    # ==================================================
    # Output Path
    # ==================================================

    def _resolve_output_path(
        self,
        *,
        input_path: Path,
        output_path: str | Path | None,
    ) -> Path:
        """
        解析单文件 JSON 输出路径。

        单文件调用保持原来的：

            spec.pdf
                ->
            output/spec.json

        行为。
        """

        if output_path is not None:

            if not str(
                output_path
            ).strip():
                raise ValueError(
                    "output_path cannot be empty."
                )

            resolved = Path(
                output_path
            ).expanduser()

        else:
            resolved = (
                self.output_directory
                / f"{input_path.stem}.json"
            )

        if resolved.suffix.lower() != ".json":
            resolved = (
                resolved.with_suffix(
                    ".json"
                )
            )

        return resolved

    def _resolve_directory_output_path(
        self,
        *,
        input_directory: Path,
        input_path: Path,
    ) -> Path:
        """
        为目录批处理生成稳定、不冲突的输出路径。

        Example:

            input/report.pdf
                ->
            output/report.pdf.json

            input/report.docx
                ->
            output/report.docx.json

        recursive=True：

            input/a/report.pdf
                ->
            output/a/report.pdf.json

            input/b/report.pdf
                ->
            output/b/report.pdf.json
        """

        try:
            relative_path = (
                input_path.relative_to(
                    input_directory
                )
            )

        except ValueError:
            # 防御性 fallback。
            relative_path = Path(
                input_path.name
            )

        return (
            self.output_directory
            / relative_path.parent
            / f"{relative_path.name}.json"
        )

    def _prepare_output_path(
        self,
        output_path: Path,
    ) -> None:
        """
        准备 JSON 输出目录。

        仅在 save_json=True 时调用。
        """

        output_path.parent.mkdir(
            parents=True,
            exist_ok=True,
        )

        if (
            output_path.exists()
            and not self.overwrite_output
        ):
            raise FileExistsError(
                f"Output file already exists: "
                f"{output_path}"
            )

    @staticmethod
    def _is_json_output_enabled(
        pipeline: Any,
    ) -> bool:
        """
        判断当前 Pipeline 是否启用 JSON。

        正式 Pipeline 均暴露：

            save_json_enabled

        对未实现该字段的自定义 Pipeline，
        为保持兼容默认按 True 处理。
        """

        return bool(
            getattr(
                pipeline,
                "save_json_enabled",
                True,
            )
        )

    # ==================================================
    # Validation
    # ==================================================

    @staticmethod
    def _validate_input_path(
        file_path: str | Path,
    ) -> Path:

        if file_path is None:
            raise ValueError(
                "file_path cannot be None."
            )

        if not str(
            file_path
        ).strip():
            raise ValueError(
                "file_path cannot be empty."
            )

        path = Path(
            file_path
        ).expanduser()

        if not path.exists():
            raise FileNotFoundError(
                f"Input file not found: "
                f"{path}"
            )

        if not path.is_file():
            raise IsADirectoryError(
                f"Input path is not a file: "
                f"{path}"
            )

        if path.name.startswith(
            "~$"
        ):
            raise ValueError(
                "Temporary Office file "
                "is not supported: "
                f"{path.name}"
            )

        if not FormatRouter.supports(
            path
        ):
            supported = ", ".join(
                FormatRouter
                .supported_extensions()
            )

            raise ValueError(
                f"Unsupported file type: "
                f"{path.suffix or '<no extension>'}. "
                f"Supported formats: "
                f"{supported}"
            )

        if not PipelineFactory.supports(
            path
        ):
            supported_pipelines = ", ".join(
                PipelineFactory
                .supported_extensions()
            )

            raise ValueError(
                "No executable Pipeline is "
                "registered for file type: "
                f"{path.suffix or '<no extension>'}. "
                f"Executable formats: "
                f"{supported_pipelines}"
            )

        return path

    @staticmethod
    def _validate_input_directory(
        input_directory: str | Path,
    ) -> Path:

        if input_directory is None:
            raise ValueError(
                "input_directory cannot be None."
            )

        if not str(
            input_directory
        ).strip():
            raise ValueError(
                "input_directory cannot be empty."
            )

        directory = Path(
            input_directory
        ).expanduser()

        if not directory.exists():
            raise FileNotFoundError(
                f"Input directory not found: "
                f"{directory}"
            )

        if not directory.is_dir():
            raise NotADirectoryError(
                f"Input path is not a directory: "
                f"{directory}"
            )

        return directory