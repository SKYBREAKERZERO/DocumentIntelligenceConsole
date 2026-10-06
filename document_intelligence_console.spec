# -*- mode: python ; coding: utf-8 -*-

"""
Document Intelligence Console Enterprise - PyInstaller OneFile build spec

Target:
    Windows GUI / OneFile

Entry:
    app/gui/application.py

Included desktop capabilities:
    - PDF
    - DOCX
    - PPTX
    - legacy PPT via Microsoft PowerPoint COM
    - XLSX
    - TXT
    - PNG/JPG/JPEG OCR
    - Structured JSON
    - PostgreSQL / RAG Schema v6
    - Specification version history / deterministic diff
    - Deterministic engineering relation extraction / evidence registry
    - Database Explorer / SQL / Visual Query / ERD
    - Document / Embedding administration
    - Qdrant client browser
    - Enterprise AI Workspace / Ollama direct chat
    - OpenAI-compatible local RAG gateway
    - ttkbootstrap GUI

Bundled into the desktop EXE runtime:
    - sentence-transformers client runtime
    - transformers / torch dependencies required by BGE-M3 query embedding
    - Qdrant client

The BAAI/bge-m3 MODEL WEIGHTS are intentionally not embedded into the executable;
the application reuses the standard local Hugging Face cache on the target machine.
Qdrant Server, PostgreSQL Server and Ollama remain external services.
"""

from pathlib import Path

from PyInstaller.utils.hooks import (
    collect_all,
    collect_submodules,
    collect_data_files,
    copy_metadata,
)


# ============================================================
# Project
# ============================================================

project_root = Path(SPEC).resolve().parent

entry_script = (
    project_root
    / "app"
    / "gui"
    / "application.py"
)

if not entry_script.is_file():
    raise FileNotFoundError(
        f"Entry script not found: {entry_script}"
    )


# ============================================================
# Collections
# ============================================================

hiddenimports = []
datas = []
binaries = []


# ============================================================
# Helpers
# ============================================================

def collect_package(package_name):
    """
    Collect package data, native binaries and hidden submodules.

    collect_all() is intentionally used for packages that commonly
    load resources/native DLLs dynamically in a frozen application.
    """

    global datas
    global binaries
    global hiddenimports

    package_datas, package_binaries, package_hiddenimports = (
        collect_all(package_name)
    )

    datas += package_datas
    binaries += package_binaries
    hiddenimports += package_hiddenimports


def safe_collect_package(package_name):
    """
    Collect an optional/runtime package without aborting the spec
    immediately when it is not installed in the build environment.
    """

    try:
        collect_package(package_name)

    except Exception as exc:
        print(
            "[PyInstaller] "
            f"Skipping optional package '{package_name}': {exc}"
        )


def safe_collect_submodules(package_name):
    """
    Safely collect package submodules.
    """

    global hiddenimports

    try:
        hiddenimports += collect_submodules(package_name)

    except Exception as exc:
        print(
            "[PyInstaller] "
            f"Unable to collect submodules for '{package_name}': {exc}"
        )


def collect_project_modules(
    package_directory,
    *,
    excluded_prefixes=(),
    excluded_modules=(),
):
    """
    Convert every project *.py path into a hidden-import module name.

    Why this exists:
        app.pipeline.pipeline_factory uses importlib.import_module()
        with module names stored as strings. Static PyInstaller analysis
        therefore cannot reliably see every pipeline.

        In addition, several project folders are namespace-package style
        folders, so relying only on collect_submodules("app") is less
        deterministic than enumerating the source tree directly.

    Example:
        app/pipeline/image_pipeline.py
            -> app.pipeline.image_pipeline
    """

    result = []

    package_directory = Path(package_directory).resolve()

    for source_file in sorted(
        package_directory.rglob("*.py")
    ):
        relative = source_file.relative_to(
            project_root
        ).with_suffix("")

        parts = list(relative.parts)

        if parts and parts[-1] == "__init__":
            parts = parts[:-1]

        if not parts:
            continue

        module_name = ".".join(parts)

        if module_name in excluded_modules:
            continue

        if any(
            module_name == prefix
            or module_name.startswith(prefix + ".")
            for prefix in excluded_prefixes
        ):
            continue

        result.append(module_name)

    return result


def deduplicate_sequence(values):
    """
    Preserve order while removing duplicate PyInstaller entries.
    """

    result = []
    seen = set()

    for value in values:
        key = repr(value)

        if key in seen:
            continue

        seen.add(key)
        result.append(value)

    return result


# ============================================================
# Application Modules
# ============================================================

app_directory = project_root / "app"

if not app_directory.is_dir():
    raise FileNotFoundError(
        f"Application package directory not found: {app_directory}"
    )

# The unified console exposes Qdrant administration but still does not
# bundle the heavy local embedding model worker.
#
# Everything else under app/ is enumerated explicitly. This fixes the
# dynamic PipelineFactory failure such as:
#
#   No module named 'app.pipeline.image_pipeline'
#
hiddenimports += collect_project_modules(
    app_directory,
    excluded_prefixes=(),
    excluded_modules=(
        # Backup source is not required by the executable.
        "app.gui.application_backup",
    ),
)


# ============================================================
# Dynamic Pipeline Entry Points
# ============================================================
#
# PipelineFactory currently registers these modules by STRING and loads
# them through importlib.import_module(). Keep this list explicit even
# though collect_project_modules() above already discovers the files.
#
# This serves as both documentation and a build-time safety net.
# ============================================================

pipeline_modules = [
    "app.pipeline.pdf_pipeline",
    "app.pipeline.docx_pipeline",
    "app.pipeline.xlsx_pipeline",
    "app.pipeline.pptx_pipeline",
    "app.pipeline.ppt_pipeline",
    "app.pipeline.txt_pipeline",
    "app.pipeline.image_pipeline",
]

hiddenimports += pipeline_modules


# ============================================================
# GUI / Tk
# ============================================================

# ttkbootstrap contains themes/resources that are needed at runtime.
safe_collect_package("ttkbootstrap")

# Pillow is used by ttkbootstrap and the image ingestion path.
safe_collect_package("PIL")


# ============================================================
# Data Models / Validation
# ============================================================
#
# Pydantic v2 contains a native pydantic_core extension. PyInstaller
# normally has hooks for it, but collecting it explicitly improves
# reproducibility across build environments.
# ============================================================

safe_collect_package("pydantic")
safe_collect_package("pydantic_core")


# ============================================================
# PDF
# ============================================================

# PyMuPDF import name is "fitz".
safe_collect_package("fitz")


# ============================================================
# DOCX / PPTX XML Runtime
# ============================================================

safe_collect_package("docx")
safe_collect_package("pptx")

# python-docx and python-pptx rely heavily on lxml.
safe_collect_package("lxml")


# ============================================================
# XLSX
# ============================================================

safe_collect_package("openpyxl")

# openpyxl uses et_xmlfile for XML writing.
safe_collect_package("et_xmlfile")


# ============================================================
# PostgreSQL
# ============================================================

safe_collect_package("psycopg")

# psycopg-binary is an implementation backend loaded by psycopg.
# Do NOT import/collect psycopg_binary directly; that package intentionally
# raises when imported before psycopg and only creates noisy build warnings.


# ============================================================
# YAML / Configuration
# ============================================================

safe_collect_package("yaml")
safe_collect_package("qdrant_client")

# Full AI semantic-retrieval runtime for the Windows executable, but DO NOT
# collect_all(torch/transformers). collect_all() walks distributed, testing,
# CUDA, ONNX, CLI and every Transformers model family and makes the build
# needlessly slow/large. The regular PyInstaller hooks handle native torch
# binaries; we add only the dynamic modules used by SentenceTransformer +
# BAAI/bge-m3 (XLM-R based).
for _pkg in ("sentence_transformers", "transformers"):
    try:
        datas += collect_data_files(_pkg, include_py_files=False)
    except Exception as exc:
        print(f"[PyInstaller] Unable to collect data for '{_pkg}': {exc}")

for _dist in (
    "sentence-transformers",
    "transformers",
    "tokenizers",
    "safetensors",
    "huggingface-hub",
    "torch",
):
    try:
        datas += copy_metadata(_dist)
    except Exception as exc:
        print(f"[PyInstaller] Unable to copy metadata for '{_dist}': {exc}")

hiddenimports += [
    # Sentence-Transformers runtime used by app.embedding.embedding_client
    "sentence_transformers",
    "sentence_transformers.SentenceTransformer",
    "sentence_transformers.models",
    "sentence_transformers.models.Transformer",
    "sentence_transformers.models.Pooling",
    "sentence_transformers.models.Normalize",
    "sentence_transformers.util",

    # Hugging Face runtime and the model family used by BAAI/bge-m3.
    "transformers",
    "transformers.models.auto.configuration_auto",
    "transformers.models.auto.modeling_auto",
    "transformers.models.auto.tokenization_auto",
    "transformers.models.xlm_roberta",
    "transformers.models.xlm_roberta.configuration_xlm_roberta",
    "transformers.models.xlm_roberta.modeling_xlm_roberta",
    "transformers.models.xlm_roberta.tokenization_xlm_roberta",
    "transformers.models.xlm_roberta.tokenization_xlm_roberta_fast",

    # Native/runtime dependencies. Standard PyInstaller hooks collect their
    # required binaries; we intentionally do not enumerate all submodules.
    "torch",
    "tokenizers",
    "safetensors",
    "safetensors.torch",
    "huggingface_hub",
]

safe_collect_package("keyring")
safe_collect_package("sqlparse")


# ============================================================
# OCR / Image Runtime
# ============================================================
#
# ImageLoader lazily executes:
#
#     from rapidocr import RapidOCR
#
# This import is not visible to normal static analysis until the PNG/JPG
# path is actually used. RapidOCR also depends on native/image packages
# and model/config resources.
#
# collect_all("rapidocr") is important because it captures package data
# such as packaged model/config resources when present.
# ============================================================

safe_collect_package("rapidocr")
safe_collect_package("onnxruntime")

# Explicit OCR/image dependencies. These are deliberately collected
# because OCR stacks frequently import/select them dynamically.
safe_collect_package("cv2")
safe_collect_package("shapely")
safe_collect_package("pyclipper")
safe_collect_package("omegaconf")

# numpy is a native dependency of ONNX/OpenCV. Its official PyInstaller
# hook is normally sufficient; the explicit hidden import makes intent
# clear without force-collecting every numpy test/submodule.
hiddenimports += [
    "numpy",
]


# ============================================================
# Windows COM / Legacy .PPT
# ============================================================
#
# app/converter/ppt_converter.py lazily imports pythoncom and
# win32com.client only when a legacy .ppt file is processed.
#
# Microsoft PowerPoint itself must still be installed on the target PC.
# ============================================================

hiddenimports += [
    "pythoncom",
    "pywintypes",
    "win32api",
    "win32timezone",
    "win32com",
    "win32com.client",
    "win32com.client.dynamic",
    "win32com.client.gencache",
    "win32com.client.makepy",
]

# Explicit COM modules above are sufficient for legacy PowerPoint conversion.
# Do not collect all win32com demos/tests/ADSI/debug modules.


# ============================================================
# Application Configuration Data
# ============================================================

config_file = (
    project_root
    / "config"
    / "config.yaml"
)

if not config_file.is_file():
    raise FileNotFoundError(
        f"Required configuration file not found: {config_file}"
    )

# ConfigLoader looks for:
#     sys._MEIPASS/config/config.yaml
#
# Destination "config" preserves that path inside OneFile extraction.
datas.append(
    (
        str(config_file),
        "config",
    )
)

taxonomy_file = (
    project_root
    / "config"
    / "specification_taxonomy.yaml"
)

if not taxonomy_file.is_file():
    raise FileNotFoundError(
        f"Required taxonomy file not found: {taxonomy_file}"
    )

# SpecificationTaxonomyLoader reads the bundled fallback from
# sys._MEIPASS/config/specification_taxonomy.yaml.
datas.append(
    (
        str(taxonomy_file),
        "config",
    )
)

relation_rules_file = (
    project_root
    / "config"
    / "engineering_relation_rules.yaml"
)

if not relation_rules_file.is_file():
    raise FileNotFoundError(
        f"Required engineering relation rules file not found: {relation_rules_file}"
    )

datas.append(
    (
        str(relation_rules_file),
        "config",
    )
)


# ============================================================
# Deduplicate
# ============================================================

hiddenimports = sorted(set(hiddenimports))
datas = deduplicate_sequence(datas)
binaries = deduplicate_sequence(binaries)


# ============================================================
# Build-Time Validation
# ============================================================

# Fail the build early if a dynamically registered pipeline source file
# has been deleted/renamed but PipelineFactory/spec was not updated.
for module_name in pipeline_modules:
    module_file = (
        project_root
        / Path(
            module_name.replace(".", "/") + ".py"
        )
    )

    if not module_file.is_file():
        raise FileNotFoundError(
            "Registered pipeline source file is missing: "
            f"{module_name} -> {module_file}"
        )


# ============================================================
# Debug Information
# ============================================================

print("============================================================")
print("[PyInstaller] Document Intelligence Console Enterprise")
print(f"[PyInstaller] Project root: {project_root}")
print(f"[PyInstaller] Entry script: {entry_script}")
print(f"[PyInstaller] Hidden imports: {len(hiddenimports)}")
print(f"[PyInstaller] Data entries: {len(datas)}")
print(f"[PyInstaller] Binary entries: {len(binaries)}")
print("[PyInstaller] Pipelines:")
for module_name in pipeline_modules:
    print(f"  - {module_name}")
print("============================================================")


# ============================================================
# Analysis
# ============================================================

analysis = Analysis(
    [
        str(entry_script),
    ],

    # Critical for top-level imports such as:
    #     from app.pipeline.pipeline_factory import PipelineFactory
    pathex=[
        str(project_root),
    ],

    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,

    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],

    excludes=[
        # ====================================================
        # Development / Notebook
        # ====================================================
        "matplotlib",
        "notebook",
        "jupyter",
        "IPython",

        # ====================================================
        # Legacy PostgreSQL Driver
        # ====================================================
        "psycopg2",

        # ====================================================
        # Heavy development/training stacks not used by BGE-M3 inference
        # ====================================================
        "torch.testing",
        "torch.distributed",
        "torch.onnx",
        "torch._inductor",
        "torch.profiler",
        "torch.utils.benchmark",
        "transformers.cli",
        "transformers.testing_utils",

        # pywin32 optional/demo/test families; PowerPoint conversion only needs
        # win32com.client + pythoncom/pywintypes.
        "win32com.test",
        "win32com.demos",
        "win32com.axdebug",
        "win32com.axscript",
        "win32com.adsi",
        "win32com.directsound",
    ],

    noarchive=False,
    optimize=0,
)


# ============================================================
# Python Archive
# ============================================================

pyz = PYZ(
    analysis.pure,
)


# ============================================================
# OneFile Windows GUI EXE
# ============================================================

version_info_file = project_root / "windows_version_info.txt"

exe = EXE(
    pyz,
    analysis.scripts,
    analysis.binaries,
    analysis.datas,
    [],

    name="DocumentIntelligenceConsole",
    version=str(version_info_file),

    debug=False,
    bootloader_ignore_signals=False,
    strip=False,

    # UPX is intentionally disabled because native packages such as
    # ONNX Runtime, PyMuPDF, OpenCV and psycopg are more predictable
    # without binary compression.
    upx=False,

    # Windows GUI application.
    console=False,

    # Keep PyInstaller's windowed traceback support.
    disable_windowed_traceback=False,
)
