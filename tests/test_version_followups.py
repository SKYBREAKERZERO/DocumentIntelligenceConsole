import pytest
from app.ai.agent import FastIntentRouter


@pytest.mark.parametrize("question", ["有哪些版本", "一共有几个版本", "列出全部版本", "版本有哪些", "v2.52和v2.51有什么变动？", "v2.51和v2.01有什么区别"])
def test_version_followups_keep_selected_spec(question):
    messages = [{"role":"user", "content":"查一下21MM的110式样书"},
                {"role":"assistant", "content":"508 unrelated assistant text"},
                {"role":"user", "content":question}]
    contextual = FastIntentRouter.contextualize(question, messages)
    assert "SPEC-110" in contextual
    assert FastIntentRouter.classify(contextual).route == "SPEC_VERSION_FACTS"


def test_explicit_version_list_routes_to_version_records():
    assert FastIntentRouter.classify("21MM 110式样书有哪些版本").route == "SPEC_VERSION_FACTS"
