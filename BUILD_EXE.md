# Document Intelligence Console Enterprise v1.03 — Windows EXE Build

## One-click build

On a **Windows 10/11 x64** machine, extract this package and double-click:

`build_exe.cmd`

The builder creates an isolated build environment when required, installs the full runtime, runs source/tests checks, and builds:

`release\v1.03\DocumentIntelligenceConsole.exe`

A SHA-256 checksum is written beside the executable.

## Runtime dependencies intentionally external

The EXE contains the desktop application and Python ML runtime required for semantic retrieval. The following services/data remain external by design:

- PostgreSQL server and your existing RAG databases
- Qdrant server and collections
- Ollama and the selected chat model
- BAAI/bge-m3 model **weights** in the normal Hugging Face cache
- Microsoft PowerPoint when legacy `.ppt` conversion is needed

The application does **not** embed PostgreSQL/Qdrant/Ollama databases or model weights into the executable.

## Build notes

- 64-bit Python 3.12 or 3.13 is required if no project `.venv` exists.
- The full AI runtime includes PyTorch / Transformers / SentenceTransformers, so the resulting OneFile EXE can be large and the first launch can take longer than the source version.
- The build script intentionally performs a clean PyInstaller build to prevent stale module/DLL leakage between releases.

## v1.03 build verification dependency

The EXE build runs a focused pytest regression suite before PyInstaller. `pytest==9.1.1` is now included in `requirements-exe.txt`, and `build_exe.ps1` also self-installs pytest when an older `.venv-build` is reused. You do not need to delete the existing build virtual environment.

## Pytest Temp / WinError 5

The EXE verification step does not use the global Windows pytest temp directory.
`build_exe.ps1` forces `TEMP`, `TMP`, and pytest `--basetemp` to a project-local
`.build-temp` directory. This avoids `PermissionError: [WinError 5]` caused by
stale or restricted ACLs under `%LOCALAPPDATA%\Temp\pytest-of-<user>`.

If an older build environment produced that error, use the current build package
and rerun `build_exe.cmd`; no administrator shell and no manual deletion of the
global pytest temp directory is required.
