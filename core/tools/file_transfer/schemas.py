"""Non-builtin Host-relayed file transfer; endpoints are configured by the Session owner."""
from multiagent.tools import tool

SCHEMA = tool('transfer_files',
    'Copy a file/directory between configured node workspaces, ALWAYS streamed through the Host (remote nodes need not reach each other). '
    'action=nodes lists public file targets; it does not imply remote Python/Bash execution support. '
    'action=copy requires source_path and destination_path (exact destination, not an implicit parent); source_node/destination_node default to local. '
    'mode=sync waits; async returns transfer.id; use status/list/cancel to manage. '
    'Symlinks, special files, private Session/runtime data and credentials are excluded/rejected. '
    'overwrite defaults false; each verified file publishes atomically, directory copies can partially commit. '
    'Files/logs never enter model context; completion reports paths, counts and errors. No implicit directory synchronization or command execution.',
    {'action': {'type': 'string', 'enum': ['copy', 'nodes', 'status', 'list', 'cancel']},
     'source_node': {'type': 'string'}, 'destination_node': {'type': 'string'},
     'source_path': {'type': 'string', 'minLength': 1}, 'destination_path': {'type': 'string', 'minLength': 1},
     'mode': {'type': 'string', 'enum': ['sync', 'async']}, 'overwrite': {'type': 'boolean'},
     'transfer_id': {'type': 'string'}})

PROMPT = ('Use transfer_files for explicit file/directory copies between configured node workspaces. '
          'Discover targets with action=nodes. All bytes pass through the Host; no remote-to-remote connectivity is assumed. '
          'Use async for large transfers and status/cancel to manage them. A directory copy may partially commit; '
          'inspect completed counts and errors before retrying. Transfer remote figures to local workspace before '
          'linking them in human/Agent messages. File targets alone do not enable remote Python/Bash execution.')
