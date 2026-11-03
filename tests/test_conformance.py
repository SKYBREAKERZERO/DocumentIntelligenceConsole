import pytest
from unittest.mock import Mock
from app.ai.conformance import evaluate_rule,ConformanceService,parse_declared_observations

TEXT="The controller shall stop playback within 700 ms after ignition OFF, unless an emergency call is active."
RULE={"kind":"MAXIMUM","measurement_key":"latency_ms","conditions":{"ignition":"OFF","call":False},"anchor":"within 700 ms","unit":"ms","expected":700}

@pytest.mark.parametrize("value,status",[(600,"PASS"),(700,"PASS"),(701,"FAIL")])
def test_source_bound_numeric_boundary(value,status):
    assert evaluate_rule(RULE,TEXT,{"ignition":"OFF","call":False,"latency_ms":value})["status"]==status

def test_missing_exception_condition_never_defaults_to_false():
    assert evaluate_rule(RULE,TEXT,{"ignition":"OFF","latency_ms":600})["status"]=="UNKNOWN"
    assert evaluate_rule(RULE,TEXT,{"ignition":"OFF","call":True,"latency_ms":900})["status"]=="NOT_APPLICABLE"

@pytest.mark.parametrize("v",[True,"NaN","Infinity","1e99",-1])
def test_invalid_measurements(v):
    with pytest.raises(ValueError):evaluate_rule(RULE,TEXT,{"ignition":"OFF","call":False,"latency_ms":v})

@pytest.mark.parametrize("change",[{"expected":900},{"anchor":"within 800 ms"},{"unit":"s"},{"sql":"DELETE FROM contents"}])
def test_check_cannot_invent_limit_unit_or_executable_code(change):
    with pytest.raises(ValueError):evaluate_rule({**RULE,**change},TEXT,{"ignition":"OFF","call":False,"latency_ms":600})

def test_supported_range_and_state_are_literal_and_bounded():
    rule={"kind":"RANGE","measurement_key":"duration_ms","conditions":{},"anchor":"between 800 ms and 1500 ms","unit":"ms","expected":[800,1500]}
    assert evaluate_rule(rule,"Long press between 800 ms and 1500 ms shall be accepted",{"duration_ms":1000})["status"]=="PASS"
    state={"kind":"STATE_EQUALS","measurement_key":"mode","conditions":{},"anchor":"shall remain active","expected":"active"}
    assert evaluate_rule(state,"The audio mode shall remain active",{"mode":"active"})["status"]=="PASS"
    with pytest.raises(ValueError):evaluate_rule({**state,"anchor":"shall not remain active"},"The mode shall not remain active",{"mode":"active"})

def test_user_can_supply_observations_but_never_approval_authority():
    code,ctx,obs=parse_declared_observations('式样编号=990101；软件版本=S1；车型=""；地域=JP；latency_ms=600；call=false；approved=true')
    assert code=="990101" and ctx["model"]=="" and obs["call"] is False
    assert "approved" in obs  # unused observation; service authority is SQL-only
    r=Mock();result,_=ConformanceService(r).assess("21MM",code,{},obs)
    assert result["verdict"]=="UNDETERMINED" and result["mapping_verified"] is False
    r._connect.assert_not_called()

def test_conflicting_user_measurements_are_not_silently_overwritten():
    with pytest.raises(ValueError):parse_declared_observations('latency_ms=600；latency_ms=900')
    assert parse_declared_observations('call=false；call=false')[2]=={'call':False}


def test_proposals_validate_entire_rule_without_observations():
    from app.ai.conformance import validate_rule
    assert validate_rule(RULE,TEXT)==RULE
    for change in ({"kind":"PYTHON"},{"expected":900},{"conditions":{"x":None}},{"conditions":{"bad-key":"x"}}):
        with pytest.raises((ValueError,TypeError)):validate_rule({**RULE,**change},TEXT)
