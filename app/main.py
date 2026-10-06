from __future__ import annotations

import logging
import sys
import traceback
from dataclasses import dataclass
from pathlib import Path

from app.config.config_loader import (
    AppConfig,
    ConfigLoader,
    ConfigurationError,
)
from app.router.pipeline_router import (
    PipelineRouteResult,
    PipelineRouter,
)


LOGGER = logging.getLogger(
    "document_ingestion"
)


# =====================
# Runtime Environment
# =====================

IS_FROZEN = bool(
    getattr(
        sys,
        "frozen",
        False,
    )
)


@dataclass(frozen=True)
class RuntimeContext:
    """
    主程序运行上下文。

    仅保存已经加载并解析完成的运行配置，
    避免在模块 import 阶段读取 config.yaml。
    """

    config: AppConfig
    base_directory: Path
    input_directory: Path
    output_directory: Path
    log_directory: Path
    save_json: bool
    save_database: bool
    chunk_max_length: int


def get_application_directory() -> Path:
    """
    获取应用程序运行目录。

    Python 开发环境：
        当前 main.py 位于 app/ 下时，
        返回项目根目录。

    PyInstaller EXE：
        返回 EXE 所在目录。
    """

    if IS_FROZEN:
        return Path(
            sys.executable
        ).resolve().parent

    return Path(
        __file__
    ).resolve().parents[1]


def load_application_config() -> AppConfig:
    """
    加载应用配置。

    注意：
        只允许在真正启动应用时调用。

        不在模块 import 阶段执行，
        这样测试、GUI、其他模块 import main
        时不会因为配置文件或数据库环境变量
        产生副作用。
    """

    try:
        return ConfigLoader.load()

    except ConfigurationError:
        raise

    except Exception as exc:
        raise ConfigurationError(
            "Unexpected configuration loading error: "
            f"{exc}"
        ) from exc


def build_runtime_context(
    config: AppConfig,
) -> RuntimeContext:
    """
    根据 AppConfig 构建运行上下文。
    """

    if config is None:
        raise ConfigurationError(
            "Application config cannot be None."
        )

    base_directory = (
        get_application_directory()
    )

    input_directory = (
        base_directory
        / config.runtime.input_directory
    ).resolve()

    output_directory = (
        base_directory
        / config.runtime.output_directory
    ).resolve()

    log_directory = (
        base_directory
        / config.runtime.log_directory
    ).resolve()

    save_json = bool(
        config.output.save_json
    )

    save_database = bool(
        config.database.enabled
    )

    chunk_max_length = int(
        config.chunk.max_length
    )

    if chunk_max_length <= 0:
        raise ConfigurationError(
            "chunk.max_length must be greater than 0."
        )

    if (
        not save_json
        and not save_database
    ):
        raise ConfigurationError(
            "At least one output must be enabled: "
            "JSON or PostgreSQL."
        )

    return RuntimeContext(
        config=config,
        base_directory=base_directory,
        input_directory=input_directory,
        output_directory=output_directory,
        log_directory=log_directory,
        save_json=save_json,
        save_database=save_database,
        chunk_max_length=chunk_max_length,
    )


# =====================
# Runtime Directories
# =====================


def ensure_directories(
    context: RuntimeContext,
) -> None:
    """
    创建当前运行模式真正需要的目录。

    JSON disabled 时不主动创建 output 目录，
    避免 database-only 模式产生无关 JSON 副作用。
    """

    required_directories = [
        context.input_directory,
        context.log_directory,
    ]

    if context.save_json:
        required_directories.append(
            context.output_directory
        )

    for directory in required_directories:
        directory.mkdir(
            parents=True,
            exist_ok=True,
        )


# =====================
# Logging
# =====================


def configure_logging(
    context: RuntimeContext,
) -> None:
    """
    初始化日志系统。
    """

    context.log_directory.mkdir(
        parents=True,
        exist_ok=True,
    )

    log_file = (
        context.log_directory
        / context.config.logging.file_name
    )

    log_level = getattr(
        logging,
        context.config.logging.level,
        logging.INFO,
    )

    logging.basicConfig(
        level=log_level,
        format=(
            "%(asctime)s | "
            "%(levelname)s | "
            "%(name)s | "
            "%(message)s"
        ),
        handlers=[
            logging.FileHandler(
                log_file,
                encoding="utf-8",
            ),
            logging.StreamHandler(
                sys.stdout
            ),
        ],
        force=True,
    )


# =====================
# Pipeline
# =====================


def build_pipeline_kwargs(
    context: RuntimeContext,
) -> dict[str, object]:
    """
    构建所有格式 Pipeline 共用参数。
    """

    return {
        "save_json": context.save_json,
        "save_database": (
            context.save_database
        ),
        "chunk_max_length": (
            context.chunk_max_length
        ),
    }


def build_pipeline_router(
    context: RuntimeContext,
) -> PipelineRouter:
    """
    创建统一 PipelineRouter。

    Main 不再自己维护：

        extension -> Pipeline

    映射，也不再直接调用 PipelineFactory。
    """

    return PipelineRouter(
        output_directory=(
            context.output_directory
        ),
        overwrite_output=True,
        pipeline_kwargs=(
            build_pipeline_kwargs(
                context
            )
        ),
    )


# =====================
# Presentation
# =====================


def print_runtime_information(
    context: RuntimeContext,
) -> None:
    """
    输出当前运行环境与应用配置。

    不输出数据库密码。
    """

    runtime_mode = (
        "PyInstaller EXE"
        if IS_FROZEN
        else "Python"
    )

    print()
    print(
        "========================================"
    )
    print(
        context.config.application.name
    )
    print(
        "========================================"
    )

    print(
        "Runtime:",
        runtime_mode,
    )

    print(
        "Environment:",
        context.config.application.environment,
    )

    print(
        "Base directory:",
        context.base_directory,
    )

    print(
        "Input directory:",
        context.input_directory,
    )

    if context.save_json:
        print(
            "Output directory:",
            context.output_directory,
        )

    print(
        "Log directory:",
        context.log_directory,
    )

    print(
        "JSON:",
        (
            "Enabled"
            if context.save_json
            else "Disabled"
        ),
    )

    print(
        "PostgreSQL:",
        (
            "Enabled"
            if context.save_database
            else "Disabled"
        ),
    )

    if context.save_database:
        print(
            "DB Host:",
            context.config.database.host,
        )

        print(
            "DB Port:",
            context.config.database.port,
        )

        print(
            "DB Name:",
            context.config.database.database,
        )

        print(
            "DB User:",
            context.config.database.user,
        )

    print(
        "Chunk max length:",
        context.chunk_max_length,
    )

    print(
        "========================================"
    )


def print_success_result(
    result: PipelineRouteResult,
    *,
    save_json: bool,
    save_database: bool,
) -> None:
    """
    输出单文件成功结果。
    """

    document = result.document

    print()
    print("====================")
    print(
        f"Completed: {result.input_path.name}"
    )
    print("====================")

    print(
        "Pipeline:",
        result.pipeline_name,
    )

    print(
        "Pages:",
        len(document.pages),
    )

    print(
        "Chapters:",
        len(document.chapters),
    )

    print(
        "Sections:",
        len(document.sections),
    )

    print(
        "Contents:",
        len(document.contents),
    )

    if save_json:
        print(
            "Output:",
            result.output_path,
        )

    if save_database:
        print(
            "Database:",
            "Saved",
        )


def print_failure_result(
    *,
    input_path: Path,
    error: Exception,
) -> None:
    """
    输出单文件失败结果。
    """

    print()
    print("====================")
    print(
        f"Failed: {input_path.name}"
    )
    print("====================")

    print(
        f"{type(error).__name__}: {error}"
    )


def print_batch_summary(
    *,
    success_count: int,
    failure_count: int,
    total_count: int,
    context: RuntimeContext,
) -> None:
    """
    输出批处理汇总。
    """

    print()
    print("====================")
    print("Batch Summary")
    print("====================")

    print(
        "Success:",
        success_count,
    )

    print(
        "Failed:",
        failure_count,
    )

    print(
        "Total:",
        total_count,
    )

    print(
        "JSON:",
        (
            "Enabled"
            if context.save_json
            else "Disabled"
        ),
    )

    print(
        "PostgreSQL:",
        (
            "Enabled"
            if context.save_database
            else "Disabled"
        ),
    )


# =====================
# Error Logging
# =====================


def log_failure_traceback(
    *,
    input_path: Path,
    error: Exception,
) -> None:
    """
    将保存于异常对象上的 traceback 写入 debug log。

    PipelineRouter 批处理会把异常对象放入
    PipelineFailureResult，因此这里不需要丢失底层栈信息。
    """

    LOGGER.error(
        "Processing failed | "
        "file=%s | "
        "error_type=%s | "
        "error=%s",
        input_path.name,
        type(error).__name__,
        error,
    )

    trace = "".join(
        traceback.format_exception(
            type(error),
            error,
            error.__traceback__,
        )
    )

    LOGGER.debug(
        "Processing traceback | "
        "file=%s\n%s",
        input_path.name,
        trace,
    )


# =====================
# Exit
# =====================


def wait_before_exit() -> None:
    """
    EXE 环境下暂停窗口。

    Python 开发环境不暂停。
    """

    if not IS_FROZEN:
        return

    try:
        input(
            "\nPress Enter to exit..."
        )

    except (
        EOFError,
        KeyboardInterrupt,
    ):
        pass


# =====================
# Main
# =====================


def main() -> None:
    """
    Document Ingestion Platform 主入口。

    流程：

        Config
            ↓
        RuntimeContext
            ↓
        Runtime Directories
            ↓
        PipelineRouter
            ↓
        FormatRouter
            ↓
        PipelineFactory
            ↓
        PDF / DOCX / PPTX / XLSX Pipeline
            ↓
        JSON / PostgreSQL
    """

    # 配置只在真正启动时读取。
    config = load_application_config()

    context = build_runtime_context(
        config
    )

    ensure_directories(
        context
    )

    configure_logging(
        context
    )

    print_runtime_information(
        context
    )

    LOGGER.info(
        "Application started."
    )

    LOGGER.info(
        "Runtime mode: %s",
        (
            "frozen"
            if IS_FROZEN
            else "python"
        ),
    )

    LOGGER.info(
        "Environment: %s",
        context.config.application.environment,
    )

    LOGGER.info(
        "Base directory: %s",
        context.base_directory,
    )

    LOGGER.info(
        "Input directory: %s",
        context.input_directory,
    )

    if context.save_json:
        LOGGER.info(
            "Output directory: %s",
            context.output_directory,
        )

    LOGGER.info(
        "Log directory: %s",
        context.log_directory,
    )

    LOGGER.info(
        "JSON enabled: %s",
        context.save_json,
    )

    LOGGER.info(
        "PostgreSQL enabled: %s",
        context.save_database,
    )

    LOGGER.info(
        "Chunk max length: %s",
        context.chunk_max_length,
    )

    router = build_pipeline_router(
        context
    )

    # PipelineRouter 统一负责：
    #
    #     - 文件发现
    #     - 格式支持判断
    #     - FormatRouter
    #     - PipelineFactory
    #     - 单文件错误隔离
    #     - 输出路径生成
    #
    # Main 只负责应用层展示与汇总。
    (
        success_results,
        failed_results,
    ) = router.execute_directory(
        context.input_directory,
        recursive=False,
        continue_on_error=True,
    )

    total_count = (
        len(success_results)
        + len(failed_results)
    )

    if total_count == 0:
        message = (
            "No supported input files found. "
            "Input directory: "
            f"{context.input_directory}"
        )

        print()
        print(
            message
        )

        LOGGER.warning(
            message
        )

        wait_before_exit()

        return

    LOGGER.info(
        "Batch processed | "
        "success=%s | "
        "failed=%s | "
        "total=%s",
        len(success_results),
        len(failed_results),
        total_count,
    )

    for result in success_results:
        print_success_result(
            result,
            save_json=context.save_json,
            save_database=(
                context.save_database
            ),
        )

        document = result.document

        LOGGER.info(
            "Processing completed | "
            "file=%s | "
            "pipeline=%s | "
            "pages=%s | "
            "chapters=%s | "
            "sections=%s | "
            "contents=%s | "
            "save_json=%s | "
            "save_database=%s | "
            "output=%s",
            result.input_path.name,
            result.pipeline_name,
            len(document.pages),
            len(document.chapters),
            len(document.sections),
            len(document.contents),
            context.save_json,
            context.save_database,
            (
                result.output_path
                if context.save_json
                else "<disabled>"
            ),
        )

    for failure in failed_results:
        print_failure_result(
            input_path=failure.input_path,
            error=failure.error,
        )

        log_failure_traceback(
            input_path=failure.input_path,
            error=failure.error,
        )

    print_batch_summary(
        success_count=len(
            success_results
        ),
        failure_count=len(
            failed_results
        ),
        total_count=total_count,
        context=context,
    )

    LOGGER.info(
        "Batch completed | "
        "success=%s | "
        "failed=%s | "
        "total=%s",
        len(success_results),
        len(failed_results),
        total_count,
    )

    wait_before_exit()


if __name__ == "__main__":
    try:
        main()

    except ConfigurationError as exc:
        print()
        print(
            "Configuration Error:"
        )
        print(
            exc
        )

        if IS_FROZEN:
            try:
                input(
                    "\nPress Enter to exit..."
                )

            except (
                EOFError,
                KeyboardInterrupt,
            ):
                pass

        raise SystemExit(
            2
        ) from exc

    except KeyboardInterrupt:
        print()
        print(
            "Operation cancelled by user."
        )

        raise SystemExit(
            130
        )