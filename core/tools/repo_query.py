"""Bounded repository navigation with lazy, disposable workspace symbol caches."""

import ast
import fnmatch
import importlib.util
import importlib.metadata
import json
import os
from pathlib import Path
import re
import shutil
import site
import sqlite3
import subprocess
import sys

spec=importlib.util.spec_from_file_location('_workspace_files',Path(__file__).with_name('workspace_files.py'))
files=importlib.util.module_from_spec(spec);spec.loader.exec_module(files)
SKIP={'.git','.recoding','node_modules','.venv','venv','__pycache__','dist','build','target','.pytest_cache','.mypy_cache','.ruff_cache'}
LANGUAGES={'.py':'python','.js':'javascript','.jsx':'javascript','.ts':'typescript','.tsx':'tsx','.rs':'rust','.go':'go','.c':'c','.h':'c','.cpp':'cpp','.java':'java','.rb':'ruby','.cs':'c_sharp'}


def root_path(value):
    if value in ('.','/workspace',str(files.ROOT)):p=Path.cwd() if value=='.' else files.ROOT
    else:p=files.ROOT/files.relative_path(value)
    if p.is_symlink() or not p.resolve().is_relative_to(files.ROOT):raise ValueError('Use a workspace path without symlinks')
    if p!=files.ROOT:
        with files.parent_fd(str(p.relative_to(files.ROOT)) if p.is_file() else str(p.relative_to(files.ROOT)/'placeholder')):pass
    return p


def enumerate_files(base,maximum,include_ignored,globs):
    if base.is_file():return [base],'explicit',False
    rg=shutil.which('rg') or next((p for p in ('/usr/bin/rg','/usr/local/bin/rg') if Path(p).is_file()),None)
    found=[];more=False
    if rg:
        argv=[rg,'--files','--hidden','-0']
        if include_ignored:argv+=['--no-ignore']
        for name in sorted(SKIP):argv+=['-g','!'+name+'/**']
        for pattern in globs:argv+=['-g',pattern]
        argv+=['.']
        child=subprocess.Popen(argv,cwd=base,stdout=subprocess.PIPE,stderr=subprocess.DEVNULL)
        try:
            pending=b''
            while True:
                data=child.stdout.read(65536)
                if not data:break
                parts=(pending+data).split(b'\0');pending=parts.pop()
                for raw in parts:
                    if len(found)>=maximum:more=True;break
                    found.append(base/os.fsdecode(raw))
                if more:break
        finally:
            if child.poll() is None:child.terminate()
            try:child.wait(timeout=1)
            except subprocess.TimeoutExpired:child.kill();child.wait()
            child.stdout.close()
        return sorted(found),'ripgrep',more
    for current,dirs,names in os.walk(base,followlinks=False):
        dirs[:]=sorted(d for d in dirs if d not in SKIP and not (Path(current)/d).is_symlink())
        for name in sorted(names):
            p=Path(current)/name
            if p.is_symlink() or not p.is_file():continue
            relative=p.relative_to(base).as_posix()
            if globs and not any(fnmatch.fnmatch(relative,g) or p.match(g) for g in globs):continue
            if len(found)>=maximum:return found,'filesystem_defaults',True
            found.append(p)
    return found,'filesystem_defaults',False


def outline(source,path):
    if path.suffix=='.py':
        try:
            tree=ast.parse(source);result=[]
            def visit(node,prefix=''):
                for child in ast.iter_child_nodes(node):
                    if isinstance(child,(ast.ClassDef,ast.FunctionDef,ast.AsyncFunctionDef)):
                        name=prefix+child.name
                        result.append({'name':name,'kind':'class' if isinstance(child,ast.ClassDef) else 'function',
                                       'line':child.lineno,'end_line':getattr(child,'end_lineno',child.lineno)})
                        visit(child,name+'.')
                    else:visit(child,prefix)
            visit(tree);return result,'python_ast'
        except SyntaxError:pass
    language=LANGUAGES.get(path.suffix)
    if language:
        try:
            for directory in [os.environ.get('RECODING_PACKAGE_PATH','/opt/recoding/workspace-packages'),site.getusersitepackages(),*site.getsitepackages()]:
                if directory not in sys.path:sys.path.append(directory)
            from tree_sitter_language_pack import get_parser
            tree=get_parser(language).parse(source.encode('utf-8'));nodes=[tree.root_node];result=[]
            while nodes:
                node=nodes.pop();nodes.extend(reversed(node.named_children))
                if any(kind in node.type for kind in ('function_definition','function_declaration','method_definition','method_declaration','class_definition','class_declaration','struct_item','function_item','interface_declaration')):
                    name=node.child_by_field_name('name')
                    if name is not None:result.append({'name':name.text.decode('utf-8'),'kind':node.type,'line':node.start_point[0]+1,'end_line':node.end_point[0]+1})
            return result,'tree_sitter'
        except (ImportError,LookupError,ValueError,RuntimeError,AttributeError):pass
    result=[]
    pattern=re.compile(r'^\s*(?:(?:export|public|private|static|async)\s+)*(?:class|interface|struct|enum|type|function|def|fn|func)\s+([\w$]+)')
    for number,line in enumerate(source.splitlines(),1):
        match=pattern.search(line)
        if match:result.append({'name':match[1],'kind':'definition_candidate','line':number,'end_line':number})
    return result,'text_heuristic'


def parser_revision(path):
    if path.suffix == '.py':
        return 'python-ast-' + str(sys.version_info[:2])
    for directory in [os.environ.get('RECODING_PACKAGE_PATH','/opt/recoding/workspace-packages'),site.getusersitepackages(),*site.getsitepackages()]:
        if directory not in sys.path:sys.path.append(directory)
    try:
        return 'tree-sitter-' + importlib.metadata.version('tree-sitter-language-pack') + '-' + importlib.metadata.version('tree-sitter')
    except importlib.metadata.PackageNotFoundError:
        return 'text-heuristic-v1'


def query(fields):
    mode=fields.get('mode','files');base=root_path(fields.get('path','.'))
    maximum,limit,offset=fields.get('max_files',2000),fields.get('limit',100),fields.get('offset',0)
    if not 1<=maximum<=20000 or not 1<=limit<=500 or offset<0:raise ValueError('Invalid scan/result bounds')
    candidates,engine,scan_limited=enumerate_files(base,maximum,fields.get('include_ignored',False),fields.get('globs',[]))
    query=fields.get('query','');results=[];scanned=0;cache=None;cache_warning=None;backends=set()
    if mode not in {'files','search','outline','symbol'}:raise ValueError('Unknown query mode')
    if mode in {'search','symbol'} and not query:raise ValueError('Search/symbol requires query')
    pattern=re.compile(query if fields.get('regex',False) else re.escape(query),0 if fields.get('case_sensitive',True) else re.I) if mode=='search' else None
    if mode in {'outline','symbol'}:
        try:
            cache=sqlite3.connect(files.runtime_directory('repo-index')/'outline.sqlite3',timeout=.5)
            cache.execute('PRAGMA journal_mode=WAL')
            cache.execute('CREATE TABLE IF NOT EXISTS symbols (path TEXT PRIMARY KEY, fingerprint TEXT, records TEXT, backend TEXT)')
        except sqlite3.Error as error:cache_warning=str(error);cache=None
    try:
        for path in candidates:
            relative=path.relative_to(files.ROOT).as_posix()
            if mode=='files':
                results.append({'path':str(files.ROOT/relative)});continue
            try:data,_=files.snapshot(relative)
            except (OSError,ValueError):continue
            if data is None:continue
            try:source=files.text(data)
            except (UnicodeError,ValueError):continue
            scanned+=1
            if mode=='search':
                for number,line in enumerate(source.splitlines(),1):
                    if pattern.search(line):results.append({'path':str(files.ROOT/relative),'line':number,'text':line[:600],'sha256':files.digest(data)})
                    if len(results)>offset+limit:break
            else:
                if mode=='symbol' and query not in source:continue
                fingerprint=files.digest(data)+':outline-v2:'+parser_revision(path);cached=None
                try:cached=cache.execute('SELECT records,backend FROM symbols WHERE path=? AND fingerprint=?',(relative,fingerprint)).fetchone() if cache else None
                except sqlite3.Error:pass
                if cached:
                    try:records,backend=json.loads(cached[0]),cached[1]
                    except (ValueError,TypeError):cached=None
                if not cached:
                    records,backend=outline(source,path)
                    if cache:
                        try:cache.execute('INSERT OR REPLACE INTO symbols VALUES (?,?,?,?)',(relative,fingerprint,json.dumps(records),backend));cache.commit()
                        except sqlite3.Error as error:cache_warning=str(error)
                backends.add(backend)
                for record in records:
                    if mode=='outline' or query in record['name']:
                        results.append({'path':str(files.ROOT/relative),**record,'sha256':files.digest(data),'backend':backend})
            if len(results)>offset+limit:break
    finally:
        if cache:cache.close()
    more=len(results)>offset+limit
    return {'ok':True,'mode':mode,'_result_offset':offset,'results':results[offset:offset+limit],'next_offset':offset+limit if more else None,
            'has_more':more,'scan_limited':scan_limited,'candidate_files':len(candidates),'files_read':scanned,'ignore_engine':engine,
            'backends':sorted(backends),'semantic_references':False,'cache_warning':cache_warning,
            'note':'Text matches and heuristic definitions are not semantic references; scope large searches with path/globs.'}


if __name__=='__main__':
    try:result=query(json.loads(sys.stdin.read(1024*1024+1)))
    except (OSError,ValueError,TypeError,KeyError,re.error) as error:result={'ok':False,'error':str(error)}
    files.emit(result);raise SystemExit(0 if result.get('ok') else 1)
