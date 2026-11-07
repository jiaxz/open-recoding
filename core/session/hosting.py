"""Opt-in process protocol for application hosts; direct Session use is separate."""

from .store import StoreLease


class SessionBusyError(ValueError):
    """Another Host owns the archive, or a short archive read is in progress."""


class SessionHostProtocol:
    """Run excludes browsing. Locks are kernel-owned descriptors, never PID files."""

    @staticmethod
    def _lease(directory, *, shared=False):
        try:
            return StoreLease(directory, shared=shared, name='host.lock')
        except ValueError as error:
            raise SessionBusyError('Session 正由其他 Host 独占，或正在完成存档读取；请稍后重试') from error

    @staticmethod
    def available(directory):
        return StoreLease.available(directory, name='host.lock')

    @staticmethod
    def read(directory):
        return SessionHostProtocol._lease(directory, shared=True)

    @staticmethod
    def browse(directory):
        from .saved import SavedSession
        return SavedSession(directory, enforce_host_protocol=True)

    @staticmethod
    def resume(directory, *, config=None, initial_settings=None, progress=None):
        from .session import Session
        from .store import SessionStore
        store = SessionStore(directory)
        lease = SessionHostProtocol._lease(store.folder)
        try:
            return Session(store, config, initial_settings=initial_settings,
                           progress=progress, host_lease=lease)
        except BaseException:
            lease.close()
            raise
