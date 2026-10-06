from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from threading import RLock


class FormatRouterError(RuntimeError):
    """格式路由基础异常。"""


class UnsupportedFormatError(FormatRouterError):
    """文件格式不受支持。"""


class InvalidInputPathError(FormatRouterError):
    """输入路径无效。"""


class FormatRegistrationError(FormatRouterError):
    """格式注册配置异常。"""


@dataclass(frozen=True)
class FormatRoute:
    """
    文件格式路由结果。

    attributes:
        extension:
            标准化扩展名，例如 .pdf。

        format_name:
            标准格式名称，例如 pdf。

        media_type:
            MIME 类型。

        pipeline_key:
            对应的 Pipeline 注册键。
    """

    extension: str
    format_name: str
    media_type: str
    pipeline_key: str


class FormatRouter:
    """
    文件格式路由器。

    职责：
        1. 根据文件路径或扩展名识别文件格式
        2. 返回统一的 FormatRoute
        3. 验证输入文件路径
        4. 提供支持格式查询
        5. 支持动态注册和注销格式

    不负责：
        - 创建 Pipeline
        - 加载文件内容
        - 执行文档解析
        - 判断文件内容是否与扩展名一致
        - 旧格式文件转换

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
        - CSV
        - Markdown
        - HTML
        - XML
    """

    _lock = RLock()

    _routes: dict[str, FormatRoute] = {
        ".pdf": FormatRoute(
            extension=".pdf",
            format_name="pdf",
            media_type="application/pdf",
            pipeline_key="pdf",
        ),
        ".docx": FormatRoute(
            extension=".docx",
            format_name="docx",
            media_type=(
                "application/vnd.openxmlformats-officedocument."
                "wordprocessingml.document"
            ),
            pipeline_key="docx",
        ),
        ".xlsx": FormatRoute(
            extension=".xlsx",
            format_name="xlsx",
            media_type=(
                "application/vnd.openxmlformats-officedocument."
                "spreadsheetml.sheet"
            ),
            pipeline_key="xlsx",
        ),
        ".pptx": FormatRoute(
            extension=".pptx",
            format_name="pptx",
            media_type=(
                "application/vnd.openxmlformats-officedocument."
                "presentationml.presentation"
            ),
            pipeline_key="pptx",
        ),
        ".ppt": FormatRoute(
            extension=".ppt",
            format_name="ppt",
            media_type="application/vnd.ms-powerpoint",
            pipeline_key="ppt",
        ),
        ".txt": FormatRoute(
            extension=".txt",
            format_name="txt",
            media_type="text/plain",
            pipeline_key="txt",
        ),
        ".png": FormatRoute(
            extension=".png",
            format_name="png",
            media_type="image/png",
            pipeline_key="image",
        ),
        ".jpg": FormatRoute(
            extension=".jpg",
            format_name="jpg",
            media_type="image/jpeg",
            pipeline_key="image",
        ),
        ".jpeg": FormatRoute(
            extension=".jpeg",
            format_name="jpeg",
            media_type="image/jpeg",
            pipeline_key="image",
        ),
    }

    @classmethod
    def route(
        cls,
        file_path_or_extension: str | Path,
        *,
        validate_exists: bool = True,
    ) -> FormatRoute:
        """
        根据文件路径或扩展名返回格式路由结果。

        支持输入：

            FormatRouter.route(
                "input/spec.pdf"
            )

            FormatRouter.route(
                ".pdf",
                validate_exists=False,
            )

            FormatRouter.route(
                "pdf",
                validate_exists=False,
            )

        Args:
            file_path_or_extension:
                文件路径、文件名或扩展名。

            validate_exists:
                是否验证真实文件存在。

                传入实际文件路径时通常设为 True。
                传入 ".pdf" 等扩展名时应设为 False。

        Returns:
            FormatRoute

        Raises:
            InvalidInputPathError:
                输入为空、文件不存在或路径不是文件。

            UnsupportedFormatError:
                扩展名未注册。
        """

        extension = cls._resolve_extension(
            file_path_or_extension,
            validate_exists=validate_exists,
        )

        with cls._lock:
            route = cls._routes.get(
                extension
            )

        if route is None:
            supported = ", ".join(
                cls.supported_extensions()
            )

            raise UnsupportedFormatError(
                f"Unsupported file format: "
                f"{extension or '<no extension>'}. "
                f"Supported formats: "
                f"{supported or '<none>'}"
            )

        return route

    @classmethod
    def detect_format(
        cls,
        file_path_or_extension: str | Path,
        *,
        validate_exists: bool = True,
    ) -> str:
        """
        返回标准格式名。

        Examples:
            test.docx -> docx
            .pptx     -> pptx
            xlsx      -> xlsx
        """

        return cls.route(
            file_path_or_extension,
            validate_exists=validate_exists,
        ).format_name

    @classmethod
    def get_pipeline_key(
        cls,
        file_path_or_extension: str | Path,
        *,
        validate_exists: bool = True,
    ) -> str:
        """
        返回 Pipeline 注册键。

        Examples:
            test.pdf -> pdf
            .docx    -> docx
        """

        return cls.route(
            file_path_or_extension,
            validate_exists=validate_exists,
        ).pipeline_key

    @classmethod
    def get_media_type(
        cls,
        file_path_or_extension: str | Path,
        *,
        validate_exists: bool = True,
    ) -> str:
        """返回文件对应的 MIME 类型。"""

        return cls.route(
            file_path_or_extension,
            validate_exists=validate_exists,
        ).media_type

    @classmethod
    def supports(
        cls,
        file_path_or_extension: str | Path,
    ) -> bool:
        """
        判断文件路径、文件名或扩展名是否受支持。

        Examples:
            FormatRouter.supports("spec.pdf")
                -> True

            FormatRouter.supports(".docx")
                -> True

            FormatRouter.supports("xlsx")
                -> True

            FormatRouter.supports(".ppt")
                -> False
        """

        try:
            extension = cls._resolve_extension(
                file_path_or_extension,
                validate_exists=False,
            )

        except (
            InvalidInputPathError,
            TypeError,
            ValueError,
        ):
            return False

        with cls._lock:
            return extension in cls._routes

    @classmethod
    def supported_extensions(
        cls,
    ) -> tuple[str, ...]:
        """返回所有已注册扩展名。"""

        with cls._lock:
            return tuple(
                sorted(
                    cls._routes.keys()
                )
            )

    @classmethod
    def supported_formats(
        cls,
    ) -> tuple[str, ...]:
        """返回所有标准格式名。"""

        with cls._lock:
            formats = {
                route.format_name
                for route in cls._routes.values()
            }

        return tuple(
            sorted(
                formats
            )
        )

    @classmethod
    def supported_pipeline_keys(
        cls,
    ) -> tuple[str, ...]:
        """返回所有已注册的 Pipeline Key。"""

        with cls._lock:
            pipeline_keys = {
                route.pipeline_key
                for route in cls._routes.values()
            }

        return tuple(
            sorted(
                pipeline_keys
            )
        )

    @classmethod
    def routes(
        cls,
    ) -> tuple[FormatRoute, ...]:
        """
        返回所有注册路由。

        返回不可变 tuple，防止外部修改注册表。
        """

        with cls._lock:
            return tuple(
                cls._routes[
                    extension
                ]
                for extension in sorted(
                    cls._routes
                )
            )

    @classmethod
    def get_route_by_extension(
        cls,
        extension: str,
    ) -> FormatRoute:
        """
        根据扩展名获取 FormatRoute。

        Args:
            extension:
                .pdf、pdf、DOCX 等。

        Raises:
            UnsupportedFormatError:
                扩展名未注册。
        """

        normalized_extension = (
            cls._normalize_extension(
                extension
            )
        )

        if not normalized_extension:
            raise UnsupportedFormatError(
                "Extension cannot be empty."
            )

        with cls._lock:
            route = cls._routes.get(
                normalized_extension
            )

        if route is None:
            supported = ", ".join(
                cls.supported_extensions()
            )

            raise UnsupportedFormatError(
                f"Unsupported file format: "
                f"{normalized_extension}. "
                f"Supported formats: "
                f"{supported or '<none>'}"
            )

        return route

    @classmethod
    def register(
        cls,
        *,
        extension: str,
        format_name: str,
        media_type: str,
        pipeline_key: str,
        overwrite: bool = False,
    ) -> None:
        """
        动态注册格式。

        Example:
            FormatRouter.register(
                extension=".json",
                format_name="json",
                media_type="application/json",
                pipeline_key="json",
            )
        """

        normalized_extension = (
            cls._normalize_extension(
                extension
            )
        )

        normalized_format_name = (
            cls._normalize_required_value(
                value=format_name,
                field_name="format_name",
            )
        ).lower()

        normalized_media_type = (
            cls._normalize_required_value(
                value=media_type,
                field_name="media_type",
            )
        )

        normalized_pipeline_key = (
            cls._normalize_required_value(
                value=pipeline_key,
                field_name="pipeline_key",
            )
        ).lower()

        if not normalized_extension:
            raise FormatRegistrationError(
                "extension cannot be empty."
            )

        cls._validate_extension_format(
            normalized_extension
        )

        route = FormatRoute(
            extension=normalized_extension,
            format_name=normalized_format_name,
            media_type=normalized_media_type,
            pipeline_key=normalized_pipeline_key,
        )

        with cls._lock:
            if (
                normalized_extension
                in cls._routes
                and not overwrite
            ):
                raise FormatRegistrationError(
                    f"Format already registered: "
                    f"{normalized_extension}"
                )

            cls._routes[
                normalized_extension
            ] = route

    @classmethod
    def unregister(
        cls,
        extension: str,
    ) -> bool:
        """
        注销指定格式。

        Returns:
            True:
                注册项存在并已删除。

            False:
                注册项不存在。
        """

        normalized_extension = (
            cls._normalize_extension(
                extension
            )
        )

        if not normalized_extension:
            return False

        with cls._lock:
            return (
                cls._routes.pop(
                    normalized_extension,
                    None,
                )
                is not None
            )

    @classmethod
    def _resolve_extension(
        cls,
        file_path_or_extension: str | Path,
        *,
        validate_exists: bool,
    ) -> str:
        """
        统一解析文件路径或扩展名。

        这是 route() 和 supports() 共享的唯一入口，
        用于保证两者行为一致。

        支持：
            .pdf
            pdf
            spec.pdf
            input/spec.pdf
            C:\\documents\\spec.pdf
            Path("input/spec.pdf")
        """

        if file_path_or_extension is None:
            raise InvalidInputPathError(
                "Input path cannot be None."
            )

        value = str(
            file_path_or_extension
        ).strip()

        if not value:
            raise InvalidInputPathError(
                "Input path cannot be empty."
            )

        # =====================
        # Pure extension: .pdf
        # =====================

        if cls._is_pure_extension(
            value
        ):
            if validate_exists:
                raise InvalidInputPathError(
                    "validate_exists=True cannot be used "
                    f"with an extension-only value: {value}"
                )

            return cls._normalize_extension(
                value
            )

        # =====================
        # Extension name: pdf
        # =====================

        if cls._is_extension_name(
            value
        ):
            if validate_exists:
                raise InvalidInputPathError(
                    "validate_exists=True cannot be used "
                    f"with an extension-only value: {value}"
                )

            return cls._normalize_extension(
                value
            )

        # =====================
        # File path
        # =====================

        path = cls._normalize_path(
            value
        )

        if validate_exists:
            cls._validate_path(
                path
            )

        extension = (
            cls._normalize_extension(
                path.suffix
            )
        )

        if not extension:
            raise UnsupportedFormatError(
                f"Input file has no extension: {path}"
            )

        return extension

    @staticmethod
    def _is_pure_extension(
        value: str,
    ) -> bool:
        """
        判断是否为 .pdf 形式的纯扩展名。
        """

        return (
            value.startswith(".")
            and "/" not in value
            and "\\" not in value
            and len(value) > 1
        )

    @staticmethod
    def _is_extension_name(
        value: str,
    ) -> bool:
        """
        判断是否为 pdf 形式的扩展名名称。

        文件名 test.pdf 不属于该情况。
        """

        return (
            "/" not in value
            and "\\" not in value
            and "." not in value
            and value.isalnum()
        )

    @staticmethod
    def _normalize_path(
        file_path: str | Path,
    ) -> Path:
        """规范化文件路径。"""

        value = str(
            file_path
        ).strip()

        if not value:
            raise InvalidInputPathError(
                "Input path cannot be empty."
            )

        return Path(
            value
        ).expanduser()

    @staticmethod
    def _validate_path(
        path: Path,
    ) -> None:
        """验证输入路径。"""

        if path.name.startswith(
            "~$"
        ):
            raise InvalidInputPathError(
                f"Temporary Office file is not supported: "
                f"{path.name}"
            )

        if not path.exists():
            raise InvalidInputPathError(
                f"Input file not found: {path}"
            )

        if not path.is_file():
            raise InvalidInputPathError(
                f"Input path is not a file: {path}"
            )

    @staticmethod
    def _normalize_extension(
        extension: str,
    ) -> str:
        """
        规范化扩展名。

        Examples:
            PDF   -> .pdf
            .DOCX -> .docx
            pptx  -> .pptx
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

    @staticmethod
    def _validate_extension_format(
        extension: str,
    ) -> None:
        """
        验证扩展名格式。

        合法示例：
            .pdf
            .docx
            .xlsx

        不合法示例：
            .
            .test file
            ./pdf
        """

        if extension == ".":
            raise FormatRegistrationError(
                "extension cannot be only '.'."
            )

        if any(
            character.isspace()
            for character in extension
        ):
            raise FormatRegistrationError(
                "extension cannot contain whitespace."
            )

        if (
            "/" in extension
            or "\\" in extension
        ):
            raise FormatRegistrationError(
                "extension cannot contain path separators."
            )

    @staticmethod
    def _normalize_required_value(
        *,
        value: str,
        field_name: str,
    ) -> str:
        """验证并规范化必填字符串。"""

        normalized = str(
            value
            or ""
        ).strip()

        if not normalized:
            raise FormatRegistrationError(
                f"{field_name} cannot be empty."
            )

        return normalized