"""Stdlib-only repair lease. Windows sharing denial survives inherited handles.

The file is permanent: never unlink a lock inode or steal it based on mtime.
The pip child inherits a duplicate lease, so closing/killing its GUI parent does
not unlock an environment while pip is still writing it.
"""
from contextlib import contextmanager
import os
import subprocess


def acquire(path: str) -> int | None:
    if os.name == 'nt':
        import ctypes
        from ctypes import wintypes
        import msvcrt
        kernel = ctypes.WinDLL('kernel32', use_last_error=True)
        create = kernel.CreateFileW
        create.argtypes = (wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
                           ctypes.c_void_p, wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE)
        create.restype = wintypes.HANDLE
        handle = create(path, 0x80000000 | 0x40000000, 0, None, 4, 0x80, None)
        if handle == ctypes.c_void_p(-1).value:
            error = ctypes.get_last_error()
            if error == 32:  # ERROR_SHARING_VIOLATION: another owner (or pip) holds it
                return None
            raise ctypes.WinError(error)
        try:
            return msvcrt.open_osfhandle(handle, os.O_RDWR)
        except BaseException:
            kernel.CloseHandle.argtypes = (wintypes.HANDLE,)
            kernel.CloseHandle(handle)
            raise
    import fcntl
    fd = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        os.close(fd)
        return None
    except BaseException:
        os.close(fd)
        raise
    return fd


@contextmanager
def child_lease(fd: int | None):
    """Popen keyword arguments; caller holds this context until run() finishes."""
    if fd is None:
        # Direct installer callers must not run an unguarded package mutation.
        raise RuntimeError('pip installation requires an active repair lease')
    duplicate = os.dup(fd)
    try:
        if os.name == 'nt':
            import msvcrt
            handle = msvcrt.get_osfhandle(duplicate)
            os.set_handle_inheritable(handle, True)
            startup = subprocess.STARTUPINFO()
            startup.dwFlags |= subprocess.STARTF_USESHOWWINDOW
            startup.lpAttributeList = {'handle_list': [handle]}
            yield {'startupinfo': startup, 'close_fds': True}
        else:
            yield {'pass_fds': (duplicate,), 'close_fds': True}
    finally:
        os.close(duplicate)
