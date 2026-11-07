"""Data-only speed observations; no context, event bodies or UI dependencies."""

import time

FIELDS={'run_id','step','timestamp','elapsed','output_tokens','rate','model','agent_id','agent_name','purpose'}


def speed_snapshot(master, peers, *, started_at, live):
    samples=[{**item,'agent_id':'master','agent_name':'Master'} for item in master]+list(peers)
    return {'samples':[{key:value for key,value in item.items() if key in FIELDS} for item in samples],
            'started_at':started_at,'as_of':time.time(),'live':live,'scope':'session_lifetime',
            'rate_basis':'API completion_tokens / full call elapsed','smoothing_seconds':10}
