"""Session-wide read-only execution projection."""
from copy import deepcopy

def visible_executions(pools, agent_id, limit=100):
    """The same bounded execution projection for live and saved Sessions."""
    records = [deepcopy(record) for pool in pools for record in pool['executions'].values()]
    return {'agent_id': agent_id, 'executions': sorted(records, key=lambda record: record.get('created_at', 0), reverse=True)[:limit]}
