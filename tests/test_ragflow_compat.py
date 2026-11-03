"""Offline HTTP integration tests; fake runtime never connects to production."""
import unittest
from types import SimpleNamespace
from unittest.mock import Mock

from fastapi.testclient import TestClient
from app.ai.retrieval import EvidenceItem, RetrievalResult
from app.server.api import create_app, MAX_BODY

BASE = "/compat/ragflow/api/v1"
TOKEN = "test-only-token-" + "x" * 40


class AdapterTests(unittest.TestCase):
    def setUp(self):
        self.item = EvidenceItem(evidence_id="1", scope="21MM", source_type="SQL_EXACT",
                                 content="original engineering evidence", document_id="doc21",
                                 content_id=9, page_number=3, score=7.5)
        self.result = RetrievalResult(evidence=(self.item,), resolved_scopes=("21MM",), request_id="req")
        self.retriever = Mock()
        self.retriever.retrieve.return_value = self.result
        self.service = Mock(retriever=self.retriever)
        self.service.answer.return_value = ("native answer [1]", self.result, 2)
        self.runtime = Mock(preferred_model="local-model")
        self.runtime.local_status.return_value = {"configured_scopes": ["21MM", "QABUG"], "embedding": "READY"}
        self.runtime.get_service.return_value = self.service
        self.app = create_app(self.runtime, TOKEN, ragflow_compat=True)
        self.client = TestClient(self.app, base_url="http://127.0.0.1")
        self.headers = {"Authorization": "Bearer " + TOKEN}

    def post(self, path, body):
        return self.client.post(BASE + path, json=body, headers=self.headers)

    def template(self):
        return self.client.get(BASE+"/workflows/template?dataset_id=dic-21mm", headers=self.headers).json()["data"]

    def test_opt_in_and_original_endpoints(self):
        disabled = TestClient(create_app(self.runtime, TOKEN, ragflow_compat=False), base_url="http://127.0.0.1")
        self.assertEqual(disabled.get(BASE+"/datasets", headers=self.headers).status_code, 404)
        self.assertEqual(disabled.get("/v1/models", headers=self.headers).status_code, 200)
        self.retriever.retrieve_spec_lookup.return_value = self.result
        original = self.client.post("/api/v1/retrieval", headers=self.headers,
                                    json={"question": "spec", "scope": "21MM", "operation": "spec_lookup"})
        self.assertEqual(original.status_code, 200)
        self.assertIn("evidence", original.json())
        self.assertNotIn("code", original.json())

    def test_auth_host_and_shell(self):
        self.assertEqual(self.client.get(BASE+"/datasets").status_code, 401)
        self.assertEqual(self.client.get(BASE+"/datasets", headers={**self.headers,"Host":"evil.example"}).status_code,403)
        page=self.client.get("/compat/ragflow")
        self.assertEqual(page.status_code,200)
        self.assertNotIn(TOKEN,page.text)
        self.assertIn("frame-ancestors 'none'",page.headers["content-security-policy"])
        self.runtime.get_service.assert_not_called()

    def test_dataset_mapping_no_connection_or_fabricated_counts(self):
        data=self.client.get(BASE+"/datasets",headers=self.headers).json()["data"]
        self.assertEqual([d["id"] for d in data],["dic-21mm","dic-qabug"])
        self.assertIsNone(data[0]["dic"]["document_count"])
        self.runtime.get_service.assert_not_called()

    def test_retrieval_filters_cross_scope_dedup_and_document(self):
        wrong = EvidenceItem(evidence_id="2",scope="24MM",source_type="SQL_EXACT",content="must not return")
        self.retriever.retrieve.return_value=RetrievalResult(evidence=(self.item,self.item,wrong),resolved_scopes=("21MM",))
        r=self.post("/retrieval",{"question":"spec", "dataset_ids":["dic-21mm"],"document_ids":["doc21"]})
        data=r.json()["data"]
        self.assertEqual(data["total"],1)
        self.assertEqual(data["chunks"][0]["dic"]["page_number"],3)
        self.assertIsNone(data["chunks"][0]["similarity"])
        self.assertEqual(self.post("/retrieval",{"question":"spec","dataset_ids":["dic-21mm"],"document_ids":["absent"]}).json()["data"]["total"],0)

    def test_reject_unsupported_and_bad_input_before_service(self):
        for change in ({"dataset_ids":["dic-24mm"]},{"dataset_ids":["dic-21mm"]*2},
                       {"similarity_threshold":0.5},{"question":" "},{"page":0},{"page_size":True}):
            response=self.post("/retrieval",{"question":"spec","dataset_ids":["dic-21mm"],**change})
            self.assertIn(response.status_code,(400,422))
        self.runtime.get_service.assert_not_called()

    def test_retrieval_failure_redaction_and_release(self):
        self.retriever.retrieve.side_effect=RuntimeError("secret DSN")
        r=self.post("/retrieval",{"question":"spec","dataset_ids":["dic-21mm"]})
        self.assertEqual(r.status_code,503)
        self.assertNotIn("secret",r.text)
        self.assertEqual(self.app.state.factual_admissions.snapshot()["active"],0)

    def test_issue_dataset_uses_issue_lane_and_reports_degradation(self):
        item=EvidenceItem(evidence_id="qa1",scope="QABUG",source_type="ISSUE_EXACT",content="issue")
        self.retriever.retrieve_issue_register.return_value=RetrievalResult(
            evidence=(item,),resolved_scopes=("QABUG",),warnings=("semantic unavailable",))
        r=self.post("/retrieval",{"question":"issue", "dataset_ids":["dic-qabug"],"page_size":1})
        self.assertEqual(r.status_code,200)
        self.assertEqual(r.json()["data"]["dic"]["degraded_scopes"],["QABUG"])
        self.retriever.retrieve.assert_not_called()
        second=self.post("/retrieval",{"question":"issue", "dataset_ids":["dic-qabug"],"page":2,"page_size":1})
        self.assertEqual(second.json()["data"]["chunks"],[])
        self.assertEqual(second.json()["data"]["total"],1)

    def test_chat_native_chain_and_history(self):
        r=self.post("/chat/completions",{"question":"spec", "chat_id":"dic-21mm", "history":[{"role":"user","content":"previous"}]})
        self.assertEqual(r.status_code,200)
        self.assertEqual(r.json()["data"]["answer"],"native answer [1]")
        kwargs=self.service.answer.call_args.kwargs
        self.assertEqual(kwargs["scope"],"21MM")
        self.assertEqual(kwargs["model"],"local-model")
        self.assertEqual(self.service.answer.call_args.args[0][0]["content"],"previous")

    def test_chat_no_system_prompt_stream_session_override(self):
        for change in ({"stream":True},{"session_id":"x"},{"history":[{"role":"system","content":"override"}]}):
            self.assertEqual(self.post("/chat/completions",{"question":"spec","chat_id":"dic-21mm",**change}).status_code,422)
        self.runtime.get_service.assert_not_called()

    def test_chat_busy_and_failure_release(self):
        leases=[self.app.state.chat_admissions.acquire() for _ in range(2)]
        self.assertEqual(self.post("/chat/completions",{"question":"explain engineering behavior","chat_id":"dic-21mm"}).status_code,429)
        for lease in leases: lease.release()
        self.service.answer.side_effect=RuntimeError("secret")
        r=self.post("/chat/completions",{"question":"explain engineering behavior","chat_id":"dic-21mm"})
        self.assertEqual(r.status_code,503)
        self.assertNotIn("secret",r.text)
        self.assertEqual(self.app.state.chat_admissions.snapshot()["active"],0)

    def test_workflow_round_trip_rejects_arbitrary_code_and_upstream_dsl(self):
        w=self.template()
        self.assertEqual(self.post("/workflows/validate",w).json()["data"]["workflow"],w)
        self.runtime.get_service.assert_not_called()
        self.assertEqual(self.post("/workflows/validate",{**w,"steps":["http","sql","python"]}).status_code,400)
        self.assertEqual(self.post("/workflows/validate",{**w,"skills":["evil"]}).status_code,400)
        self.assertEqual(self.post("/workflows/validate",{**w,"components":{}}).status_code,422)
        self.assertEqual(self.post("/workflows/run",{"workflow":w,"question":"spec"}).status_code,200)

    def test_actual_body_bound_even_without_length(self):
        r=self.client.post(BASE+"/retrieval",headers=self.headers,content=iter([b"x"*MAX_BODY,b"x"]))
        self.assertEqual(r.status_code,413)
        self.runtime.get_service.assert_not_called()


if __name__ == "__main__":
    unittest.main()
