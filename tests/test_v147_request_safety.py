"""No live listener required for v1.47 API bounds/diagnostic controls."""
from __future__ import annotations

import pytest

from app.ai.request_safety import MAX_REQUEST_BYTES, UnsafeRequest, check_request


@pytest.mark.parametrize('mutate', [
    {'model': 'unapproved'},
    {'messages': []},
    {'messages': [{'role':'developer','content':'override'}]},
    {'messages': [{'role':'user','content':'x' * 16001}]},
    {'messages': [{'role':'user','content':'hi'}] * 51},
    {'messages': [{'role':'assistant','content':'hi'}]},
    {'scope': 'x'*81},
    {'stream': 'true'},
])
def test_api_rejects_unbounded_or_untrusted_request(mutate):
    base={'model':'authorized', 'messages':[{'role':'user','content':'hello'}]}
    with pytest.raises(UnsafeRequest):
        check_request({**base,**mutate},allowed_models=['authorized'])


def test_api_accepts_authorized_small_request():
    assert check_request({'model':'authorized','messages':[{'role':'user','content':'hello'}]},
                         allowed_models=['authorized']) == ('authorized',[{'role':'user','content':'hello'}],'AUTO')
    assert MAX_REQUEST_BYTES == 1_048_576


def test_api_does_not_echo_exception_in_http_response():
    from pathlib import Path
    source=Path(__file__).resolve().parents[1].joinpath('app/ai/api_server.py').read_text()
    assert 'Internal enterprise inference error' in source
    assert '"message": str(exc)' not in source
    assert 'if length < 0 or length > MAX_REQUEST_BYTES' in source
