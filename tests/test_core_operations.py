import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest
from fastapi.testclient import TestClient
from app.enterprise.core_operations import IngestionReceipt, recent_receipts
from app.server.api import create_app, MAX_BODY


@pytest.fixture
def receipt(tmp_path):
    result=IngestionReceipt(tmp_path,scope="21MM", files=[Path("source.pdf")],database="rag_21mm",collection="rag_21mm_current_chunks")
    result.file_done(0,success=True)
    return result


def finish(receipt, **overrides):
    service=Mock()
    service.sync.return_value=SimpleNamespace(status="READY",failed_count=0)
    validator=Mock()
    validator.validate.return_value=SimpleNamespace(status="PASS")
    arguments=dict(imported=1,failed=0,cancelled=False,service=service,validator=validator,cancel_check=lambda:False,progress=Mock())
    arguments.update(overrides)
    result=receipt.finish_vectors(**arguments)
    return result, arguments


def test_receipt_complete_requires_vector_and_quality(receipt):
    result,args=finish(receipt)
    assert result==("COMPLETED",0)
    assert args["service"].sync.call_args.kwargs["full_rebuild"] is False
    assert receipt.data["stages"]==dict(CONVERT_JSON_SQL="COMPLETED",VECTOR_SYNC="COMPLETED",QUALITY="COMPLETED")
    assert json.loads(receipt.path.read_text(encoding="utf-8"))["quality"]=="PASS"


@pytest.mark.parametrize("changes",[{"failed":1},{"imported":0}])
def test_partial_conversion_never_syncs(receipt,changes):
    result,args=finish(receipt,**changes)
    assert result==("PARTIAL",1)
    args["service"].sync.assert_not_called()
    args["validator"].validate.assert_not_called()


def test_cancel_before_vector_preserves_sql(receipt):
    result,args=finish(receipt,cancelled=True)
    assert result==("CANCELLED",0)
    args["service"].sync.assert_not_called()
    assert receipt.data["files"][0]["status"]=="COMPLETED"


def test_vector_failure_is_partial_redacted_and_retryable(receipt):
    service=Mock()
    service.sync.side_effect=RuntimeError("postgresql://user:secret@localhost")
    result,args=finish(receipt,service=service)
    assert result==("PARTIAL",1)
    assert receipt.data["retry"]=="VECTOR_SYNC_ONLY"
    assert "secret" not in receipt.path.read_text(encoding="utf-8")
    args["validator"].validate.assert_not_called()


def test_drift_is_not_completion(receipt):
    service=Mock()
    service.sync.return_value=SimpleNamespace(status="READY_WITH_DRIFT",failed_count=0)
    result,args=finish(receipt,service=service)
    assert result==("PARTIAL",1)
    args["validator"].validate.assert_not_called()


def test_quality_failure_never_requests_reconversion(receipt):
    validator=Mock()
    validator.validate.side_effect=RuntimeError("sensitive path")
    result,_=finish(receipt,validator=validator)
    assert result==("PARTIAL",1)
    assert receipt.data["retry"]=="QUALITY_CHECK_ONLY"
    assert receipt.data["stages"]["VECTOR_SYNC"]=="COMPLETED"


def test_quality_warn_does_not_claim_pass(receipt):
    validator=Mock()
    validator.validate.return_value=SimpleNamespace(status="WARN")
    result,_=finish(receipt,validator=validator)
    assert result==("PARTIAL",1)
    assert receipt.data["retry"]=="QUALITY_REVIEW"


def test_recent_receipts_never_assumes_running_is_alive(receipt):
    (receipt.directory/"broken.json").write_text("{",encoding="utf-8")
    rows=recent_receipts(receipt.directory)
    assert len(rows)==1
    assert rows[0]["display_status"]=="UNCONFIRMED_RUNNING"
    assert receipt.data["status"]=="RUNNING"


def test_receipt_failure_atomicity(receipt):
    before=receipt.path.read_bytes()
    receipt.data["quality"]="PASS"
    with patch("app.enterprise.core_operations.os.replace",side_effect=OSError("disk error")):
        with pytest.raises(OSError): receipt.save()
    assert receipt.path.read_bytes()==before
    assert len(recent_receipts(receipt.directory))==1


def test_explicit_scope_required(tmp_path):
    with pytest.raises(ValueError):
        IngestionReceipt(tmp_path,scope="AUTO",files=[],database="rag",collection="anything")


def test_skill_and_receipt_api_auth_and_no_runtime_load(tmp_path):
    runtime=Mock(config_dir=tmp_path)
    token="test"*12
    app=create_app(runtime,token,ragflow_compat=False)
    client=TestClient(app,base_url="http://127.0.0.1")
    auth={"Authorization":"Bearer "+token}
    assert client.get("/api/v1/skills").status_code==401
    skills=client.get("/api/v1/skills",headers=auth).json()
    assert len(skills["skills"])==7 and skills["database_write"] is False
    payload={"answer":"Timeout 500ms.[1]", "evidence":[{"id":"1","text":"Timeout 500ms"}]}
    response=client.post("/api/v1/skills/rag-evidence-verification/evaluate",headers=auth,json=payload)
    assert response.status_code==200
    assert response.json()["database_access"]=="NONE"
    assert client.post("/api/v1/skills/unknown/evaluate",headers=auth,json={}).status_code==400
    assert app.state.factual_admissions.snapshot()["active"]==0
    assert client.get("/api/v1/operations/receipts",headers=auth).json()["items"]==[]
    runtime.get_service.assert_not_called()


def test_skill_api_body_size_and_busy(tmp_path):
    runtime=Mock(config_dir=tmp_path)
    app=create_app(runtime,"a"*48)
    client=TestClient(app,base_url="http://127.0.0.1")
    auth={"Authorization":"Bearer "+"a"*48}
    path="/api/v1/skills/rag-evidence-verification/evaluate"
    assert client.post(path,headers=auth,content=iter([b"x"*MAX_BODY,b"x"])).status_code==413
    leases=[app.state.factual_admissions.acquire() for _ in range(2)]
    try:
        assert client.post(path,headers=auth,json={}).status_code==429
    finally:
        for lease in leases: lease.release()


def test_core_navigation_preserves_required_and_advanced_pages():
    from app.gui.core_workspace import CORE_GROUPS
    core={key for group,items in CORE_GROUPS if group!="高级工具" for key,_ in items}
    assert {"settings","document_import","database","qdrant","spec_compare","spec_relations","skills","core_home"}<=core
    assert "sql" not in core and "erd" not in core
    assert len([key for _,items in CORE_GROUPS for key,_ in items])==len({key for _,items in CORE_GROUPS for key,_ in items})


@pytest.mark.parametrize("conversion_fails",[False,True])
def test_real_worker_orchestration_keeps_sql_target_and_no_vector_on_failure(tmp_path,conversion_fails):
    import threading
    from app.gui.application import DocumentIngestionGUI, ProcessingOptions
    from app.config.config_loader import DatabaseConfig
    from app.storage.project_database_router import ProjectDatabaseRouter
    config=DatabaseConfig(enabled=True,host="127.0.0.1",port=6543,database="explicit_target",project_databases={},
                          user="tester",password="test-secret",password_env="NO_REAL_SECRET",connect_timeout=2)
    options=ProcessingOptions(output_directory=tmp_path,save_json=True,save_database=True,project_code="21MM",database_config=config)
    receipt=IngestionReceipt(tmp_path/"receipts",scope="21MM",files=[tmp_path/"doc.pdf"],database=config.database,collection="test")
    events=[]
    host=SimpleNamespace(cancel_requested=threading.Event(),failures=[],_emit=lambda key,value:events.append((key,value)))
    pipeline=Mock()
    pipeline.run.return_value=SimpleNamespace(metadata={},pages=[],chapters=[],sections=[],contents=[])
    if conversion_fails: pipeline.run.side_effect=RuntimeError("fixture conversion failed")
    service=Mock()
    service.sync.return_value=SimpleNamespace(status="READY",failed_count=0)
    validator=Mock()
    validator.validate.return_value=SimpleNamespace(status="PASS")
    with patch("app.gui.application.PipelineFactory.create",return_value=pipeline), \
         patch.object(ProjectDatabaseRouter,"set_runtime_override") as set_target, \
         patch.object(ProjectDatabaseRouter,"clear_runtime_override") as clear_target:
        DocumentIngestionGUI._process_files(host,files=[tmp_path/"doc.pdf"],options=options,
                                             vector_service=service,quality_validator=validator,receipt=receipt)
    assert set_target.call_args.args==("21MM",config)
    clear_target.assert_called_once_with("21MM")
    summary=[value for key,value in events if key=="finished"][0]
    assert summary.pipeline_status==("PARTIAL" if conversion_fails else "COMPLETED")
    if conversion_fails: service.sync.assert_not_called()
    else: service.sync.assert_called_once()
    assert "test-secret" not in receipt.path.read_text(encoding="utf-8")


def test_core_capture_matches_ai_embedding_and_exact_sql_target(tmp_path,monkeypatch):
    from app.gui.application import DocumentIngestionGUI, ProcessingOptions
    from app.config.config_loader import DatabaseConfig
    from app.ai.config import AISettings
    import app.gui.application as gui
    monkeypatch.setattr(gui,"USER_CONFIG_DIR",tmp_path)
    config=DatabaseConfig(enabled=True,host="127.0.0.1",port=6543,database="explicit_target",project_databases={},
                          user="tester",password="test-secret",password_env="NO_REAL_SECRET",connect_timeout=2)
    options=ProcessingOptions(output_directory=tmp_path,save_json=True,save_database=True,project_code="21MM",database_config=config)
    settings=AISettings(embedding_model="local/test-model",vector_dimension=7,collections={"21MM":"exact_collection"})
    host=SimpleNamespace(qdrant_url_var=Mock(get=Mock(return_value="http://127.0.0.1:6333")),
                         qdrant_timeout_var=Mock(get=Mock(return_value="2")),qdrant_api_key_var=Mock(get=Mock(return_value="")))
    with patch("app.ai.config.AISettingsStore.load",return_value=settings):
        service,validator,receipt=DocumentIngestionGUI._capture_spec_chain(host,options,[tmp_path/"doc.pdf"])
    assert service.postgres.database=="explicit_target" and service.postgres.port==6543
    assert service.embedding_model=="local/test-model" and service.vector_dimension==7
    assert service.collection_name=="exact_collection"
    assert receipt.data["database"]=="explicit_target"


@pytest.mark.parametrize("failed_rows",[0,1])
def test_qabug_core_worker_records_partial_rows_and_skips_vector(tmp_path,failed_rows):
    import threading
    from app.gui.application import DocumentIngestionGUI, ProcessingOptions
    from app.enterprise.profiles import PostgresProfile
    from app.qabug.qdrant_sync import QABUG_COLLECTION
    options=ProcessingOptions(output_directory=tmp_path,save_json=True,save_database=True,project_code="QABUG")
    pg=PostgresProfile(database="rag_qabug")
    receipt=IngestionReceipt(tmp_path/"receipts",scope="QABUG",files=[tmp_path/"issues.xlsx"],database=pg.database,collection=QABUG_COLLECTION)
    events=[]
    host=SimpleNamespace(cancel_requested=threading.Event(),failures=[],_emit=lambda key,value:events.append((key,value)))
    pipeline=Mock()
    pipeline.detect_record_type.return_value=("BUG",1.0,{"BUG":10,"QA":0})
    pipeline.export_jsonl.return_value=SimpleNamespace(jsonl_written=1)
    pipeline.import_postgres.return_value=SimpleNamespace(records_upserted=1,chunks_upserted=1,rows_quarantined=0,rows_failed=failed_rows)
    service=Mock()
    service.sync.return_value=SimpleNamespace(status="READY",failed_count=0)
    validator=Mock()
    validator.validate.return_value=SimpleNamespace(status="PASS")
    with patch("app.gui.application.QABugImportPipeline",return_value=pipeline), \
         patch("app.gui.application.QABugSchemaManager"),patch("app.gui.application.QABugQdrantManager"):
        DocumentIngestionGUI._process_qabug_files(host,files=[tmp_path/"issues.xlsx"],options=options,
              runtime=(pg,Mock(),"test-secret"),vector_service=service,quality_validator=validator,receipt=receipt)
    summary=[value for key,value in events if key=="finished"][0]
    assert summary.pipeline_status==("PARTIAL" if failed_rows else "COMPLETED")
    assert receipt.data["files"][0]["status"]==("FAILED" if failed_rows else "COMPLETED")
    if failed_rows: service.sync.assert_not_called()
    else: service.sync.assert_called_once()


def test_qabug_capture_keeps_fixed_domain_and_canonical_model(tmp_path,monkeypatch):
    from app.gui.application import DocumentIngestionGUI
    from app.ai.config import AISettings
    from app.enterprise.profiles import PostgresProfile,QdrantProfile
    from app.enterprise.qdrant_service import QdrantService
    import app.gui.application as gui
    monkeypatch.setattr(gui,"USER_CONFIG_DIR",tmp_path)
    settings=AISettings(embedding_model="local/test-model",vector_dimension=7)
    runtime=(PostgresProfile(database="rag_qabug"),QdrantService(QdrantProfile()),"test-secret")
    from threading import Event
    from types import SimpleNamespace
    cancellation=Event();gui_state=SimpleNamespace(cancel_requested=cancellation)
    with patch("app.ai.config.AISettingsStore.load",return_value=settings):
        service,validator,receipt=DocumentIngestionGUI._capture_qabug_chain(gui_state,runtime,[tmp_path/"issues.xlsx"])
    assert service.project_code=="QABUG" and service.collection_name=="rag_qabug_current_chunks"
    assert service.embedding_model=="local/test-model" and service.vector_dimension==7
    assert receipt.data["database"]=="rag_qabug"
    assert not validator.cancel_check()
    cancellation.set();assert validator.cancel_check()
