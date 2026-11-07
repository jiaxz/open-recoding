"""Trusted runtime assembly; supplied handlers contain no Agent history or credentials."""
from pathlib import Path
from .inventory import probe_source


def runtime_source(backend):
    root=Path(__file__).parent
    values={'SERVICE_SOURCE':backend.service_source,'TOOL_SOURCES':list(backend.registry.runtimes.values()),
            'IMAGE_SOURCE':backend.image_source,'INVENTORY_SOURCE':probe_source(),
            'FILE_SOURCE':(root/'files.py').read_text(),'LOG_SOURCE':(root/'logs.py').read_text(),
            'IDENTITY':{'protocol':1,'session_id':backend.execution_owner,
                        'node_id':getattr(backend,'node_id','local'),'runtime_id':backend.session.runtime_id}}
    return '\n'.join(key+' = '+repr(value) for key,value in values.items())+'\n'+(root/'runtime.py').read_text()


def pool_source(sources):
    return 'DRIVER_SOURCES = '+repr(sources['drivers'])+'\n'+(Path(__file__).parent/'process_pool.py').read_text()
