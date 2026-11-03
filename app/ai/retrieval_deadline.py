"""Per-request cooperative retrieval deadlines, isolated across worker threads."""
from contextlib import contextmanager
from contextvars import ContextVar
import time

_deadline=ContextVar("dic_retrieval_deadline",default=None)


def remaining_ms():
    end=_deadline.get()
    if end is None:return None
    remaining=int((end-time.monotonic())*1000)
    if remaining<=0:raise TimeoutError("Retrieval budget exhausted")
    return remaining


@contextmanager
def retrieval_budget(seconds=25):
    if not 1<=seconds<=120:raise ValueError("Retrieval budget out of bounds")
    current=_deadline.get()
    end=time.monotonic()+seconds
    token=_deadline.set(min(end,current) if current else end)
    try:yield
    finally:_deadline.reset(token)
