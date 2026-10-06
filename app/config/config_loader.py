from __future__ import annotations

import os
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml


# ============================================================
# Exceptions
# ============================================================


class ConfigurationError(RuntimeError):
    """应用配置基础异常。"""


class ConfigurationFileNotFoundError(
    ConfigurationError
):
    """配置文件不存在。"""


class ConfigurationValidationError(
    ConfigurationError
):
    """配置内容校验失败。"""


# ============================================================
# Config Models
# ============================================================


@dataclass(frozen=True)
class ApplicationConfig:
    """应用基础配置。"""

    name: str
    environment: str


@dataclass(frozen=True)
class RuntimeConfig:
    """运行目录配置。"""

    input_directory: str
    output_directory: str
    log_directory: str


@dataclass(frozen=True)
class OutputConfig:
    """输出配置。"""

    save_json: bool


@dataclass(frozen=True)
class DatabaseConfig:
    """PostgreSQL 配置。"""

    enabled: bool
    host: str
    port: int
    database: str
    project_databases: dict[str, str]
    user: str
    password: str | None
    password_env: str
    connect_timeout: int


@dataclass(frozen=True)
class ChunkConfig:
    """Chunk 配置。"""

    max_length: int


@dataclass(frozen=True)
class LoggingConfig:
    """日志配置。"""

    level: str
    file_name: str


@dataclass(frozen=True)
class AppConfig:
    """应用完整配置。"""

    application: ApplicationConfig
    runtime: RuntimeConfig
    output: OutputConfig
    database: DatabaseConfig
    chunk: ChunkConfig
    logging: LoggingConfig


# ============================================================
# Config Loader
# ============================================================


class ConfigLoader:
    """
    YAML 配置加载器。

    职责：
        1. 定位 config.yaml
        2. 加载 YAML
        3. 校验配置
        4. 读取数据库密码环境变量
        5. 返回强类型 AppConfig

    支持：
        - Python 开发环境
        - PyInstaller OneDir
        - PyInstaller OneFile

    PyInstaller 路径设计：

        get_application_directory()
            -> EXE 所在目录
            -> 用于 output / logs 等运行时可写目录

        get_resource_directory()
            -> sys._MEIPASS
            -> 用于读取打包进 EXE 的 config.yaml 等只读资源
    """

    DEFAULT_CONFIG_RELATIVE_PATH = (
        Path("config")
        / "config.yaml"
    )

    CONFIG_FILE_ENV = "CONFIG_FILE"

    VALID_LOG_LEVELS = {
        "DEBUG",
        "INFO",
        "WARNING",
        "ERROR",
        "CRITICAL",
    }

    # ========================================================
    # Public API
    # ========================================================

    @classmethod
    def load(
        cls,
        config_path: str | Path | None = None,
    ) -> AppConfig:
        """
        加载应用配置。

        Args:
            config_path:
                可选的自定义 YAML 路径。

        Returns:
            AppConfig
        """

        path = cls.resolve_config_path(
            config_path
        )

        raw_config = cls._load_yaml(
            path
        )

        return cls._build_config(
            raw_config
        )

    @classmethod
    def resolve_config_path(
        cls,
        config_path: str | Path | None = None,
    ) -> Path:
        """
        获取配置文件路径。

        优先级：

            1. 显式 config_path
            2. CONFIG_FILE 环境变量
            3. PyInstaller 内嵌资源目录/config/config.yaml
            4. EXE 所在目录/config/config.yaml（fallback）
            5. Python 项目根目录/config/config.yaml

        OneFile 正常情况下使用第 3 项。
        第 4 项仅作为外部配置 fallback，不要求最终发行目录存在该文件。
        """

        # =====================
        # 1. Explicit Path
        # =====================

        if config_path is not None:
            path = cls._normalize_path_input(
                config_path,
                field_name="config_path",
            )

            return cls._validate_config_path(
                path
            )

        # =====================
        # 2. Environment Path
        # =====================

        environment_path = os.getenv(
            cls.CONFIG_FILE_ENV
        )

        if (
            environment_path is not None
            and environment_path.strip()
        ):
            path = cls._normalize_path_input(
                environment_path,
                field_name=(
                    cls.CONFIG_FILE_ENV
                ),
            )

            return cls._validate_config_path(
                path
            )

        # =====================
        # 3+. Default Candidates
        # =====================

        candidates = (
            cls._get_default_config_candidates()
        )

        for candidate in candidates:
            if (
                candidate.exists()
                and candidate.is_file()
            ):
                return cls._validate_config_path(
                    candidate
                )

        searched_paths = "\n".join(
            f"  - {candidate.resolve()}"
            for candidate in candidates
        )

        raise ConfigurationFileNotFoundError(
            "Configuration file not found. "
            "Searched paths:\n"
            f"{searched_paths}"
        )

    @staticmethod
    def get_application_directory() -> Path:
        """
        获取应用运行目录。

        Python:
            项目根目录。

        PyInstaller:
            EXE 所在目录。

        用途：
            input/
            output/
            logs/
            以及其他运行时可写文件。
        """

        if getattr(
            sys,
            "frozen",
            False,
        ):
            return Path(
                sys.executable
            ).resolve().parent

        return Path(
            __file__
        ).resolve().parents[2]

    @staticmethod
    def get_resource_directory() -> Path:
        """
        获取只读资源目录。

        Python:
            项目根目录。

        PyInstaller OneFile / OneDir:
            PyInstaller 资源目录 sys._MEIPASS。

        用途：
            config/config.yaml
            以及其他打包进 EXE 的静态资源。
        """

        if getattr(
            sys,
            "frozen",
            False,
        ):
            meipass = getattr(
                sys,
                "_MEIPASS",
                None,
            )

            if meipass:
                return Path(
                    meipass
                ).resolve()

        return Path(
            __file__
        ).resolve().parents[2]

    # ========================================================
    # Config Path
    # ========================================================

    @classmethod
    def _get_default_config_candidates(
        cls,
    ) -> list[Path]:
        """
        返回默认配置候选路径。

        冻结环境：
            1. sys._MEIPASS/config/config.yaml
            2. EXE目录/config/config.yaml

        开发环境：
            1. 项目根目录/config/config.yaml

        保持顺序且自动去重。
        """

        candidates = [
            (
                cls.get_resource_directory()
                / cls.DEFAULT_CONFIG_RELATIVE_PATH
            )
        ]

        if getattr(
            sys,
            "frozen",
            False,
        ):
            candidates.append(
                (
                    cls.get_application_directory()
                    / cls.DEFAULT_CONFIG_RELATIVE_PATH
                )
            )

        result: list[Path] = []
        seen: set[str] = set()

        for candidate in candidates:
            key = str(
                candidate.resolve()
            ).casefold()

            if key in seen:
                continue

            seen.add(
                key
            )

            result.append(
                candidate
            )

        return result

    @staticmethod
    def _normalize_path_input(
        value: str | Path,
        *,
        field_name: str,
    ) -> Path:
        """
        标准化配置路径输入。
        """

        if isinstance(
            value,
            Path,
        ):
            return value.expanduser()

        if not isinstance(
            value,
            str,
        ):
            raise ConfigurationValidationError(
                f"{field_name} must be a "
                "string or Path."
            )

        normalized = value.strip()

        if not normalized:
            raise ConfigurationValidationError(
                f"{field_name} cannot be empty."
            )

        return Path(
            normalized
        ).expanduser()

    @staticmethod
    def _validate_config_path(
        path: Path,
    ) -> Path:
        """
        校验配置文件路径。
        """

        try:
            resolved_path = (
                path.resolve()
            )

        except OSError as exc:
            raise ConfigurationValidationError(
                "Unable to resolve "
                f"configuration path: {path}"
            ) from exc

        if not resolved_path.exists():
            raise (
                ConfigurationFileNotFoundError(
                    "Configuration file "
                    f"not found: {resolved_path}"
                )
            )

        if not resolved_path.is_file():
            raise ConfigurationValidationError(
                "Configuration path is "
                f"not a file: {resolved_path}"
            )

        if resolved_path.suffix.lower() not in {
            ".yaml",
            ".yml",
        }:
            raise ConfigurationValidationError(
                "Configuration file must "
                "use .yaml or .yml extension. "
                f"Received: "
                f"{resolved_path.suffix}"
            )

        return resolved_path

    # ========================================================
    # YAML
    # ========================================================

    @staticmethod
    def _load_yaml(
        path: Path,
    ) -> dict[str, Any]:
        """
        加载 YAML。
        """

        try:
            with path.open(
                "r",
                encoding="utf-8",
            ) as file:
                data = yaml.safe_load(
                    file
                )

        except yaml.YAMLError as exc:
            raise ConfigurationError(
                "Failed to parse YAML "
                f"configuration: {exc}"
            ) from exc

        except UnicodeError as exc:
            raise ConfigurationError(
                "Configuration file must "
                f"be valid UTF-8: {path}"
            ) from exc

        except OSError as exc:
            raise ConfigurationError(
                "Failed to read "
                f"configuration file: {exc}"
            ) from exc

        if data is None:
            raise ConfigurationValidationError(
                "Configuration file is empty."
            )

        if not isinstance(
            data,
            dict,
        ):
            raise ConfigurationValidationError(
                "Configuration root "
                "must be a mapping."
            )

        return data

    # ========================================================
    # Build Config
    # ========================================================

    @classmethod
    def _build_config(
        cls,
        data: dict[str, Any],
    ) -> AppConfig:
        """
        构建强类型配置。
        """

        application_data = (
            cls._get_mapping(
                data,
                "application",
            )
        )

        runtime_data = cls._get_mapping(
            data,
            "runtime",
        )

        output_data = cls._get_mapping(
            data,
            "output",
        )

        database_data = cls._get_mapping(
            data,
            "database",
        )

        chunk_data = cls._get_mapping(
            data,
            "chunk",
        )

        logging_data = cls._get_mapping(
            data,
            "logging",
        )

        # =====================
        # Application
        # =====================

        application = ApplicationConfig(
            name=cls._get_string(
                application_data,
                "name",
                default=(
                    "Document Ingestion Platform"
                ),
            ),
            environment=cls._get_string(
                application_data,
                "environment",
                default="development",
            ),
        )

        # =====================
        # Runtime
        # =====================

        runtime = RuntimeConfig(
            input_directory=cls._get_string(
                runtime_data,
                "input_directory",
                default="input",
            ),
            output_directory=cls._get_string(
                runtime_data,
                "output_directory",
                default="output",
            ),
            log_directory=cls._get_string(
                runtime_data,
                "log_directory",
                default="logs",
            ),
        )

        # =====================
        # Output
        # =====================

        output = OutputConfig(
            save_json=cls._get_bool(
                output_data,
                "save_json",
                default=True,
            )
        )

        # =====================
        # Database
        # =====================

        password_env = cls._get_string(
            database_data,
            "password_env",
            default="POSTGRES_PASSWORD",
        )

        # 密码必须保持原始值。
        #
        # 不 strip，不写日志，不写配置对象之外的位置。
        password = os.getenv(
            password_env
        )

        raw_project_databases = database_data.get(
            "project_databases",
            {},
        )
        if raw_project_databases is None:
            raw_project_databases = {}
        if not isinstance(raw_project_databases, dict):
            raise ConfigurationValidationError(
                "database.project_databases must be a mapping."
            )

        default_database_name = cls._get_string(
            database_data,
            "database",
            default="rag",
        )

        project_databases = {
            "21MM": str(
                raw_project_databases.get("21MM", default_database_name)
            ).strip(),
            "24MM": str(
                raw_project_databases.get("24MM", default_database_name)
            ).strip(),
            "COMMON": str(
                raw_project_databases.get("COMMON", default_database_name)
            ).strip(),
            "QABUG": str(
                raw_project_databases.get("QABUG", "rag_qabug")
            ).strip(),
        }
        for project_code, database_name in project_databases.items():
            if not database_name:
                raise ConfigurationValidationError(
                    f"database.project_databases.{project_code} cannot be empty."
                )

        database = DatabaseConfig(
            enabled=cls._get_bool(
                database_data,
                "enabled",
                default=False,
            ),
            host=cls._get_string(
                database_data,
                "host",
                default="127.0.0.1",
            ),
            port=cls._get_int(
                database_data,
                "port",
                default=5432,
                minimum=1,
                maximum=65535,
            ),
            database=default_database_name,
            project_databases=project_databases,
            user=cls._get_string(
                database_data,
                "user",
                default="postgres",
            ),
            password=password,
            password_env=password_env,
            connect_timeout=cls._get_int(
                database_data,
                "connect_timeout",
                default=10,
                minimum=1,
            ),
        )

        # =====================
        # Chunk
        # =====================

        chunk = ChunkConfig(
            max_length=cls._get_int(
                chunk_data,
                "max_length",
                default=1000,
                minimum=1,
            )
        )

        # =====================
        # Logging
        # =====================

        log_level = cls._get_string(
            logging_data,
            "level",
            default="INFO",
        ).upper()

        if log_level not in (
            cls.VALID_LOG_LEVELS
        ):
            raise (
                ConfigurationValidationError(
                    "Invalid logging.level: "
                    f"{log_level}. "
                    "Supported values: "
                    + ", ".join(
                        sorted(
                            cls.VALID_LOG_LEVELS
                        )
                    )
                )
            )

        logging_config = LoggingConfig(
            level=log_level,
            file_name=cls._get_string(
                logging_data,
                "file_name",
                default="application.log",
            ),
        )

        # =====================
        # Final Validation
        # =====================

        cls._validate_database(
            database
        )

        return AppConfig(
            application=application,
            runtime=runtime,
            output=output,
            database=database,
            chunk=chunk,
            logging=logging_config,
        )

    # ========================================================
    # Database Validation
    # ========================================================

    @staticmethod
    def _validate_database(
        config: DatabaseConfig,
    ) -> None:
        """
        数据库配置最终校验。

        database.enabled=False:
            不要求数据库凭据。

        database.enabled=True:
            host / database / user / password
            必须有效。
        """

        if not config.enabled:
            return

        if not config.host:
            raise (
                ConfigurationValidationError(
                    "database.host "
                    "cannot be empty "
                    "when database is enabled."
                )
            )

        if not config.database:
            raise (
                ConfigurationValidationError(
                    "database.database "
                    "cannot be empty "
                    "when database is enabled."
                )
            )

        if not config.user:
            raise (
                ConfigurationValidationError(
                    "database.user "
                    "cannot be empty "
                    "when database is enabled."
                )
            )

        if config.password is None:
            raise (
                ConfigurationValidationError(
                    "PostgreSQL password "
                    "environment variable "
                    f"'{config.password_env}' "
                    "is not configured."
                )
            )

        if config.password == "":
            raise (
                ConfigurationValidationError(
                    "PostgreSQL password "
                    "environment variable "
                    f"'{config.password_env}' "
                    "is empty."
                )
            )

    # ========================================================
    # Mapping Helpers
    # ========================================================

    @staticmethod
    def _get_mapping(
        data: dict[str, Any],
        key: str,
    ) -> dict[str, Any]:
        """
        获取 Mapping 类型配置段。
        """

        value = data.get(
            key,
            {},
        )

        if value is None:
            return {}

        if not isinstance(
            value,
            dict,
        ):
            raise ConfigurationValidationError(
                f"Configuration section "
                f"'{key}' must be a mapping."
            )

        return value

    @staticmethod
    def _get_string(
        data: dict[str, Any],
        key: str,
        *,
        default: str,
    ) -> str:
        """
        获取非空字符串。

        缺失 / None / 空字符串：
            返回 default。

        list / dict 等结构值：
            判定为配置错误，避免静默 str(...)。
        """

        value = data.get(
            key,
            default,
        )

        if value is None:
            return default

        if not isinstance(
            value,
            str,
        ):
            raise ConfigurationValidationError(
                f"Configuration value "
                f"'{key}' must be a string."
            )

        normalized = value.strip()

        if not normalized:
            return default

        return normalized

    @staticmethod
    def _get_bool(
        data: dict[str, Any],
        key: str,
        *,
        default: bool,
    ) -> bool:
        """
        获取 bool。

        支持 YAML bool 以及常见字符串形式。
        """

        value = data.get(
            key,
            default,
        )

        if isinstance(
            value,
            bool,
        ):
            return value

        if isinstance(
            value,
            str,
        ):
            normalized = (
                value.strip().lower()
            )

            if normalized in {
                "true",
                "yes",
                "1",
                "on",
            }:
                return True

            if normalized in {
                "false",
                "no",
                "0",
                "off",
            }:
                return False

        raise ConfigurationValidationError(
            f"Configuration value "
            f"'{key}' must be boolean."
        )

    @staticmethod
    def _get_int(
        data: dict[str, Any],
        key: str,
        *,
        default: int,
        minimum: int | None = None,
        maximum: int | None = None,
    ) -> int:
        """
        获取整数配置。

        允许：
            5432
            "5432"

        拒绝：
            True
            5432.5
            "5432.5"

        防止 int(float) 产生静默截断。
        """

        value = data.get(
            key,
            default,
        )

        if isinstance(
            value,
            bool,
        ):
            raise ConfigurationValidationError(
                f"Configuration value "
                f"'{key}' must be integer."
            )

        if isinstance(
            value,
            int,
        ):
            normalized = value

        elif isinstance(
            value,
            str,
        ):
            stripped = value.strip()

            if not stripped:
                raise ConfigurationValidationError(
                    f"Configuration value "
                    f"'{key}' must be integer."
                )

            try:
                normalized = int(
                    stripped,
                    10,
                )

            except ValueError as exc:
                raise (
                    ConfigurationValidationError(
                        f"Configuration value "
                        f"'{key}' must be integer."
                    )
                ) from exc

        else:
            raise ConfigurationValidationError(
                f"Configuration value "
                f"'{key}' must be integer."
            )

        if (
            minimum is not None
            and normalized < minimum
        ):
            raise ConfigurationValidationError(
                f"Configuration value "
                f"'{key}' must be >= "
                f"{minimum}."
            )

        if (
            maximum is not None
            and normalized > maximum
        ):
            raise ConfigurationValidationError(
                f"Configuration value "
                f"'{key}' must be <= "
                f"{maximum}."
            )

        return normalized