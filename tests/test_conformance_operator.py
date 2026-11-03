import pytest
from scripts.manage_conformance import proposal_shape,Operator

MAPPING={'issue_version_field':'software_version','software_version':'SYN-SW','model':'M','destination':'JP',
         'spec_scope':'21MM','spec_code':'990101','spec_version_id':1}

def test_mapping_cannot_self_declare_authority_or_choose_ambiguous_scope():
    assert proposal_shape(MAPPING,'MAPPING')==MAPPING
    for change in ({'status':'APPROVED'},{'spec_scope':'AUTO'},{'model':''},{'spec_version_id':True},{'spec_code':'x;DROP'}):
        with pytest.raises(ValueError):proposal_shape({**MAPPING,**change},'MAPPING')

def test_check_proposal_accepts_only_source_identity_and_finite_rule():
    proposal={'mapping_id':1,'content_id':2,'rule_json':{}}
    assert proposal_shape(proposal,'CHECK')==proposal
    with pytest.raises(ValueError):proposal_shape({**proposal,'content_hash':'invented'},'CHECK')

def test_dry_run_never_opens_writer_connection():
    operator=Operator({},'unused')
    operator.bind=lambda data,kind:({'bound':True},{'content':'synthetic'})
    operator.connect=lambda *a,**k:pytest.fail('Dry run must not open a writer connection')
    result=operator.propose({},'CHECK')
    assert result['mode']=='DRY_RUN' and result['approved'] is False
