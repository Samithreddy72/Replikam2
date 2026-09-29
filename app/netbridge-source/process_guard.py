"""Own Studio's process tree across shell/engine crashes without global process-name killing."""
import ctypes
import os
import queue
from pathlib import Path
import signal
import subprocess
import sys
import threading


def guard_main(group):
    # The guardian stays in the private engine group, reserving its identity until cleanup.
    if os.name == 'nt' or os.getpgrp() != group or os.getpid() == group:
        raise SystemExit('Refusing an unowned process group')
    print('READY', flush=True)
    if sys.stdin.buffer.readline() != b'DONE\n':
        os.killpg(group, signal.SIGKILL)


class Lifecycle:
    def __init__(self):
        self.guard = None
        self.job = None

    def start(self):
        if os.name == 'nt':
            from ctypes import wintypes as w
            class Basic(ctypes.Structure):
                _fields_ = [('process_time', ctypes.c_int64), ('job_time', ctypes.c_int64),
                            ('flags', w.DWORD), ('min_ws', ctypes.c_size_t), ('max_ws', ctypes.c_size_t),
                            ('active', w.DWORD), ('affinity', ctypes.c_size_t),
                            ('priority', w.DWORD), ('scheduling', w.DWORD)]
            class IO(ctypes.Structure):
                _fields_ = [(name, ctypes.c_uint64) for name in ('ro', 'wo', 'oo', 'rb', 'wb', 'ob')]
            class Extended(ctypes.Structure):
                _fields_ = [('basic', Basic), ('io', IO), ('process_memory', ctypes.c_size_t),
                            ('job_memory', ctypes.c_size_t), ('peak_process', ctypes.c_size_t),
                            ('peak_job', ctypes.c_size_t)]
            kernel = ctypes.WinDLL('kernel32', use_last_error=True)
            kernel.CreateJobObjectW.argtypes = [ctypes.c_void_p, w.LPCWSTR]
            kernel.CreateJobObjectW.restype = w.HANDLE
            kernel.SetInformationJobObject.argtypes = [w.HANDLE, ctypes.c_int, ctypes.c_void_p, w.DWORD]
            kernel.SetInformationJobObject.restype = w.BOOL
            kernel.AssignProcessToJobObject.argtypes = [w.HANDLE, w.HANDLE]
            kernel.AssignProcessToJobObject.restype = w.BOOL
            kernel.GetCurrentProcess.restype = w.HANDLE
            kernel.CloseHandle.argtypes = [w.HANDLE]
            job = kernel.CreateJobObjectW(None, None)
            if not job:
                raise ctypes.WinError(ctypes.get_last_error())
            info = Extended(); info.basic.flags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
            if not kernel.SetInformationJobObject(job, 9, ctypes.byref(info), ctypes.sizeof(info)) or not kernel.AssignProcessToJobObject(job, kernel.GetCurrentProcess()):
                error = ctypes.get_last_error(); kernel.CloseHandle(job)
                raise ctypes.WinError(error)
            # Do not manually close while the engine is alive. OS teardown closes this
            # non-inherited handle and terminates any surviving descendants atomically.
            self.job = job
        else:
            if os.getpgrp() != os.getpid():
                os.setpgid(0, 0)
            args = [sys.executable]
            if not getattr(sys, 'frozen', False):
                args.append(str(Path(__file__).resolve()))
            args += ['--netbridge-process-guard', str(os.getpgrp())]
            self.guard = subprocess.Popen(args, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                          stderr=subprocess.DEVNULL)
            ready = queue.Queue()
            threading.Thread(target=lambda: ready.put(self.guard.stdout.readline()), daemon=True).start()
            try:
                handshake = ready.get(timeout=10)
            except queue.Empty:
                self.guard.kill(); self.guard.wait(timeout=5)
                raise RuntimeError('Process guardian startup timed out')
            if handshake != b'READY\n':
                raise RuntimeError('Process guardian failed to start')

    def watch_owner(self):
        if os.environ.get('NB_DESKTOP_OWNER_PIPE') != '1':
            return
        def watch():
            # Raw reads avoid a daemon thread holding Python stdin's buffered lock at normal exit.
            while os.read(sys.stdin.fileno(), 4096):
                pass  # Rust owns the sole write end; EOF means shell disappeared.
            if os.name == 'nt':
                os._exit(70)  # closing the engine's Job Object kills only its descendants
            os.killpg(os.getpgrp(), signal.SIGKILL)
        threading.Thread(target=watch, daemon=True, name='desktop-owner').start()

    def finish(self):
        if self.guard:
            self.guard.stdin.write(b'DONE\n'); self.guard.stdin.flush()
            self.guard.stdin.close()
            self.guard.wait(timeout=5)
            self.guard.stdout.close()


if __name__ == '__main__':
    if len(sys.argv) != 3 or sys.argv[1] != '--netbridge-process-guard':
        raise SystemExit('Private process guardian')
    guard_main(int(sys.argv[2]))
