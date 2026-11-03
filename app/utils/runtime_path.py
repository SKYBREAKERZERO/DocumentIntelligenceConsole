from __future__ import annotations

import sys
from pathlib import Path


def get_application_directory() -> Path:
    """
    返回应用程序运行目录。

    开发环境：
        返回项目根目录。

    PyInstaller 环境：
        返回 EXE 所在目录。
    """

    if getattr(sys, "frozen", False):
        return Path(
            sys.executable
        ).resolve().parent

    return Path(
        __file__
    ).resolve().parents[2]


def ensure_runtime_directories() -> dict[str, Path]:
    """
    创建运行所需目录。
    """

    base_directory = (
        get_application_directory()
    )

    input_directory = (
        base_directory / "input"
    )

    output_directory = (
        base_directory / "output"
    )

    log_directory = (
        base_directory / "logs"
    )

    for directory in (
        input_directory,
        output_directory,
        log_directory,
    ):
        directory.mkdir(
            parents=True,
            exist_ok=True,
        )

    return {
        "base": base_directory,
        "input": input_directory,
        "output": output_directory,
        "logs": log_directory,
    }