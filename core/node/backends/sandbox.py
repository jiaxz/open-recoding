"""Model-controlled execution stays inside a Bubblewrap session sandbox."""

import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import uuid


from node.client import NodeClient
from node.gpu import discover_gpu
from node.packages import PACKAGE_PATH, USER_PACKAGE_BASE, package_directory as default_packages, prepare_packages, python_environment, prepare_cache, CACHE_PATH, RUNTIME_PATH
from node.storage import protect_directory, workspace_inputs
from types import SimpleNamespace


class SandboxError(RuntimeError):
    pass


def user_site_mount(environment=None):
    """Expose only the system interpreter's compatible user site, read-only."""
    try:
        environment = environment or python_environment()
        host, target = Path(environment["user_site"]).resolve(), environment["sandbox_user_site"]
        if not host.is_dir():
            return None
        if Path.home().resolve().is_relative_to(host):
            raise SandboxError("user site must be a dedicated package directory, not home or its parent")
        return host, target
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        raise SandboxError(f"cannot discover Python user site: {exc}") from exc


class Sandbox:
    mode = "sandbox"

    def __init__(self, directory, timeout=30.0, max_output=16000, *, network=True, gpu=True,
                 hidden_paths=(), hidden_files=(), package_directory=None, execution_owner=None, service_source="", registry=None, image_source=""):
        self.directory = Path(directory).resolve(strict=True)
        if not self.directory.is_dir():
            raise SandboxError("session directory must be a directory")
        # Avoid accidentally mounting all host files or the runtime itself writable.
        protected = ("/usr", "/bin", "/sbin", "/lib", "/lib64", "/etc", "/proc", "/sys", "/dev")
        if self.directory == Path("/") or any(
            self.directory.is_relative_to(Path(p).resolve()) for p in protected
        ):
            raise SandboxError("choose a project directory, not a system directory")
        if sys.platform != "linux" or self.mode == "sandbox" and not shutil.which("bwrap"):
            raise SandboxError("requires Linux and bubblewrap (apt install bubblewrap)")
        if not Path("/usr/bin/python3").exists():
            raise SandboxError("requires /usr/bin/python3")
        if not Path("/bin/bash").exists():
            raise SandboxError("requires /bin/bash")
        self.bwrap = shutil.which("bwrap")
        self.execution_owner = execution_owner or uuid.uuid4().hex
        if not __import__("re").fullmatch(r"[a-zA-Z0-9_-]{1,100}", self.execution_owner):
            raise SandboxError("Invalid job owner")
        self.service_source = service_source
        self.timeout = timeout
        self.max_output = max_output
        self.network = network
        environment = python_environment()
        self.python_environment = environment
        protect_directory(self.directory / ".recoding")
        self.input_directory = workspace_inputs(self.directory)
        self.package_directory = prepare_packages(package_directory or default_packages(self.directory), environment)
        self.runtime_directory = self.directory / ".recoding" / "runtime"
        if self.runtime_directory.is_symlink():
            raise SandboxError("workspace runtime cannot be a symlink")
        self.runtime_directory.mkdir(mode=0o700, exist_ok=True)
        self.home_directory = self.runtime_directory / "home"
        if self.home_directory.is_symlink():
            raise SandboxError("workspace tool home cannot be a symlink")
        self.home_directory.mkdir(mode=0o700, exist_ok=True)
        # OpenSSH and other Unix tools require getpwuid/getgrgid. Supply a
        # synthetic identity, never the Host account database or credentials.
        identity=self.runtime_directory/'system'
        if identity.is_symlink():raise SandboxError('Sandbox identity directory cannot be a symlink')
        identity.mkdir(mode=0o700,exist_ok=True)
        uid,gid=os.getuid(),os.getgid()
        records={'passwd':('root:x:0:0:root:/root:/bin/bash\n' if uid else '')+f'recoding:x:{uid}:{gid}:Recoding:{RUNTIME_PATH}/home:/bin/bash\n',
                 'group':('root:x:0:\n' if gid else '')+f'recoding:x:{gid}:\n'}
        self.identity_files={}
        for name,content in records.items():
            target=identity/name
            fd=os.open(target,os.O_WRONLY|os.O_CREAT|os.O_TRUNC|os.O_NOFOLLOW|os.O_CLOEXEC,0o600)
            with os.fdopen(fd,'w') as stream:stream.write(content)
            self.identity_files[name]=target
        self.cache_directory = prepare_cache()
        private = {Path(value).resolve(strict=True) for value in hidden_paths}
        container = self.directory / ".recoding"
        if container.is_dir():
            if container.is_symlink():
                raise SandboxError("Session storage cannot be a symlink")
            private.add(container)
        if any(path == self.directory or not path.is_relative_to(self.directory) or not path.is_dir() for path in private):
            raise SandboxError("private Session metadata must be inside the workspace")
        def masks(parent, path):
            public = any(path.is_relative_to(shared) for shared in (self.input_directory, self.runtime_directory))
            return path.is_relative_to(parent) and not (parent == container and public)
        self.hidden_paths = [path for path in sorted(private)
                             if not any(path != parent and masks(parent, path) for parent in private)]
        files = {Path(value).resolve() for value in hidden_files}
        self.hidden_files = sorted(path for path in files if path.is_file() and path.is_relative_to(self.directory)
                                   and not any(masks(folder, path) for folder in private))
        self.registry = registry or SimpleNamespace(entries={},mounts={},runtimes={})
        self.image_source=image_source
        self.tool_names = self.registry.entries.keys()
        self.gpu = discover_gpu(enabled=gpu)
        self.user_site = user_site_mount(environment)
        self.tool_workspace = "/workspace" if self.mode == "sandbox" else str(self.directory)
        self.paths = {"workspace":self.tool_workspace,
                      "packages":PACKAGE_PATH if self.mode == "sandbox" else str(self.package_directory),
                      "runtime":RUNTIME_PATH if self.mode == "sandbox" else str(self.runtime_directory),
                      "cache":CACHE_PATH if self.mode == "sandbox" else str(self.cache_directory),
                      "user_base":USER_PACKAGE_BASE if self.mode == "sandbox" else environment["user_base"]}
        from node.capabilities import describe
        self.capabilities=describe(self.directory)
        self.session = NodeClient(self)

    @property
    def gpu_info(self):
        return self.gpu.metadata()

    def close(self):
        with self.session.lock:
            self.session.stopped=True
            self.session.close()

    def command(self, argv, *, source_fd=None):
        command = [self.bwrap, "--unshare-all", "--die-with-parent", "--new-session",
                   "--cap-drop", "ALL", "--clearenv"]
        if source_fd is not None:
            command += ["--ro-bind-data",str(source_fd),"/opt/recoding/supervisor.py"]
        if self.network:
            command += ["--share-net"]
        for path in ("/usr", "/bin", "/lib", "/lib64"):
            if Path(path).exists():
                command += ["--ro-bind", path, path]
        if Path("/etc/ld.so.cache").exists():
            command += ["--ro-bind", "/etc/ld.so.cache", "/etc/ld.so.cache"]
        for name,source in self.identity_files.items():
            command += ['--ro-bind',str(source),'/etc/'+name]
        # DNS configuration and public CA certificates, never host credentials.
        for path in ("/etc/resolv.conf", "/etc/hosts", "/etc/nsswitch.conf",
                     "/etc/gai.conf", "/etc/ssl/certs", "/etc/pki/tls/certs"):
            if Path(path).exists():
                command += ["--ro-bind", path, path]
        # Public distro runtime data: alternatives resolve BLAS/LAPACK links,
        # while font and TeX tools require their configuration and built formats.
        for path in ("/etc/alternatives", "/etc/fonts", "/etc/matplotlibrc", "/etc/texmf",
                     "/var/lib/texmf", "/var/cache/fontconfig"):
            if Path(path).exists():
                command += ["--ro-bind", path, path]
        for bundle in ("/etc/ssl/certs/ca-certificates.crt", "/etc/pki/tls/certs/ca-bundle.crt"):
            if Path(bundle).is_file():
                command += ["--setenv", "SSL_CERT_FILE", bundle, "--setenv", "PIP_CERT", bundle]
                break
        command += ["--proc", "/proc", "--dev", "/dev", "--tmpfs", "/tmp",
                    "--bind", str(self.directory), "/workspace"]
        for private in self.hidden_paths:
            target = "/workspace/" + private.relative_to(self.directory).as_posix()
            command += ["--tmpfs", target]
            if private == self.directory / ".recoding":
                command += ["--bind", str(self.input_directory), target + "/inputs",
                            "--bind", str(self.runtime_directory), target + "/runtime"]
            command += ["--remount-ro", target]
        # Runtime code and controller metadata are never writable task files.
        for name in ('node-code','nodes'):
            if (self.runtime_directory/name).exists():
                target='/workspace/.recoding/runtime/'+name
                command+=['--tmpfs',target,'--remount-ro',target]
        for private in self.hidden_files:
            target = "/workspace/" + private.relative_to(self.directory).as_posix()
            command += ["--ro-bind", "/dev/null", target]
        command += self.gpu.mount_arguments()
        for source, target in self.registry.mounts.items():
            command += ["--ro-bind", str(source), target]
        if self.user_site:
            host, target = self.user_site
            # Expose only packages, never the whole user base or home directory.
            command += ["--ro-bind", str(host), target]
        command += ["--bind", str(self.home_directory), RUNTIME_PATH + "/home",
                    "--bind", str(self.package_directory.parent / "packages.lock"), RUNTIME_PATH + "/packages.lock",
                    "--bind", str(self.package_directory), PACKAGE_PATH,
                    "--bind", str(self.cache_directory), CACHE_PATH]
        command += ["--chdir", self.tool_workspace, "--remount-ro", "/"]
        for name,value in self.environment().items():
            command += ["--setenv",name,value]
        return [*command,"--",*argv]

    def process_options(self):
        return {"env":{"PATH":"/usr/bin:/bin","LANG":"C.UTF-8"}}

    def environment(self):
        paths=self.paths;home=paths["runtime"]+"/home";cache=paths["cache"]
        path=paths["packages"]+"/bin:/usr/bin:/bin:/usr/local/bin"
        if self.gpu.cuda_home:path+=":"+self.gpu.cuda_home+"/bin"
        env={"PATH":path,"HOME":home,"LANG":"C.UTF-8","TMPDIR":"/tmp",
             "XDG_CONFIG_HOME":home+"/config","XDG_DATA_HOME":home+"/data","XDG_STATE_HOME":home+"/state",
             "MPLCONFIGDIR":home+"/config/matplotlib","PYTHONPATH":paths["packages"],
             "PYTHONUSERBASE":paths["user_base"],"PIP_TARGET":paths["packages"],
             "RECODING_EXECUTION_OWNER":self.execution_owner,"RECODING_EXECUTION_MODE":self.mode,
             "RECODING_WORKSPACE":paths["workspace"],"RECODING_PACKAGE_PATH":paths["packages"],
             "RECODING_RUNTIME_PATH":paths["runtime"],"RECODING_CACHE_PATH":cache,
             "XDG_CACHE_HOME":cache,"PIP_DISABLE_PIP_VERSION_CHECK":"1","PIP_NO_INPUT":"1"}
        for name,suffix in {"PIP_CACHE_DIR":"pip","UV_CACHE_DIR":"uv","npm_config_cache":"npm",
                            "CARGO_HOME":"cargo","GOMODCACHE":"go/mod","GOCACHE":"go/build",
                            "HF_HOME":"huggingface","TORCH_HOME":"torch","CCACHE_DIR":"ccache","SCCACHE_DIR":"sccache"}.items():
            env[name]=cache+"/"+suffix
        for bundle in ("/etc/ssl/certs/ca-certificates.crt","/etc/pki/tls/certs/ca-bundle.crt"):
            if Path(bundle).is_file():env.update(SSL_CERT_FILE=bundle,PIP_CERT=bundle);break
        env.update(self.gpu.environment())
        return env

    @property
    def session_state(self):
        return dict(self.session.session_state)

    def check(self):
        result = self.session.request("check")
        if result.get("exit_code") != 0 or result.get("stdout", "").strip() != "sandbox-ok":
            raise SandboxError("sandbox unavailable; refusing unsandboxed execution: "
                               + result.get("error", result.get("stderr", "")).strip())

    def execute(self, name, arguments):
        try:
            if name in self.registry.entries:
                if self.registry.entries[name].get('service'):
                    return {'error':'This service requires an injected Host tool transport','exit_code':1}
                return self.registry.execute(self.session, name, arguments)
            raise ValueError(f"Tool is not installed: {name}")
        except (OSError, ValueError, TypeError) as exc:
            return {"error": str(exc)}
