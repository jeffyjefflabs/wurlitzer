"""Capture C-level FD output on pipes

Use `wurlitzer.capture` or `wurlitzer.redirect_to_sys` as context managers.
"""
from __future__ import print_function

__version__ = '0.0.1'

__all__ = [
    'capture',
    'redirect_to_sys',
    'Wurlitzer',
]

from contextlib import contextmanager
import ctypes
from fcntl import fcntl, F_GETFL, F_SETFL
import io
import os
import select
import sys
import threading

libc = ctypes.CDLL(None)

try:
    c_stdout_p = ctypes.c_void_p.in_dll(libc, 'stdout')
    c_stderr_p = ctypes.c_void_p.in_dll(libc, 'stderr')
except ValueError:
    # libc.stdout is has a funny name on OS X
    c_stdout_p = ctypes.c_void_p.in_dll(libc, '__stdoutp')
    c_stderr_p = ctypes.c_void_p.in_dll(libc, '__stderrp')

STDOUT = 2
PIPE = 3

_default_encoding = getattr(sys.stdin, 'encoding', None) or 'utf8'
if _default_encoding.lower() == 'ascii':
    # don't respect ascii
    _default_encoding = 'utf8'

class Wurlitzer(object):
    """Class for Capturing Process-level FD output via dup2
    
    Typically used via `wurlitzer.capture`
    """
    flush_interval = 0.2
    
    def __init__(self, stdout=None, stderr=None, encoding=_default_encoding):
        """
        Parameters
        ----------
        stdout: stream or None
            The stream for forwarding stdout.
        stderr = stream or None
            The stream for forwarding stderr.
        encoding: str or None
            The encoding to use, if streams should be interpreted as text.
        """
        self._stdout = stdout
        if stderr == STDOUT:
            self._stderr = self._stdout
        else:
            self._stderr = stderr
        self.encoding = encoding
        self._save_fds = {}
        self._real_fds = {}
        self._handlers = {}
        self._handlers['stderr'] = self._handle_stderr
        self._handlers['stdout'] = self._handle_stdout
    
    def _setup_pipe(self, name):
        real_fd = getattr(sys, '__%s__' % name).fileno()
        save_fd = os.dup(real_fd)
        self._save_fds[name] = save_fd
        
        pipe_out, pipe_in = os.pipe()
        os.dup2(pipe_in, real_fd)
        os.close(pipe_in)
        self._real_fds[name] = real_fd
        
        # make pipe_out non-blocking
        flags = fcntl(pipe_out, F_GETFL)
        fcntl(pipe_out, F_SETFL, flags|os.O_NONBLOCK)
        return pipe_out
    
    def _decode(self, data):
        """Decode data, if any
        
        Called before pasing to stdout/stderr streams
        """
        if self.encoding:
            data = data.decode(self.encoding, 'replace')
        return data
    
    def _handle_stdout(self, data):
        if self._stdout:
            self._stdout.write(self._decode(data))
    
    def _handle_stderr(self, data):
        if self._stderr:
            self._stderr.write(self._decode(data))
    
    def _setup_handle(self):
        """Setup handle for output, if any"""
        self.handle = (self._stdout, self._stderr)
    
    def _finish_handle(self):
        """Finish handle, if anything should be done when it's all wrapped up."""
        pass
    
    def __enter__(self):
        # setup handle
        self._setup_handle()
        
        # create pipe for stdout
        pipes = []
        names = {}
        if self._stdout:
            pipe = self._setup_pipe('stdout')
            pipes.append(pipe)
            names[pipe] = 'stdout'
        if self._stderr:
            pipe = self._setup_pipe('stderr')
            pipes.append(pipe)
            names[pipe] = 'stderr'
        
        def forwarder():
            """Forward bytes on a pipe to stream messages"""
            while True:
                # flush libc's buffers before calling select
                libc.fflush(c_stdout_p)
                libc.fflush(c_stderr_p)
                r, w, x = select.select(pipes, [], [], self.flush_interval)
                if not r:
                    # nothing to read, next iteration will flush and check again
                    continue
                for pipe in r:
                    name = names[pipe]
                    data = os.read(pipe, 1024)
                    if not data:
                        # pipe closed, stop polling
                        pipes.remove(pipe)
                    else:
                        handler = getattr(self, '_handle_%s' % name)
                        handler(data)
                if not pipes:
                    # pipes closed, we are done
                    break
        self.thread = threading.Thread(target=forwarder)
        self.thread.daemon = True
        self.thread.start()
        
        return self.handle

    def __exit__(self, exc_type, exc_value, traceback):
        # flush the underlying C buffers
        libc.fflush(c_stdout_p)
        libc.fflush(c_stderr_p)
        # close FDs, signaling output is complete
        for real_fd in self._real_fds.values():
            os.close(real_fd)
        self.thread.join()
        
        # restore original state
        for name, real_fd in self._real_fds.items():
            save_fd = self._save_fds[name]
            os.dup2(save_fd, real_fd)
            os.close(save_fd)
        # finalize handle
        self._finish_handle()


@contextmanager
def capture(stdout=PIPE, stderr=PIPE, encoding=_default_encoding):
    """Capture C-level stdout/stderr in a context manager.
    
    The return value for the context manager is (stdout, stderr).
    
    Examples
    --------
    
    >>> with capture() as (stdout, stderr):
    ...     printf("C-level stdout")
    ... output = stdout.read()
    """
    stdout_pipe = stderr_pipe = False
    # setup stdout
    if stdout == PIPE:
        stdout_r, stdout_w = os.pipe()
        stdout_w = os.fdopen(stdout_w, 'wb')
        stdout_r = os.fdopen(stdout_r, 'rb')
        if encoding:
            stdout_r = io.TextIOWrapper(stdout_r, encoding=encoding)
        stdout_pipe = True
    else:
        stdout_r = stdout_w = stdout
    # setup stderr
    if stderr == STDOUT:
        stderr_r = None
        stderr_w = stdout_w
    elif stderr == PIPE:
        stderr_r, stderr_w = os.pipe()
        stderr_w = os.fdopen(stderr_w, 'wb')
        stderr_r = os.fdopen(stderr_r, 'rb')
        if encoding:
            stderr_r = io.TextIOWrapper(stderr_r, encoding=encoding)
        stderr_pipe = True
    else:
        stderr_r = stderr_w = stderr
    if stdout_pipe or stderr_pipe:
        capture_encoding = None
    else:
        capture_encoding = encoding
    w = Wurlitzer(stdout=stdout_w, stderr=stderr_w, encoding=capture_encoding)
    try:
        with w:
            yield stdout_r, stderr_r
    finally:
        # close pipes
        if stdout_pipe:
            stdout_w.close()
        if stderr_pipe:
            stderr_w.close()


def redirect_to_sys(encoding=_default_encoding):
    """Redirect C-level stdout/stderr to sys.stdout/stderr
    
    This is useful of sys.sdout/stderr are already being forwarded somewhere.
    
    DO NOT USE THIS if sys.stdout and sys.stderr are not already being forwarded.
    """
    return capture(sys.stdout, sys.stderr, encoding=encoding)


def redirect_everything_to_sys(encoding=_default_encoding):
    """Redirect all C output to sys.stdout/err
    
    This is not a context manager; it turns on C-forwarding permanently.
    """
    redirect_to_sys(encoding).__enter__()

