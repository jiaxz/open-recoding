"""Public schema for Session resource inspection."""
from multiagent.tools import tool

POOL_STATUS=tool('pool_status','Inspect the Session-wide Bash/Python pool and execution environment. Returns current bounded process/execution metadata, creators, owners, states and counts; never namespace values, code, model context or log bodies. Query this before answering human questions about current resources. Use language execution tools to read logs or perform actions. Filter/paginate; global administration never requires taking ownership.',{
    'kind':{'type':'string','enum':['bash','python']},'owner':{'type':'string'},'status':{'type':'string'},
    'include_closed':{'type':'boolean'},'limit':{'type':'integer','minimum':1,'maximum':100},'offset':{'type':'integer','minimum':0}})

