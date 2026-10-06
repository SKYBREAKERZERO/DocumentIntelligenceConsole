from __future__ import annotations

from contextlib import contextmanager
from typing import Iterator

import psycopg
from psycopg import Connection

from app.config.config_loader import (
    ConfigLoader,
    DatabaseConfig,
)


class DatabaseConnectionError(RuntimeError):
    """
    PostgreSQL 数据库连接异常。
    """


class DatabaseConnection:
    """
    PostgreSQL 数据库连接管理器。

    职责：
        - 从 DatabaseConfig 获取数据库连接参数
        - 创建 PostgreSQL Connection
        - 支持依赖注入
        - 支持 ConfigLoader 默认配置
        - 统一处理数据库连接异常
        - 不在源码中保存数据库密码

    默认配置来源：

        config/config.yaml
            ↓
        ConfigLoader
            ↓
        DatabaseConfig
            ↓
        DatabaseConnection
    """

    def __init__(
        self,
        config: DatabaseConfig | None = None,
    ) -> None:

        if config is None:
            app_config = (
                ConfigLoader.load()
            )

            config = (
                app_config.database
            )

        self.config = config

        self._validate_config()

    def _validate_config(
        self,
    ) -> None:
        """
        校验 PostgreSQL 配置。
        """

        if not self.config.enabled:
            raise DatabaseConnectionError(
                "PostgreSQL storage is disabled "
                "in configuration."
            )

        if not self.config.host:
            raise DatabaseConnectionError(
                "PostgreSQL host cannot be empty."
            )

        if (
            self.config.port < 1
            or self.config.port > 65535
        ):
            raise DatabaseConnectionError(
                "PostgreSQL port must be "
                "between 1 and 65535."
            )

        if not self.config.database:
            raise DatabaseConnectionError(
                "PostgreSQL database "
                "cannot be empty."
            )

        if not self.config.user:
            raise DatabaseConnectionError(
                "PostgreSQL user "
                "cannot be empty."
            )

        if not self.config.password:
            raise DatabaseConnectionError(
                "PostgreSQL password "
                "is not configured. "
                f"Expected environment variable: "
                f"{self.config.password_env}"
            )

        if self.config.connect_timeout < 1:
            raise DatabaseConnectionError(
                "PostgreSQL connect_timeout "
                "must be greater than 0."
            )

    def connect(
        self,
    ) -> Connection:
        """
        创建 PostgreSQL Connection。

        Returns:
            psycopg.Connection

        Raises:
            DatabaseConnectionError:
                数据库连接失败。
        """

        try:
            return psycopg.connect(
                host=self.config.host,
                port=self.config.port,
                dbname=self.config.database,
                user=self.config.user,
                password=self.config.password,
                connect_timeout=(
                    self.config.connect_timeout
                ),
            )

        except psycopg.Error as exc:
            raise DatabaseConnectionError(
                "Failed to connect to PostgreSQL. "
                f"host={self.config.host}, "
                f"port={self.config.port}, "
                f"database={self.config.database}, "
                f"user={self.config.user}. "
                f"Reason: {exc}"
            ) from exc

    def test_connection(
        self,
    ) -> bool:
        """
        测试 PostgreSQL 是否可以正常连接。

        执行：

            SELECT 1

        Returns:
            True:
                数据库连接正常。

        Raises:
            DatabaseConnectionError:
                连接或查询失败。
        """

        try:
            with self.connect() as connection:
                with connection.cursor() as cursor:
                    cursor.execute(
                        "SELECT 1"
                    )

                    result = (
                        cursor.fetchone()
                    )

                    return bool(
                        result
                        and result[0] == 1
                    )

        except DatabaseConnectionError:
            raise

        except psycopg.Error as exc:
            raise DatabaseConnectionError(
                "PostgreSQL connection "
                f"test failed: {exc}"
            ) from exc

    @contextmanager
    def connection(
        self,
    ) -> Iterator[Connection]:
        """
        提供显式连接上下文。

        Example:

            with db.connection() as conn:
                ...

        正常：
            commit

        异常：
            rollback

        最终：
            close
        """

        conn: Connection | None = None

        try:
            conn = self.connect()

            yield conn

            conn.commit()

        except Exception:
            if conn is not None:
                conn.rollback()

            raise

        finally:
            if conn is not None:
                conn.close()

    def get_connection_info(
        self,
    ) -> dict[str, str | int]:
        """
        返回安全的数据库连接信息。

        注意：
            不返回 password。
        """

        return {
            "host": self.config.host,
            "port": self.config.port,
            "database": self.config.database,
            "user": self.config.user,
            "connect_timeout": (
                self.config.connect_timeout
            ),
        }