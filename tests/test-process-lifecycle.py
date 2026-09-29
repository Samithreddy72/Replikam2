"""Real process-tree fault injection; never starts media or touches other applications."""
import os
import json
import socket
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import time
import unittest

SOURCE=Path(__file__).resolve().parents[1]/'app/netbridge-source'
WORKER="""import json,os,subprocess,sys,time
sys.path.insert(0,sys.argv[1])
from process_guard import Lifecycle
life=Lifecycle();life.start();life.watch_owner()
helpers=[]
for name in ('camera','voice','return','mesh'):
 child=subprocess.Popen([sys.executable,'-u','-c',
   'import socket,time;s=socket.socket(socket.AF_INET,socket.SOCK_DGRAM);s.bind((\"127.0.0.1\",0));print(s.getsockname()[1],flush=True);time.sleep(90)'],
   stdin=subprocess.DEVNULL,stdout=subprocess.PIPE)
 helpers.append((child,int(child.stdout.readline())))
print(json.dumps([(p.pid,port) for p,port in helpers]),flush=True)
if sys.argv[2]=='normal':
 for p,port in helpers:p.terminate()
 for p,port in helpers:p.wait(timeout=10);p.stdout.close()
 life.finish()
else:
 time.sleep(90)
"""


def running(pid):
    if os.name=='nt':
        import ctypes
        from ctypes import wintypes as w
        k=ctypes.WinDLL('kernel32',use_last_error=True)
        k.OpenProcess.argtypes=[w.DWORD,w.BOOL,w.DWORD];k.OpenProcess.restype=w.HANDLE
        k.GetExitCodeProcess.argtypes=[w.HANDLE,ctypes.POINTER(w.DWORD)]
        k.CloseHandle.argtypes=[w.HANDLE]
        handle=k.OpenProcess(0x1000,False,pid)
        if not handle:return False
        try:
            code=w.DWORD();return bool(k.GetExitCodeProcess(handle,ctypes.byref(code))) and code.value==259
        finally:k.CloseHandle(handle)
    try:
        os.kill(pid,0)
        proc=Path('/proc')/str(pid)/'stat'
        if proc.exists() and proc.read_text().split(') ',1)[1].startswith('Z'):return False
        return True
    except ProcessLookupError:return False

class LifecycleTest(unittest.TestCase):
    def worker(self,mode):
        env=dict(os.environ,NB_DESKTOP_OWNER_PIPE='1' if mode=='owner' else '0')
        process=subprocess.Popen([sys.executable,'-u','-c',WORKER,str(SOURCE),mode],stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.PIPE,env=env)
        line=process.stdout.readline()
        if not line:
            self.fail(process.stderr.read().decode())
        helpers=json.loads(line)
        def cleanup():
            if process.poll() is None:process.kill();process.wait(timeout=10)
            for stream in (process.stdin,process.stdout,process.stderr):
                if stream and not stream.closed:stream.close()
        self.addCleanup(cleanup)
        return process,helpers

    def assert_gone(self,pid):
        for _ in range(100):
            if not running(pid):return
            time.sleep(.05)
        self.fail('Owned child survived the engine/shell failure')

    def assert_helpers_released(self,helpers):
        for pid,port in helpers:
            self.assert_gone(pid)
            # Windows Job termination is asynchronous; process exit can precede
            # socket teardown. Require actual release within a bounded interval.
            for attempt in range(100):
                try:
                    with socket.socket(socket.AF_INET,socket.SOCK_DGRAM) as probe:
                        probe.bind(('127.0.0.1',port))
                    break
                except OSError:
                    if attempt == 99:raise
                    time.sleep(.05)

    def test_engine_hard_kill_stops_all_owned_helpers_only(self):
        outsider=subprocess.Popen([sys.executable,'-c','import time;time.sleep(90)'])
        try:
            process,helpers=self.worker('crash');process.kill();process.wait(timeout=10)
            self.assert_helpers_released(helpers)
            self.assertIsNone(outsider.poll())
        finally:
            outsider.terminate();outsider.wait(timeout=10)

    def test_shell_pipe_loss_stops_engine_and_helpers(self):
        process,helpers=self.worker('owner');process.stdin.close();process.wait(timeout=10)
        self.assert_helpers_released(helpers)

    def test_normal_shutdown_exits_successfully(self):
        process,helpers=self.worker('normal');self.assertEqual(process.wait(timeout=10),0)
        self.assert_helpers_released(helpers)

    def test_repeated_restart_has_no_leftover_helper_or_port(self):
        for _ in range(5):
            process,helpers=self.worker('owner');process.stdin.close();process.wait(timeout=10)
            self.assert_helpers_released(helpers)

if __name__=='__main__':unittest.main()
