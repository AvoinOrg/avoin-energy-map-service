"""Deadline, private local disk budgets and cancellable source work."""
import os
import signal
import time
import sys
from pathlib import Path
from .validation import ExportError


class Budget:
    def __init__(self, root, limits):
        self.root, self.limits = Path(root), limits
        self.end = time.monotonic() + limits['overall_seconds']
        self.total = 0
        self.bundle_bytes = 0
        self.peak_scratch = 0
        self.peak_stream_buffer_bytes = 0
        self.old_handler = None
        self.old_umask = None

    def check(self, scratch=None):
        if time.monotonic() >= self.end:
            raise ExportError('deadline_exceeded')
        st = os.statvfs(self.root)
        if st.f_bavail * st.f_frsize < self.limits['reserve_bytes'] or st.f_favail < 32:
            raise ExportError('disk_reserve')
        if scratch is not None:
            size = sum(p.lstat().st_size for p in scratch.iterdir())
            self.peak_scratch = max(self.peak_scratch, size)
            if size > self.limits['scratch_bytes']:
                raise ExportError('scratch_limit')
            if size + max(self.total,self.bundle_bytes) + 131072 > self.limits['disk_bytes']:
                raise ExportError('disk_limit')

    def observe(self, *objects, resident_bytes=0):
        # Instrument live per-row heap plus conservative one-row driver/parser
        # reserve, bounded SQLite page cache and explicit file read-ahead buffer.
        seen = set()
        def size(v):
            if id(v) in seen:
                return 0
            seen.add(id(v))
            n = sys.getsizeof(v)
            if isinstance(v, dict):
                n += sum(size(k)+size(x) for k,x in v.items())
            elif isinstance(v, (tuple,list)):
                n += sum(size(x) for x in v)
            return n
        used = 128*1024 + self.limits['buffer_bytes']//4 + resident_bytes + sum(size(o) for o in objects)
        self.peak_stream_buffer_bytes = max(self.peak_stream_buffer_bytes,used)
        if used > self.limits['buffer_bytes']:
            raise ExportError('buffer_limit')

    def add(self, count):
        self.total += count
        if self.total > self.limits['total_bytes']:
            raise ExportError('byte_limit')
        self.check()

    def __enter__(self):
        self.old_umask = os.umask(0o077)
        # POSIX main-thread alarm covers blocked libpq, fsync and local SQL too.
        # Source statement timeout is an independent backstop. No snapshot retry.
        self.old_handler = {s: signal.getsignal(s) for s in (signal.SIGALRM, signal.SIGINT, signal.SIGTERM)}
        def stop(signum, frame):
            raise ExportError('deadline_exceeded' if signum == signal.SIGALRM else 'interrupted')
        for s in self.old_handler:
            signal.signal(s, stop)
        signal.setitimer(signal.ITIMER_REAL, max(.001, self.end - time.monotonic()))
        return self

    def __exit__(self, *args):
        signal.setitimer(signal.ITIMER_REAL, 0)
        os.umask(self.old_umask)
        for s, handler in self.old_handler.items():
            signal.signal(s, handler)
