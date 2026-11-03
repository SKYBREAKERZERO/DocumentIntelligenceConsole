from types import SimpleNamespace
from fastapi.testclient import TestClient
from app.server.api import create_app, MODEL_ID
from app.ai.retrieval import RetrievalResult
from app.ai.agent import FastIntentRouter


def client_and_history():
    received = []
    def answer(messages, **kwargs):
        received.extend(messages)
        return '引用结果', RetrievalResult(evidence=(), resolved_scopes=('21MM',), route='SPEC_RELATIONS'), 0
    runtime = SimpleNamespace(preferred_model='test', get_service=lambda:SimpleNamespace(answer=answer))
    client = TestClient(create_app(runtime, 't'*48), base_url='http://127.0.0.1')
    return client, received


def test_more_than_40_messages_reaches_service_and_preserves_active_spec():
    client, received = client_and_history()
    messages = [{'role':'user' if n%2 == 0 else 'assistant', 'content':'继续'} for n in range(40)]
    messages.extend([{'role':'user', 'content':'查一下21MM的508式样书'},
                     {'role':'user', 'content':'当前式样引用了哪些其他式样？'}])
    response = client.post('/v1/chat/completions', headers={'Authorization':'Bearer '+'t'*48},
                           json={'model':MODEL_ID, 'messages':messages})
    assert response.status_code == 200
    assert len(received) == 42
    contextual = FastIntentRouter.contextualize(received[-1]['content'], received)
    assert 'SPEC-508' in contextual
    assert FastIntentRouter.classify(contextual).route == 'SPEC_RELATIONS'


def test_validation_error_does_not_echo_private_conversation():
    client, _ = client_and_history()
    response = client.post('/v1/chat/completions', headers={'Authorization':'Bearer '+'t'*48},
                           json={'model':MODEL_ID, 'messages':{'private':'PRIVATE_TICKET_BODY'}})
    assert response.status_code == 422
    assert 'PRIVATE_TICKET_BODY' not in response.text
    assert 'input' not in response.json()['detail'][0]
