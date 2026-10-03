"""Real SQLite claims, stale execution rejection and durable recovery holds."""
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
import multiprocessing
import sqlite3
from uuid import uuid4

import pytest

from webagent.db import connect, migrate, transaction
from webagent.db.migrations import MigrationError
from webagent.errors import BusinessError
from webagent.scheduler.models import Resource
from webagent.scheduler.store import SchedulerStore
from conftest import seed


class Clock:
    def __init__(self):
        self.now = datetime.now(timezone.utc)
    def __call__(self):
        return self.now
    def advance(self, seconds=31):
        self.now += timedelta(seconds=seconds)


def run(path, name, identity=None, repository=None, site='github'):
    with connect(path) as db, transaction(db):
        seed(db, task_id='task-'+name, run_id=name)
    resources = [Resource.site_identity(site, identity or name),Resource.browser_context(name)]
    if repository:
        resources.append(Resource.repository_write(repository))
    return resources


def queued(path, store, name, **kwargs):
    resources=run(path,name,**kwargs)
    store.enqueue(name,resources,expected_state_version=0)
    return resources


def worker(store, name='worker'):
    return store.start_worker(name)


def assert_stale(store, token):
    with pytest.raises(BusinessError) as failed:
        store.validate(token)
    assert failed.value.code=='RESOURCE_CONFLICT'


def checkpoint(path,store,token,**overrides):
    """Persist real immutable business progress, without graph/budget execution."""
    from webagent.db.repository import utc_text
    if isinstance(store.clock,Clock):
        # SQL state triggers use wall time; the injected expiry clock must be
        # advanced to that same point before persisting a checkpoint receipt.
        store.clock.now=max(store.clock.now,datetime.now(timezone.utc))
    with connect(path) as db,transaction(db):
        run=db.execute('SELECT * FROM runs WHERE run_id=?',(token.run_id,)).fetchone()
        event=db.execute("SELECT event_id FROM task_events WHERE run_id=? AND event_type='state_changed' ORDER BY event_id DESC LIMIT 1",(token.run_id,)).fetchone()[0]
        if db.execute('SELECT 1 FROM run_budgets WHERE run_id=?',(token.run_id,)).fetchone() is None:
            db.execute('INSERT INTO run_budgets(budget_record_id,run_id) VALUES(?,?)',('budget-'+token.run_id,token.run_id))
        values=dict(checkpoint_id='checkpoint-'+uuid4().hex,task_id=run['task_id'],run_id=token.run_id,
                    contract_version=run['contract_version'],current_subgoal='resource-discovery-safe-point',
                    current_object_id='synthetic-object',action_sequence=0,business_event_id=event,
                    budget_record_ref=db.execute('SELECT budget_record_id FROM run_budgets WHERE run_id=?',(token.run_id,)).fetchone()[0],epoch=token.epoch,saved_at=utc_text(store.clock()))
        values.update(overrides)
        db.execute('INSERT INTO run_checkpoints('+','.join(values)+') VALUES('+','.join('?' for _ in values)+')',tuple(values.values()))
    return values['checkpoint_id']


def test_two_slots_third_queued_and_resources_are_acquired_as_a_complete_set(database):
    store=SchedulerStore(database); generation=worker(store)
    for name in ('first','second','third'):
        queued(database,store,name)
    first,second=store.claim('worker',generation),store.claim('worker',generation)
    assert first.run_id=='first' and second.run_id=='second'
    assert store.claim('worker',generation) is None
    snapshot=store.snapshot()
    assert [q['status'] for q in snapshot['queue']]==['ACTIVE','ACTIVE','QUEUED']
    assert not any(x['holder_run_id']=='third' for x in snapshot['leases'])
    assert len(snapshot['context_reservations'])==2
    assert len([x for x in snapshot['leases'] if x['resource_type']=='active_slot'])==2
    store.finish(first)
    assert store.claim('worker',generation).run_id=='third'


@pytest.mark.parametrize('shared', ['identity','repository','environment'])
def test_resource_conflict_never_leaves_a_partial_claim(database, shared):
    store=SchedulerStore(database);generation=worker(store)
    a=run(database,'a',identity='same' if shared=='identity' else 'a',repository='Owner/Repo' if shared=='repository' else None)
    b=run(database,'b',identity='same' if shared=='identity' else 'b',repository='https://github.com/owner/repo.git' if shared=='repository' else None,site='github_editor')
    if shared=='environment':
        a.append(Resource.webarena_environment());b.append(Resource.webarena_environment())
    for name,resources in [('a',a),('b',b)]:
        store.enqueue(name,resources,expected_state_version=0)
    assert store.claim('worker',generation).run_id=='a'
    assert store.claim('worker',generation) is None
    assert not any(x['holder_run_id']=='b' for x in store.snapshot()['leases'])
    assert store.snapshot()['queue'][1]['reason']=='resource_conflict'


def test_claim_and_state_event_commit_together_and_cas_rejects_external_transition(database):
    store=SchedulerStore(database); generation=worker(store)
    resources=queued(database,store,'r')
    assert store.enqueue('r',resources,expected_state_version=0)['status']=='QUEUED'
    token=store.claim('worker',generation)
    with connect(database) as db:
        assert db.execute("SELECT count(*) FROM task_events WHERE run_id='r' AND event_type='state_changed'").fetchone()[0]==1
        assert db.execute("SELECT state_version FROM runs WHERE run_id='r'").fetchone()[0]==token.state_version==1
    with pytest.raises(BusinessError):
        store.enqueue('r',resources,expected_state_version=0)
    from webagent.state import transition
    transition(database,run_id='r',expected_state_version=1,target='VERIFYING')
    assert_stale(store,token)


def test_heartbeat_fresh_db_expiry_and_wrong_worker_epoch_scope(database):
    from dataclasses import replace
    clock=Clock();store=SchedulerStore(database,clock=clock);generation=worker(store)
    queued(database,store,'r')
    token=store.claim('worker',generation)
    clock.advance(10)
    renewed=store.heartbeat(token)
    assert renewed.expires_at>token.expires_at and renewed.epoch==token.epoch
    assert renewed.state_version==token.state_version
    clock.advance(21)
    assert store.validate(token)['status']=='ACTIVE' # persisted renewal wins over cached timestamp.
    for changed in (replace(token,epoch=token.epoch+1),replace(token,worker_id='wrong'),replace(token,worker_generation=generation+1),replace(token,state_version=0)):
        assert_stale(store,changed)
    with pytest.raises(BusinessError):
        store.validate(token,resource_key=Resource.repository_write('other/repo').resource_key)
    clock.advance(10)
    assert_stale(store,token)
    assert store.sweep_expired()==1
    assert store.sweep_expired()==0
    snapshot=store.snapshot()
    assert snapshot['queue'][0]['status']=='RECOVERY' and snapshot['queue'][0]['state']=='RECONCILING'
    assert not any(x['resource_type']=='active_slot' for x in snapshot['leases'])
    assert all(x['logical_hold']==1 and x['control_owner']=='none' for x in snapshot['leases'])


def test_worker_generation_recovery_prevents_old_executor_and_expiry_does_not_steal_identity(database):
    store=SchedulerStore(database); generation=worker(store)
    queued(database,store,'a',identity='shared');queued(database,store,'b',identity='shared')
    first=store.claim('worker',generation)
    next_generation=worker(store)
    assert next_generation>generation
    assert_stale(store,first)
    recovered=store.claim('worker',next_generation)
    assert recovered.run_id=='a' and recovered.epoch>first.epoch
    assert_stale(store,recovered)
    assert store.validate(recovered,allow_reconciling=True)['state']=='RECONCILING'
    assert store.claim('worker',next_generation) is None
    continued=store.reconcile('a',recovered.state_version)
    assert store.validate(continued)['state']=='RUNNING'
    assert_stale(store,recovered)
    store.finish(continued)
    assert store.claim('worker',next_generation).run_id=='b'


@pytest.mark.parametrize('target', ['PAUSED','WAITING_CI','WAITING_SITE','WAITING_HANDOFF'])
def test_wait_releases_active_slot_and_preserves_identity_context_control(database,target):
    clock=Clock();store=SchedulerStore(database,clock=clock);generation=worker(store)
    queued(database,store,'a',identity='same');queued(database,store,'b',identity='same');queued(database,store,'c',identity='other')
    token=store.claim('worker',generation)
    args={'handoff_deadline':clock()+timedelta(hours=1),'control_owner':'human'} if target=='WAITING_HANDOFF' else {}
    waiting=store.defer(token,target,**args)
    assert waiting['state']==target and waiting['status']=='WAITING'
    assert_stale(store,token)
    leases=[x for x in store.snapshot()['leases'] if x['holder_run_id']=='a']
    assert len(leases)==2 and all(x['logical_hold']==1 for x in leases)
    assert all(x['control_owner']==('human' if target=='WAITING_HANDOFF' else 'worker') for x in leases)
    assert store.claim('worker',generation).run_id=='c'
    resumed=store.resume('a',waiting['run_state_version'])
    assert resumed['state']=='RECONCILING' and resumed['status']=='RECOVERY'
    recovery=store.claim('worker',generation)
    assert recovery.run_id=='a' and recovery.epoch>token.epoch
    assert_stale(store,recovery)


def test_scope_expansion_requeues_full_set_and_releases_safe_conflicting_resources(database):
    store=SchedulerStore(database);generation=worker(store)
    a=queued(database,store,'a',identity='a');b=queued(database,store,'b',identity='b')
    first,second=store.claim('worker',generation),store.claim('worker',generation)
    first_checkpoint=checkpoint(database,store,first)
    store.expand(first,[b[0]],checkpoint_id=first_checkpoint)
    assert_stale(store,first)
    leases=[x for x in store.snapshot()['leases'] if x['holder_run_id']=='a']
    assert [x['resource_type'] for x in leases]==['browser_context']
    # B can now ask for A without forming a hold-A / wait-B cycle.
    store.expand(second,[a[0]],checkpoint_id=checkpoint(database,store,second))
    recovery=store.claim('worker',generation)
    assert recovery.run_id=='a'
    assert {a[0].resource_key,b[0].resource_key}.issubset(recovery.resources)
    assert store.claim('worker',generation) is None
    with connect(database) as db:
        assert db.execute('SELECT current_subgoal FROM run_checkpoints WHERE checkpoint_id=?',(first_checkpoint,)).fetchone()[0]=='resource-discovery-safe-point'


@pytest.mark.parametrize('invalid', [None,'',123,'missing-checkpoint'])
def test_expansion_requires_an_existing_checkpoint_before_any_resource_release(database,invalid):
    store=SchedulerStore(database);generation=worker(store);queued(database,store,'a')
    token=store.claim('worker',generation);before=store.snapshot()
    with pytest.raises(BusinessError) as refused:
        store.expand(token,[Resource.site_identity('new-site','a')],checkpoint_id=invalid)
    assert refused.value.code in ('INVALID_PARAMETER','STATE_CONFLICT')
    assert store.snapshot()==before
    assert store.validate(token)['status']=='ACTIVE'
    with pytest.raises(TypeError):
        store.expand(token,[Resource.site_identity('new-site','a')])


@pytest.mark.parametrize('stale', ['other_run','old_epoch','old_event','old_time','future_time','old_progress'])
def test_expansion_checkpoint_must_match_epoch_latest_state_event_time_and_step_progress(database,stale):
    from webagent.db.repository import utc_text
    clock=Clock();store=SchedulerStore(database,clock=clock);generation=worker(store)
    queued(database,store,'a');queued(database,store,'b')
    first,second=store.claim('worker',generation),store.claim('worker',generation)
    if stale=='other_run':
        saved=checkpoint(database,store,second)
    else:
        if stale=='old_epoch':
            # Save progress under the first valid qualification, then advance
            # the same Run's epoch through a real wait/recovery/claim cycle.
            saved=checkpoint(database,store,first)
            waiting=store.defer(first,'PAUSED');store.resume('a',waiting['run_state_version'])
            first=store.claim('worker',generation)
        elif stale=='old_event':
            from webagent.state import transition
            saved=checkpoint(database,store,first)
            transition(database,run_id='a',expected_state_version=first.state_version,target='VERIFYING')
            # A new valid qualification binds the newer state; the checkpoint
            # still references the old RUNNING state event.
            store.abandon(first);first=store.claim('worker',generation)
            saved=checkpoint(database,store,first,business_event_id=_checkpoint_event(database,saved))
        elif stale=='old_time':
            saved=checkpoint(database,store,first,saved_at=utc_text(clock()-timedelta(seconds=1)))
        elif stale=='future_time':
            saved=checkpoint(database,store,first,saved_at=utc_text(clock()+timedelta(seconds=1)))
        else:
            with connect(database) as db,transaction(db):
                db.execute('''INSERT INTO observations(snapshot_id,run_id,captured_at,source_url,title,tab_id,frame_id,page_version,
                    width,height,visible_excerpt,redaction_status) VALUES('progress','a',?,'https://fixture.invalid','synthetic','tab','frame','v1',800,600,'','FILTERED')''',(utc_text(),))
                db.execute("INSERT INTO steps(step_id,run_id,sequence,step_kind,epoch,input_snapshot_id,started_at) VALUES('decision','a',1,'decision',?,'progress',?)",(first.epoch,utc_text()))
            saved=checkpoint(database,store,first,action_sequence=0)
    before=store.snapshot()
    with pytest.raises(BusinessError) as refused:
        store.expand(first,[Resource.site_identity('new-site','a')],checkpoint_id=saved)
    assert refused.value.code=='STATE_CONFLICT' and refused.value.field=='checkpoint_id'
    assert store.snapshot()==before
    assert store.validate(first,allow_reconciling=True)['status']=='ACTIVE'
    with connect(database) as db:
        assert db.execute('SELECT 1 FROM run_checkpoints WHERE checkpoint_id=?',(saved,)).fetchone()


def _checkpoint_event(path,checkpoint_id):
    with connect(path) as db:
        return db.execute('SELECT business_event_id FROM run_checkpoints WHERE checkpoint_id=?',(checkpoint_id,)).fetchone()[0]


def test_expansion_failure_rolls_back_resource_scope_state_events_leases_and_retains_checkpoint(database):
    store=SchedulerStore(database);generation=worker(store);queued(database,store,'a')
    token=store.claim('worker',generation);saved=checkpoint(database,store,token);before=store.snapshot()
    with connect(database) as db:
        count=db.execute('SELECT count(*) FROM task_events').fetchone()[0]
        requirements=[tuple(row) for row in db.execute('SELECT * FROM scheduler_requirements')]
        db.execute("CREATE TRIGGER fixture_abort_expansion BEFORE UPDATE ON scheduler_queue WHEN NEW.reason='scope_expanded' BEGIN SELECT RAISE(ABORT,'synthetic commit failure'); END")
    with pytest.raises(sqlite3.IntegrityError):
        store.expand(token,[Resource.site_identity('new-site','a')],checkpoint_id=saved)
    assert store.snapshot()==before
    with connect(database) as db:
        assert db.execute('SELECT count(*) FROM task_events').fetchone()[0]==count
        assert [tuple(row) for row in db.execute('SELECT * FROM scheduler_requirements')]==requirements
        assert db.execute('SELECT 1 FROM run_checkpoints WHERE checkpoint_id=?',(saved,)).fetchone()
        db.execute('DROP TRIGGER fixture_abort_expansion')
    assert store.validate(token)['status']=='ACTIVE'


def unresolved(path,run_id,key):
    from webagent.db.repository import utc_text
    now=utc_text()
    with connect(path) as db,transaction(db):
        db.execute('''INSERT INTO write_intents(operation_id,business_key,task_id,originating_run_id,target,expected_change,
         identity_ref,precondition_version,status,created_at,updated_at) VALUES('op','business',?,?,?,'change','identity','v1','UNKNOWN',?,?)''', ('task-'+run_id,run_id,key,now,now))
        db.execute('INSERT INTO resource_quarantines VALUES(?,?,?)',(key,'op',now))


def test_unknown_quarantine_survives_expansion_cancellation_and_expiry(database):
    clock=Clock();store=SchedulerStore(database,clock=clock);generation=worker(store)
    resources=queued(database,store,'a',identity='same',repository='owner/repo')
    queued(database,store,'b',identity='other',repository='owner/repo')
    token=store.claim('worker',generation)
    unresolved(database,'a',resources[-1].resource_key)
    assert_stale(store,token)
    saved=checkpoint(database,store,token)
    store.expand(token,[Resource.site_identity('another','a')],checkpoint_id=saved)
    clock.advance();generation=worker(store)
    recovered=store.claim('worker',generation)
    assert recovered.run_id=='a'
    with pytest.raises(BusinessError):
        store.reconcile('a',recovered.state_version)
    store.finish(recovered)
    assert store.claim('worker',generation) is None
    with connect(database) as db:
        assert db.execute('SELECT count(*) FROM resource_quarantines').fetchone()[0]==1
        assert db.execute('SELECT count(*) FROM resource_leases WHERE resource_key=?',(resources[-1].resource_key,)).fetchone()[0]==1
        assert db.execute('SELECT epoch FROM run_checkpoints WHERE checkpoint_id=?',(saved,)).fetchone()[0]==token.epoch


def test_abandon_is_idempotent_even_expired_and_does_not_cancel_unknown_work(database):
    clock=Clock();store=SchedulerStore(database,clock=clock);generation=worker(store)
    queued(database,store,'a');token=store.claim('worker',generation);clock.advance()
    recovered=store.abandon(token)
    assert recovered['state']=='RECONCILING' and recovered['reason']=='executor_interrupted'
    assert store.abandon(token)['revision']==recovered['revision']
    assert_stale(store,token)


def _claim_process(path,generation,output):
    try:
        token=SchedulerStore(path).claim('worker',generation)
        output.put(token.run_id if token else None)
    except BaseException as error:
        output.put(type(error).__name__)


def test_real_multiprocess_claim_has_one_executor_per_run_and_two_global_slots(database):
    store=SchedulerStore(database);generation=worker(store)
    for name in ('a','b','c','d'):
        queued(database,store,name)
    context=multiprocessing.get_context('spawn');output=context.Queue()
    processes=[context.Process(target=_claim_process,args=(database,generation,output)) for _ in range(4)]
    for process in processes:process.start()
    for process in processes:
        process.join(20);assert process.exitcode==0
    results=[output.get(timeout=5) for _ in processes]
    assert sorted(item for item in results if item is not None)==['a','b']
    assert results.count(None)==2
    assert len([q for q in store.snapshot()['queue'] if q['status']=='ACTIVE'])==2


def test_due_monitoring_priority_and_future_queue_item_persists(database):
    clock=Clock();store=SchedulerStore(database,clock=clock);generation=worker(store)
    for name in ('ordinary','monitor','future'):
        resources=run(database,name)
        store.enqueue(name,resources,expected_state_version=0,queue_class='monitoring' if name!='ordinary' else 'ordinary',
                      available_at=clock()+timedelta(seconds=60) if name=='future' else None)
    assert store.claim('worker',generation).run_id=='monitor'
    assert store.claim('worker',generation).run_id=='ordinary'
    assert store.snapshot()['queue'][2]['status']=='QUEUED'


def test_atomic_claim_hook_failure_rolls_back_state_lease_context_and_event(database):
    store=SchedulerStore(database);generation=worker(store);queued(database,store,'a')
    def reject(db,row):
        db.execute("UPDATE tasks SET state_version=state_version+1 WHERE task_id='task-a'")
        raise ValueError('synthetic budget rejection')
    with pytest.raises(ValueError):store.claim('worker',generation,before_claim=reject)
    snapshot=store.snapshot()
    assert snapshot['queue'][0]['state']=='QUEUED' and not snapshot['leases'] and not snapshot['context_reservations']
    with connect(database) as db:
        assert db.execute("SELECT state_version FROM tasks WHERE task_id='task-a'").fetchone()[0]==0
        assert db.execute("SELECT count(*) FROM task_events WHERE event_type='state_changed'").fetchone()[0]==0


def test_v9_upgrade_keeps_existing_history_and_failure_rolls_back_all_v10(tmp_path,monkeypatch):
    from webagent.db import migrations
    path=tmp_path/'v9.sqlite3';migrate(path,target=9)
    with connect(path) as db,transaction(db):seed(db)
    with connect(path) as db:before=[tuple(row) for row in db.execute('SELECT * FROM runs')]
    source=(migrations.SQL_DIR/'0010_scheduler.sql').read_text()
    directory=tmp_path/'sql';directory.mkdir()
    for name in migrations.MIGRATIONS:
        (directory/name).write_text((migrations.SQL_DIR/name).read_text())
    (directory/'0010_scheduler.sql').write_text(source+'\nCREATE TABLE scheduler_queue(broken TEXT);\n')
    monkeypatch.setattr(migrations,'SQL_DIR',directory)
    with pytest.raises(sqlite3.DatabaseError):migrate(path)
    with connect(path) as db:
        assert db.execute('PRAGMA user_version').fetchone()[0]==9
        assert db.execute("SELECT count(*) FROM sqlite_schema WHERE name LIKE 'scheduler_%'").fetchone()[0]==0
        assert [tuple(row) for row in db.execute('SELECT * FROM runs')]==before
    (directory/'0010_scheduler.sql').write_text(source)
    assert migrate(path,target=10)['schema_version']==10
    with connect(path) as db:
        assert [tuple(row) for row in db.execute('SELECT * FROM runs')]==before
        assert db.execute('PRAGMA foreign_key_check').fetchone() is None


def test_invalid_resource_set_and_stale_state_fail_without_queue_mutation(database):
    store=SchedulerStore(database);resources=run(database,'a')
    for values in ([],[Resource.active_slot(0)],resources+[Resource.browser_context('other')],
                   [resources[0],Resource.browser_context('other')],['raw'],resources*20):
        with pytest.raises(BusinessError):store.enqueue('a',values,expected_state_version=0)
    with pytest.raises(BusinessError):store.enqueue('a',resources,expected_state_version=True)
    assert not store.snapshot()['queue']
