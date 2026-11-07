"""Shipped non-builtin tools: static metadata; tool scripts execute through the selected environment."""

import hashlib
from copy import deepcopy
import json
from pathlib import Path


def definitions():
    root = Path(__file__).resolve().parent
    records = json.loads((root / "registry.json").read_text())
    files = {path.relative_to(root).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest() for path in root.rglob("*")
             if path.is_file() and path.suffix in {".py", ".json"}}
    from .python.schemas import SCHEMAS
    services=[{'schema':schema,'service':'python'} for schema in SCHEMAS]
    from .bash.schemas import SCHEMAS as BASH_SCHEMAS
    from .view_image.schemas import SCHEMAS as IMAGE_SCHEMAS
    services += [{'schema':schema,'service':'bash'} for schema in BASH_SCHEMAS]
    local=[]
    local += [{'schema':schema,'runtime':'view_image/runtime.py'} for schema in IMAGE_SCHEMAS]
    from .pool_status import POOL_STATUS
    services.append({'schema':POOL_STATUS,'service':'execution'})
    from .file_transfer.schemas import SCHEMA as TRANSFER_SCHEMA
    services.append({'schema':TRANSFER_SCHEMA,'service':'file_transfer'})
    from .nodes import LIST_NODES
    services.append({'schema':LIST_NODES,'service':'nodes'})
    result=[{**deepcopy(record), "root": str(root), "files": files} for record in [*records,*services,*local]]
    for record in result:
        if record['schema']['function']['name'] not in {'transfer_files','list_nodes'}:
            record['schema']['function']['parameters']['properties']['node_id']={'type':'string','description':'New resources/path operations default to local. Existing resource IDs determine their node; a mismatching node_id is rejected.'}
    return result


def prompt_fragments(definitions):
    from .python.schemas import PROMPT as PYTHON_PROMPT
    from .bash.schemas import PROMPT as BASH_PROMPT
    from .nodes import PROMPT as NODE_PROMPT
    result=[NODE_PROMPT]
    if any(item.get("service") in {"python","bash"} for item in definitions):
        from .process_contract import PROMPT as POOL_PROMPT
        result.append(POOL_PROMPT)
    if any(item.get('service')=='python' for item in definitions): result.append(PYTHON_PROMPT)
    if any(item.get('service')=='bash' for item in definitions): result.append(BASH_PROMPT)
    if any(item.get('service')=='file_transfer' for item in definitions):
        from .file_transfer.schemas import PROMPT as FILE_PROMPT
        result.append(FILE_PROMPT)
    return result


def execution_sources():
    import multiagent.image_reader as image_reader
    root=Path(__file__).parent
    return {'drivers': {
                'python': 'WORKER_SOURCE = '+repr((root/'python/worker.py').read_text())+'\n'+(root/'python/driver.py').read_text(),
                'bash': (root/'bash/worker.py').read_text()+'\n'+(root/'bash/driver.py').read_text()},
            'image':Path(image_reader.__file__).read_text(),
            'registry':(root/'registry.py').read_text()}
