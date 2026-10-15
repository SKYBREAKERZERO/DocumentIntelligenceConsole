# Source-release packaging notice (v1.28)

The legacy Windows EXE build notes below belong to the earlier v1.03 desktop-only build. The included build_exe.ps1 now fails fast to avoid producing a misleading standalone v1.28 EXE. **They do not build or verify the v1.08 dual-process/watchdog deployment.** v1.08 GUI auto-start uses `.venv\Scripts\python.exe -m app.server` when running source. When packaging a frozen desktop EXE, you must also build/package a separate `DocumentIntelligenceBackend.exe`; the GUI deliberately refuses to simulate successful auto-start if that companion does not exist. Validate this separately on Windows before claiming production EXE support.

---

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
