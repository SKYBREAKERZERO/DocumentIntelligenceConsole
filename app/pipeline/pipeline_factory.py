from __future__ import annotations

from dataclasses import dataclass
from importlib import import_module
from pathlib import Path
from threading import RLock
from typing import Any

from app.router.format_router import (
    FormatRoute,
    FormatRouter,
    UnsupportedFormatError,
)


class PipelineFactoryError(RuntimeError):
    """Pipeline 工厂基础异常。"""


class UnsupportedFileTypeError(PipelineFactoryError):
    """输入文件格式不受支持。"""


class PipelineLoadError(PipelineFactoryError):
    """Pipeline 模块或类加载失败。"""


class PipelineRegistrationError(PipelineFactoryError):
    """Pipeline 注册配置异常。"""


@dataclass(frozen=True)
class PipelineSpec:
    """
    Pipeline 延迟加载配置。

    attributes:
        module_path:
            Pipeline 所在的 Python 模块路径。

        class_name:
            Pipeline 类名。

        extensions:
            该 Pipeline 实际支持的文件扩展名白名单。

            此字段用于防止多个扩展名共享同一个
            pipeline_key 时发生错误路由。

            示例：
                .pptx 可以交给 PPTXPipeline
                .ppt 不可以交给 PPTXPipeline

                .xlsx 可以交给 XLSXPipeline
                .xls 不可以交给 XLSXPipeline
    """

    module_path: str
    class_name: str
    extensions: frozenset[str]

    def supports_extension(
        self,
        extension: str,
    ) -> bool:
        """判断当前 Pipeline 是否支持指定扩展名。"""

        normalized_extension = (
            PipelineFactory.normalize_extension(
                extension
            )
        )

        return (
            normalized_extension
            in self.extensions
        )


class PipelineFactory:
    """
    Pipeline 实例工厂。

    职责：
        1. 通过 FormatRouter 获取 FormatRoute
        2. 根据 pipeline_key 查找 PipelineSpec
        3. 验证实际扩展名是否属于 Pipeline 白名单
        4. 延迟导入 Pipeline 类
        5. 实例化对应 Pipeline

    不负责：
        - 文档内容解析
        - Pipeline 执行
        - 输出路径管理
        - 批量任务调度
        - 文件格式转换

    当前正式支持：
        - PDF
        - DOCX
        - PPTX
        - PPT
        - XLSX
        - TXT
        - PNG
        - JPG
        - JPEG

    明确不支持：
        - XLS
        - HTML
        - XML
        - CSV
        - Markdown
    """

    _lock = RLock()

    _registry: dict[str, PipelineSpec] = {
        "pdf": PipelineSpec(
            module_path="app.pipeline.pdf_pipeline",
            class_name="PDFPipeline",
            extensions=frozenset(
                {
                    ".pdf",
                }
            ),
        ),
        "docx": PipelineSpec(
            module_path="app.pipeline.docx_pipeline",
            class_name="DOCXPipeline",
            extensions=frozenset(
                {
                    ".docx",
                }
            ),
        ),
        "xlsx": PipelineSpec(
            module_path="app.pipeline.xlsx_pipeline",
            class_name="XLSXPipeline",
            extensions=frozenset(
                {
                    ".xlsx",
                }
            ),
        ),
        "pptx": PipelineSpec(
            module_path="app.pipeline.pptx_pipeline",
            class_name="PPTXPipeline",
            extensions=frozenset(
                {
                    ".pptx",
                }
            ),
        ),
        "ppt": PipelineSpec(
            module_path="app.pipeline.ppt_pipeline",
            class_name="PPTPipeline",
            extensions=frozenset(
                {
                    ".ppt",
                }
            ),
        ),
        "txt": PipelineSpec(
            module_path="app.pipeline.txt_pipeline",
            class_name="TXTPipeline",
            extensions=frozenset(
                {
                    ".txt",
                }
            ),
        ),
        "image": PipelineSpec(
            module_path="app.pipeline.image_pipeline",
            class_name="ImagePipeline",
            extensions=frozenset(
                {
                    ".png",
                    ".jpg",
                    ".jpeg",
                }
            ),
        ),
    }

    @classmethod
    def create(
        cls,
        file_path: str | Path,
        **pipeline_kwargs: Any,
    ) -> Any:
        """
        根据输入文件创建对应 Pipeline。

        Args:
            file_path:
                输入文件路径。

            **pipeline_kwargs:
                传递给 Pipeline 构造函数的参数。

        Returns:
            对应格式的 Pipeline 实例。

        Raises:
            FileNotFoundError:
                输入文件不存在。

            IsADirectoryError:
                输入路径不是文件。

            UnsupportedFileTypeError:
                文件格式未被平台正式支持。

            PipelineLoadError:
                Pipeline 模块导入、类获取或实例化失败。
        """

        path = cls._validate_file_path(
            file_path
        )

        try:
            format_route = FormatRouter.route(
                path
            )

        except UnsupportedFormatError as exc:
            raise UnsupportedFileTypeError(
                str(exc)
            ) from exc

        return cls.create_by_route(
            format_route=format_route,
            file_path=path,
            **pipeline_kwargs,
        )

    @classmethod
    def create_by_route(
        cls,
        *,
        format_route: FormatRoute,
        file_path: str | Path | None = None,
        **pipeline_kwargs: Any,
    ) -> Any:
        """
        根据 FormatRoute 创建 Pipeline。

        PipelineRouter 已经完成格式识别时，
        可以调用此方法避免重复执行 FormatRouter.route()。

        此方法仍会验证：
            - pipeline_key 是否已注册
            - 实际扩展名是否属于 Pipeline 白名单
        """

        cls._validate_format_route(
            format_route
        )

        pipeline_key = cls._normalize_pipeline_key(
            format_route.pipeline_key
        )

        spec = cls._get_pipeline_spec(
            pipeline_key
        )

        extension = cls._resolve_route_extension(
            format_route=format_route,
            file_path=file_path,
        )

        cls._validate_pipeline_extension(
            pipeline_key=pipeline_key,
            spec=spec,
            extension=extension,
        )

        pipeline_class = cls._load_pipeline_class(
            pipeline_key=pipeline_key,
            spec=spec,
        )

        try:
            return pipeline_class(
                **pipeline_kwargs
            )

        except Exception as exc:
            file_name = (
                Path(file_path).name
                if file_path is not None
                else "<unknown>"
            )

            raise PipelineLoadError(
                f"Failed to initialize pipeline "
                f"'{spec.class_name}' for "
                f"'{file_name}': {exc}"
            ) from exc

    @classmethod
    def supports(
        cls,
        file_path_or_extension: str | Path,
    ) -> bool:
        """
        判断指定文件或扩展名是否存在可用 Pipeline。

        同时要求：
            1. FormatRouter 能识别该格式
            2. pipeline_key 已在 PipelineFactory 注册
            3. 实际扩展名在 PipelineSpec 白名单中

        Examples:
            PipelineFactory.supports("spec.pdf")
            PipelineFactory.supports(".docx")
            PipelineFactory.supports(".pptx")

            PipelineFactory.supports(".ppt")
                -> False

            PipelineFactory.supports(".xls")
                -> False
        """

        try:
            extension = cls._extract_extension(
                file_path_or_extension
            )

            if not extension:
                return False

            if not FormatRouter.supports(
                file_path_or_extension
            ):
                return False

            route = FormatRouter.route(
                file_path_or_extension,
                validate_exists=False,
            )

            pipeline_key = (
                cls._normalize_pipeline_key(
                    route.pipeline_key
                )
            )

            spec = cls._registry.get(
                pipeline_key
            )

            if spec is None:
                return False

            return spec.supports_extension(
                extension
            )

        except Exception:
            return False

    @classmethod
    def supported_pipeline_keys(
        cls,
    ) -> tuple[str, ...]:
        """返回已经注册的 Pipeline Key。"""

        with cls._lock:
            return tuple(
                sorted(
                    cls._registry.keys()
                )
            )

    @classmethod
    def supported_extensions(
        cls,
    ) -> tuple[str, ...]:
        """
        返回平台实际支持的扩展名。

        这里直接以 PipelineSpec.extensions 为准，
        不返回 FormatRouter 中仅用于识别、
        但没有实际处理能力的扩展名。
        """

        with cls._lock:
            extensions = {
                extension
                for spec in cls._registry.values()
                for extension in spec.extensions
            }

        return tuple(
            sorted(
                extensions
            )
        )

    @classmethod
    def get_spec(
        cls,
        pipeline_key: str,
    ) -> PipelineSpec:
        """
        返回指定 pipeline_key 的注册配置。

        Returns:
            PipelineSpec 的不可变对象。

        Raises:
            UnsupportedFileTypeError:
                pipeline_key 未注册。
        """

        normalized_key = (
            cls._normalize_pipeline_key(
                pipeline_key
            )
        )

        return cls._get_pipeline_spec(
            normalized_key
        )

    @classmethod
    def register(
        cls,
        pipeline_key: str,
        module_path: str,
        class_name: str,
        extensions: set[str]
        | frozenset[str]
        | tuple[str, ...]
        | list[str],
        *,
        overwrite: bool = False,
    ) -> None:
        """
        动态注册 Pipeline。

        Example:
            PipelineFactory.register(
                pipeline_key="markdown",
                module_path=(
                    "app.pipeline.markdown_pipeline"
                ),
                class_name="MarkdownPipeline",
                extensions={
                    ".md",
                    ".markdown",
                },
            )
        """

        normalized_key = (
            cls._normalize_pipeline_key(
                pipeline_key
            )
        )

        normalized_module_path = str(
            module_path
        ).strip()

        normalized_class_name = str(
            class_name
        ).strip()

        normalized_extensions = (
            cls._normalize_extensions(
                extensions
            )
        )

        if not normalized_key:
            raise PipelineRegistrationError(
                "pipeline_key cannot be empty."
            )

        if not normalized_module_path:
            raise PipelineRegistrationError(
                "module_path cannot be empty."
            )

        if not normalized_class_name:
            raise PipelineRegistrationError(
                "class_name cannot be empty."
            )

        if not normalized_extensions:
            raise PipelineRegistrationError(
                "extensions cannot be empty."
            )

        with cls._lock:
            if (
                normalized_key in cls._registry
                and not overwrite
            ):
                raise PipelineRegistrationError(
                    f"Pipeline already registered for: "
                    f"{normalized_key}"
                )

            cls._validate_extension_conflicts(
                pipeline_key=normalized_key,
                extensions=normalized_extensions,
                overwrite=overwrite,
            )

            cls._registry[
                normalized_key
            ] = PipelineSpec(
                module_path=normalized_module_path,
                class_name=normalized_class_name,
                extensions=normalized_extensions,
            )

    @classmethod
    def unregister(
        cls,
        pipeline_key: str,
    ) -> bool:
        """
        删除 Pipeline 注册。

        Returns:
            True:
                注册项存在并已删除。

            False:
                注册项不存在。
        """

        normalized_key = (
            cls._normalize_pipeline_key(
                pipeline_key
            )
        )

        if not normalized_key:
            return False

        with cls._lock:
            return (
                cls._registry.pop(
                    normalized_key,
                    None,
                )
                is not None
            )

    @classmethod
    def _get_pipeline_spec(
        cls,
        pipeline_key: str,
    ) -> PipelineSpec:
        """获取 PipelineSpec。"""

        with cls._lock:
            spec = cls._registry.get(
                pipeline_key
            )

        if spec is not None:
            return spec

        supported = ", ".join(
            cls.supported_pipeline_keys()
        )

        raise UnsupportedFileTypeError(
            f"No pipeline registered for key: "
            f"{pipeline_key or '<empty>'}. "
            f"Supported pipeline keys: "
            f"{supported or '<none>'}"
        )

    @classmethod
    def _load_pipeline_class(
        cls,
        *,
        pipeline_key: str,
        spec: PipelineSpec,
    ) -> type:
        """延迟导入并返回 Pipeline 类。"""

        try:
            module = import_module(
                spec.module_path
            )

        except Exception as exc:
            raise PipelineLoadError(
                f"Failed to import pipeline module "
                f"'{spec.module_path}' for "
                f"pipeline key '{pipeline_key}': {exc}"
            ) from exc

        try:
            pipeline_class = getattr(
                module,
                spec.class_name,
            )

        except AttributeError as exc:
            raise PipelineLoadError(
                f"Pipeline class "
                f"'{spec.class_name}' was not found "
                f"in module '{spec.module_path}'."
            ) from exc

        if not isinstance(
            pipeline_class,
            type,
        ):
            raise PipelineLoadError(
                f"Registered pipeline "
                f"'{spec.module_path}."
                f"{spec.class_name}' is not a class."
            )

        run_method = getattr(
            pipeline_class,
            "run",
            None,
        )

        if not callable(
            run_method
        ):
            raise PipelineLoadError(
                f"Pipeline class "
                f"'{spec.class_name}' must implement "
                f"a callable run() method."
            )

        return pipeline_class

    @classmethod
    def _validate_pipeline_extension(
        cls,
        *,
        pipeline_key: str,
        spec: PipelineSpec,
        extension: str,
    ) -> None:
        """
        验证实际扩展名是否属于 Pipeline 白名单。

        该检查可防止：
            .ppt -> PPTXPipeline
            .xls -> XLSXPipeline
        """

        if spec.supports_extension(
            extension
        ):
            return

        supported = ", ".join(
            sorted(
                spec.extensions
            )
        )

        raise UnsupportedFileTypeError(
            f"Pipeline '{pipeline_key}' does not "
            f"support extension "
            f"'{extension or '<no extension>'}'. "
            f"Supported extensions for this pipeline: "
            f"{supported}"
        )

    @classmethod
    def _validate_extension_conflicts(
        cls,
        *,
        pipeline_key: str,
        extensions: frozenset[str],
        overwrite: bool,
    ) -> None:
        """
        检查扩展名是否已被其他 Pipeline 注册。
        """

        for existing_key, spec in (
            cls._registry.items()
        ):
            if (
                overwrite
                and existing_key == pipeline_key
            ):
                continue

            conflicts = (
                extensions
                & spec.extensions
            )

            if conflicts:
                conflict_text = ", ".join(
                    sorted(
                        conflicts
                    )
                )

                raise PipelineRegistrationError(
                    f"Extensions already registered "
                    f"by pipeline '{existing_key}': "
                    f"{conflict_text}"
                )

    @staticmethod
    def _resolve_route_extension(
        *,
        format_route: FormatRoute,
        file_path: str | Path | None,
    ) -> str:
        """
        获取实际输入扩展名。

        优先使用 file_path，避免 FormatRoute 中的扩展名
        与实际文件不一致。
        """

        if file_path is not None:
            extension = Path(
                file_path
            ).suffix

            if extension:
                return (
                    PipelineFactory
                    .normalize_extension(
                        extension
                    )
                )

        route_extension = getattr(
            format_route,
            "extension",
            "",
        )

        return (
            PipelineFactory
            .normalize_extension(
                route_extension
            )
        )

    @staticmethod
    def _validate_format_route(
        format_route: FormatRoute,
    ) -> None:
        """验证 FormatRoute。"""

        if format_route is None:
            raise ValueError(
                "format_route cannot be None."
            )

        if not isinstance(
            format_route,
            FormatRoute,
        ):
            raise TypeError(
                "format_route must be an "
                "app.router.format_router.FormatRoute "
                "instance."
            )

        pipeline_key = str(
            format_route.pipeline_key
            or ""
        ).strip()

        if not pipeline_key:
            raise UnsupportedFileTypeError(
                "FormatRoute pipeline_key cannot be empty."
            )

    @staticmethod
    def _validate_file_path(
        file_path: str | Path,
    ) -> Path:
        """验证输入文件路径。"""

        path = Path(
            file_path
        ).expanduser()

        if not path.exists():
            raise FileNotFoundError(
                f"Input file not found: {path}"
            )

        if not path.is_file():
            raise IsADirectoryError(
                f"Input path is not a file: {path}"
            )

        if path.name.startswith(
            "~$"
        ):
            raise UnsupportedFileTypeError(
                f"Temporary Office file is not supported: "
                f"{path.name}"
            )

        return path

    @staticmethod
    def _extract_extension(
        file_path_or_extension: str | Path,
    ) -> str:
        """
        从文件路径或扩展名中提取扩展名。
        """

        value = str(
            file_path_or_extension
        ).strip()

        if not value:
            return ""

        if (
            value.startswith(".")
            and "/" not in value
            and "\\" not in value
        ):
            return (
                PipelineFactory
                .normalize_extension(
                    value
                )
            )

        return (
            PipelineFactory
            .normalize_extension(
                Path(value).suffix
            )
        )

    @staticmethod
    def normalize_extension(
        extension: str,
    ) -> str:
        """
        规范化扩展名。

        Examples:
            PDF   -> .pdf
            .DOCX -> .docx
        """

        normalized = str(
            extension
            or ""
        ).strip().lower()

        if not normalized:
            return ""

        if not normalized.startswith(
            "."
        ):
            normalized = (
                f".{normalized}"
            )

        return normalized

    @classmethod
    def _normalize_extensions(
        cls,
        extensions: set[str]
        | frozenset[str]
        | tuple[str, ...]
        | list[str],
    ) -> frozenset[str]:
        """规范化扩展名集合。"""

        if extensions is None:
            return frozenset()

        normalized_extensions = {
            cls.normalize_extension(
                extension
            )
            for extension in extensions
            if str(
                extension
                or ""
            ).strip()
        }

        normalized_extensions.discard(
            ""
        )

        return frozenset(
            normalized_extensions
        )

    @staticmethod
    def _normalize_pipeline_key(
        pipeline_key: str,
    ) -> str:
        """规范化 Pipeline 注册键。"""

        return str(
            pipeline_key
            or ""
        ).strip().lower()