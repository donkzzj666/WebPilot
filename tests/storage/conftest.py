from pathlib import Path
import pytest
from webagent.db import migrate
from webagent.db.repository import create_task, add_contract, create_run


def seed(db, task_id='task-1', run_id='run-1', version=1, parent=None):
    if not db.execute('SELECT 1 FROM tasks WHERE task_id=?', (task_id,)).fetchone():
        create_task(db, task_id=task_id, instruction='Synthetic storage test', requested_fields=['contract'])
    content = {'schema_version': 'm0-contract-v1', 'task_id': task_id, 'contract_version': version,
               'scenario': 'research', 'objective': '合成持久化测试', 'parameters': {'query': 'sqlite'},
               'sources': ['local-fixture'], 'action_policy': {'mode': 'read_only'}}
    if not db.execute('SELECT 1 FROM contracts WHERE task_id=? AND contract_version=?', (task_id,version)).fetchone():
        add_contract(db, content)
    create_run(db, run_id=run_id, task_id=task_id, contract_version=version,
               graph_version='graph-v1', graph_state_schema_version='state-v1',
               model_config_sha256='a'*64, runtime_config_sha256='b'*64, parent_run_id=parent)
    return content


@pytest.fixture
def database(tmp_path):
    path=tmp_path/'business.sqlite3'
    migrate(path)
    return path
