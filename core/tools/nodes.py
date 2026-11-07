"""Node discovery is a non-builtin resource tool; credentials stay in the Host."""
from multiagent.tools import tool

LIST_NODES=tool('list_nodes','List configured Session compute nodes, workspaces, connection state and actual runtime IDs; never SSH credentials or Agent contexts. Local is always available by default. Remote nodes must be connected before claiming their resources. Select node_id when creating processes or using path-based tools; existing process/execution IDs determine placement automatically.',{})
PROMPT='''Agent loops and communication run in the Host control plane. Actual tools run in the chosen NodeRuntime.
Local is mandatory and the default; all Agents share each node workspace and its Bash/Python pools.
Use list_nodes to discover extra compute resources and choose suitable nodes for independent work.
Combine model-call parallelism, node parallelism, process parallelism and appropriate internal I/O concurrency;
manage ownership, CPU/GPU/memory/connection budgets and cleanup at each level. Supply node_id for a new
process or path-based operation; existing IDs route to their original node. Node disconnection is unknown
execution state: query existing IDs after reconnect and never blindly replay side effects. Do not assume
identical absolute paths imply shared files. Use transfer_files for Host-relayed copies. Move remote figures
to local before send_user_message/peer messages; presentation never reads browser-local files.'''
