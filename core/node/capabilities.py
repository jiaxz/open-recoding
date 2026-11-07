"""Read-only resource facts from the actual execution node, never host guesses."""
import os,platform,shutil


def describe(workspace):
    try:memory=os.sysconf('SC_PAGE_SIZE')*os.sysconf('SC_PHYS_PAGES')
    except (ValueError,OSError):memory=None
    try:cpus=len(os.sched_getaffinity(0))
    except AttributeError:cpus=os.cpu_count()
    return {'os':platform.system(),'architecture':platform.machine(),'visible_cpus':cpus,
            'physical_memory_bytes':memory,'workspace_free_bytes':shutil.disk_usage(workspace).free,
            'quota_enforced':False}
