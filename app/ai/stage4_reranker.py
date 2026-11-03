"""Opt-in Stage 4 local-only CrossEncoder ranking (NOT engineering confidence).

Hard inference deadlines require a killable process, not a ThreadPoolExecutor:
Python cannot cancel an already-running native Torch thread. This deliberately
starts an isolated CPU process for each request and fails back to unchanged RRF.
The model path must point to an existing local directory; downloads are disabled.
"""
from __future__ import annotations

import math
import multiprocessing
import os
import threading
import time
from dataclasses import dataclass, replace
from pathlib import Path
from typing import TYPE_CHECKING, Callable, Sequence

from app.ai.hybrid_retrieval import _identity

if TYPE_CHECKING:
    from app.ai.retrieval import EvidenceItem

_PROCESS_SLOT = threading.Lock()


@dataclass(frozen=True)
class RerankConfig:
    enabled: bool = False
    model_dir: str = ""
    max_candidates: int = 12
    query_chars: int = 512
    text_chars: int = 1800
    batch_size: int = 2
    cpu_threads: int = 2
    timeout_s: float = 30.0
    # RLIMIT_AS is Linux-only and can conflict with Torch memory mappings.
    # Explicit opt-in; Windows hard memory caps require external Job Objects.
    max_address_space_mb: int = 0

    @classmethod
    def from_env(cls) -> "RerankConfig":
        def number(name: str, default: str, cast: Callable):
            try:
                return cast(os.environ.get(name, default))
            except (ValueError, TypeError):
                return cast(default)
        return cls(
            enabled=os.environ.get("DIC_STAGE4_RERANK_ENABLED", "0") == "1",
            model_dir=os.environ.get("DIC_STAGE4_RERANK_MODEL_DIR", "").strip(),
            max_candidates=number("DIC_STAGE4_RERANK_TOP_N", "12", int),
            batch_size=number("DIC_STAGE4_RERANK_BATCH_SIZE", "2", int),
            cpu_threads=number("DIC_STAGE4_RERANK_CPU_THREADS", "2", int),
            timeout_s=number("DIC_STAGE4_RERANK_TIMEOUT_S", "30", float),
        )

    def validate(self) -> None:
        if (not 2 <= self.max_candidates <= 30 or
                not 128 <= self.query_chars <= 1024 or
                not 128 <= self.text_chars <= 4096 or
                not 1 <= self.batch_size <= 8 or
                not 1 <= self.cpu_threads <= 8 or
                not math.isfinite(self.timeout_s) or not 1 <= self.timeout_s <= 120 or
                not 0 <= self.max_address_space_mb <= 65536):
            raise ValueError("Unbounded or invalid Stage 4 reranker configuration")


@dataclass(frozen=True)
class RerankStatus:
    state: str
    elapsed_ms: int
    candidates: int = 0


def _local_directory(path: str) -> str:
    candidate = Path(path).expanduser()
    if not path or len(path) > 2048 or candidate.is_symlink() or not candidate.is_dir():
        raise ValueError("Reranker model must be an existing local directory")
    return str(candidate.resolve(strict=True))


def _model_process(send, model_dir: str, pairs: list[tuple[str, str]], config: RerankConfig) -> None:
    """Run only in spawn child. Never imports a remote model in the caller."""
    try:
        os.environ["HF_HUB_OFFLINE"] = "1"
        os.environ["TRANSFORMERS_OFFLINE"] = "1"
        os.environ["OMP_NUM_THREADS"] = str(config.cpu_threads)
        os.environ["MKL_NUM_THREADS"] = str(config.cpu_threads)
        if config.max_address_space_mb and os.name == "posix":
            import resource
            limit = config.max_address_space_mb * 1024 * 1024
            resource.setrlimit(resource.RLIMIT_AS, (limit, limit))
        import torch
        torch.set_num_threads(config.cpu_threads)
        torch.set_num_interop_threads(1)
        from sentence_transformers import CrossEncoder
        model = CrossEncoder(
            model_dir, device="cpu", max_length=512,
            local_files_only=True, trust_remote_code=False,
        )
        values = model.predict(pairs, batch_size=config.batch_size,
                               show_progress_bar=False, num_workers=0)
        send.send(("ok", [float(v) for v in values]))
    except BaseException as exc:
        # Never send user content, model internals, or full exception messages.
        try:
            send.send(("error", type(exc).__name__))
        except (BrokenPipeError, OSError):
            pass
    finally:
        send.close()


def score_in_isolated_process(
    pairs: list[tuple[str, str]], config: RerankConfig
) -> list[float]:
    """Bounded CPU inference with a hard deadline; child is terminated on timeout."""
    config.validate()
    model_dir = _local_directory(config.model_dir)
    if not _PROCESS_SLOT.acquire(blocking=False):
        raise RuntimeError("RERANK_BUSY")
    process = None
    started = False
    receiver = None
    sender = None
    try:
        ctx = multiprocessing.get_context("spawn")
        receiver, sender = ctx.Pipe(duplex=False)
        process = ctx.Process(target=_model_process, args=(sender, model_dir, pairs, config), daemon=True)
        process.start()
        started = True
        sender.close()
        sender = None
        if not receiver.poll(config.timeout_s):
            raise TimeoutError("RERANK_HARD_TIMEOUT")
        try:
            state, result = receiver.recv()
        except EOFError as exc:
            raise RuntimeError("RERANK_WORKER_EXITED") from exc
        if state != "ok":
            raise RuntimeError("RERANK_WORKER_ERROR")
        return result
    finally:
        if process is not None:
            if started and process.is_alive():
                process.terminate()
            if started:
                process.join(timeout=1)
                if process.is_alive():
                    process.kill()
                    process.join(timeout=1)
            process.close()
        if receiver is not None:
            receiver.close()
        if sender is not None:
            sender.close()
        _PROCESS_SLOT.release()


def rerank_spec_chunks(
    query: str,
    items: Sequence["EvidenceItem"],
    *,
    config: RerankConfig | None = None,
    score_fn: Callable[[list[tuple[str, str]], RerankConfig], Sequence[float]] | None = None,
) -> tuple[list["EvidenceItem"], RerankStatus]:
    """Reorder verified primary-content RRF chunks; never upgrade evidence strength.

    If an inference error, timeout or invalid output occurs, returns exactly the
    original candidates. Non-RRF content and structured records are never scored.
    `score_fn` permits CPU-free deterministic unit testing; production uses spawn.
    """
    baseline = list(items)
    settings = config or RerankConfig.from_env()
    if not settings.enabled:
        return baseline, RerankStatus("DISABLED", 0)
    started = time.perf_counter()
    try:
        settings.validate()
        # Candidate pool is strictly limited BEFORE any cross-encoder invocation.
        selected = baseline[:settings.max_candidates]
        # Dense-only Qdrant payloads in the legacy index are NOT guaranteed
        # to be current PostgreSQL originals. Only SQL-backed or independently
        # SQL-hydrated Sparse records may enter the CrossEncoder.
        if (len(selected) < 2 or not query.strip() or
                any(item.source_type != "HYBRID_CHUNK" or _identity(item) is None
                    or item.metadata.get("ranking_only") is not True
                    or not ("POSTGRESQL" in item.metadata.get("retrieval_sources", ())
                            or ("QDRANT_SPARSE" in item.metadata.get("retrieval_sources", ())
                                and item.metadata.get("hydration") == "POSTGRES_HASH_AND_IDENTITY_VERIFIED"))
                    for item in selected)):
            return baseline, RerankStatus("NOT_ELIGIBLE", 0)
        # SQL/Hydrated originals only; never send metadata or QABUG to the scorer.
        question = query.strip()[:settings.query_chars]
        pairs = [(question, item.content[:settings.text_chars]) for item in selected]
        scorer = score_fn or score_in_isolated_process
        scores = list(scorer(pairs, settings))
        if (len(scores) != len(selected)
                or any(isinstance(s, bool) or not isinstance(s, (int, float))
                       or not math.isfinite(float(s)) for s in scores)):
            raise ValueError("RERANK_INVALID_SCORES")
        order = sorted(range(len(selected)), key=lambda i: (-float(scores[i]), i))
        reordered = []
        for rank, i in enumerate(order, 1):
            hit = selected[i]
            md = dict(hit.metadata)
            md.update({"rerank_method": "LOCAL_CROSS_ENCODER_CPU",
                       "rerank_rank": rank,
                       "rerank_original_rank": i + 1,
                       "reranking_only": True})
            reordered.append(replace(hit, metadata=md))
        return reordered + baseline[len(selected):], RerankStatus(
            "APPLIED", int((time.perf_counter() - started) * 1000), len(selected))
    except Exception as exc:
        state = "TIMEOUT" if isinstance(exc, TimeoutError) else (
            "BUSY" if str(exc) == "RERANK_BUSY" else "FAILED")
        return baseline, RerankStatus(state, int((time.perf_counter() - started) * 1000))
