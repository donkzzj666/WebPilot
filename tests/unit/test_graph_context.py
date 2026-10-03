"""One current filtered observation plus trusted progress, without chat history."""
from copy import deepcopy
import pickle

import pytest
from pydantic import ValidationError

from webagent.db import connect
from webagent.errors import BusinessError
from webagent.graph.context import ModelContextBuilder
from webagent.graph.models import EphemeralOutput, GraphSnapshot, validate_graph_state
from webagent.graph.store import GraphStore
from webagent.models.schema import RequestInput
from storage.test_graph_store import check_error,setup_run


@pytest.mark.parametrize('scheduled',[False,True])
def test_context_reuses_exact_published_observation_and_filtered_checkpoint(tmp_path,scheduled):
    store,_,token,evidence,view,_=setup_run(tmp_path,scheduled=scheduled,text='Current page facts ' * 1200)
    builder=ModelContextBuilder(tmp_path,store=store,evidence=evidence)
    context=builder.build('run-1','snapshot-1',execution_token=token,expected_state_version=1)
    assert context.observation.model_dump(mode='json')==view
    assert len(context.observation.visible_excerpt)==16384
    assert context.verified_checkpoint.evidence_ids==view['evidence_ids']
    assert context.selected_flow_versions==[] and context.image_evidence_ids==[]
    assert builder.build('run-1','snapshot-1',execution_token=token)==context
    assert set(context.model_dump())=={'run_id','contract','observation','verified_checkpoint',
        'image_evidence_ids','allowed_action_schema_ref','selected_flow_versions'}


def test_context_requires_current_qualification_and_version(tmp_path):
    store,_,token,evidence,_,_=setup_run(tmp_path,scheduled=True)
    builder=ModelContextBuilder(tmp_path,store=store,evidence=evidence)
    check_error(lambda:builder.build('run-1','snapshot-1'),'RESOURCE_CONFLICT')
    check_error(lambda:builder.build('run-1','snapshot-1',execution_token=token,expected_state_version=0),'STATE_CONFLICT')


def test_context_rejects_missing_original_even_when_filtered_view_exists(tmp_path):
    store,_,_,evidence,view,_=setup_run(tmp_path)
    item=evidence.store.metadata(view['evidence_ids'][0])
    original=evidence.store.metadata(item['original_evidence_id'])
    (tmp_path/original['artifact_path']).unlink()
    check_error(lambda:ModelContextBuilder(tmp_path,store=store,evidence=evidence).build('run-1','snapshot-1'),'INPUT_BLOCKED')


def test_context_does_not_import_old_observations_or_executor_history(tmp_path):
    store,_,_,evidence,view,_=setup_run(tmp_path,text='Current facts')
    old=deepcopy(view)
    old.update(snapshot_id='snapshot-old',captured_at='2026-09-29T00:00:00Z')
    evidence.publish_observation(old,dict(title='Earlier page',text='old message history'))
    context=ModelContextBuilder(tmp_path).build('run-1','snapshot-1')
    assert context.observation.visible_excerpt=='Current facts'
    assert 'old message history' not in context.model_dump_json()
    with connect(store.path) as db:
        assert db.execute('SELECT count(*) FROM filtered_observations').fetchone()[0]==2


@pytest.mark.parametrize('unexpected',['model_output','action','execution_token','api_key','messages','raw_text'])
def test_graph_state_rejects_runtime_or_sensitive_objects(unexpected):
    state=GraphSnapshot(run_id='run-1',contract_version=1,state_version=1,business_event_id=1).state()
    state[unexpected]='Forbidden persisted content'
    with pytest.raises(ValidationError):
        validate_graph_state(state)
    check_error(lambda:GraphStore.validate_state(state),'STATE_CONFLICT')


@pytest.mark.parametrize('field,value',[('graph_version','new-graph'),('state_schema_version','new-schema'),
    ('route','arbitrary-tool'),('diagnostic','free form secret'),('iteration',True),('state_version',-1)])
def test_graph_state_strict_version_phase_and_counter_validation(field,value):
    state=GraphSnapshot(run_id='run-1',contract_version=1,state_version=1,business_event_id=1).state()
    state[field]=value
    with pytest.raises(ValidationError):
        validate_graph_state(state)


def test_ephemeral_model_output_is_consumed_once_and_never_pickled():
    slot=EphemeralOutput()
    output=RequestInput(type='RequestInput',requested_fields=['period'],reason='Explicit period required')
    slot.put(output)
    with pytest.raises(RuntimeError):slot.put(output)
    with pytest.raises(TypeError):pickle.dumps(slot)
    assert slot.take()==output
    with pytest.raises(RuntimeError):slot.take()
    slot.put(output);slot.clear()
    with pytest.raises(RuntimeError):slot.take()
