"""Native sleep notifications. No power setting changes and no automatic capture on wake."""
import ctypes as C
import sys
import threading


def dispatch(platform, message, suspend, resume):
    if message == (0xe0000280 if platform == 'darwin' else 4):
        suspend()
    elif message in ((0xe0000300,) if platform == 'darwin' else (7, 18)):
        resume()


class PowerObserver:
    def __init__(self, suspend, resume):
        self.active = False
        self.error = None
        self.close_native = lambda: None
        self.refs = []
        try:
            if sys.platform == 'win32':
                self.windows(suspend, resume)
            elif sys.platform == 'darwin':
                self.macos(suspend, resume)
            else:
                self.error = 'Native sleep notifications unavailable on this platform'
        except Exception as exc:
            self.error = type(exc).__name__ + ': ' + str(exc)

    def close(self):
        self.close_native()
        self.active = False

    def windows(self, suspend, resume):
        lib = C.WinDLL('powrprof')
        callback_type = C.WINFUNCTYPE(C.c_ulong, C.c_void_p, C.c_ulong, C.c_void_p)
        class Parameters(C.Structure):
            _fields_ = [('callback', callback_type), ('context', C.c_void_p)]
        def event(context, message, setting):
            try:
                dispatch('win32', message, suspend, resume)
            except Exception:
                pass
            return 0
        callback = callback_type(event)
        parameters = Parameters(callback, None)
        handle = C.c_void_p()
        lib.PowerRegisterSuspendResumeNotification.argtypes = [C.c_ulong, C.c_void_p, C.POINTER(C.c_void_p)]
        lib.PowerRegisterSuspendResumeNotification.restype = C.c_ulong
        lib.PowerUnregisterSuspendResumeNotification.argtypes = [C.c_void_p]
        lib.PowerUnregisterSuspendResumeNotification.restype = C.c_ulong
        result = lib.PowerRegisterSuspendResumeNotification(2, C.byref(parameters), C.byref(handle))
        if result:
            raise OSError(result, 'Sleep notification registration failed')
        self.refs = [lib, callback, parameters, handle]
        self.close_native = lambda: lib.PowerUnregisterSuspendResumeNotification(handle)
        self.active = True

    def macos(self, suspend, resume):
        ready = threading.Event()
        def observe():
            root = 0
            try:
                io = C.CDLL('/System/Library/Frameworks/IOKit.framework/IOKit')
                cf = C.CDLL('/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation')
                callback_type = C.CFUNCTYPE(None, C.c_void_p, C.c_uint, C.c_uint, C.c_void_p)
                io.IORegisterForSystemPower.argtypes = [C.c_void_p, C.POINTER(C.c_void_p), callback_type, C.POINTER(C.c_uint)]
                io.IORegisterForSystemPower.restype = C.c_uint
                io.IOAllowPowerChange.argtypes = [C.c_uint, C.c_long]
                io.IONotificationPortGetRunLoopSource.argtypes = [C.c_void_p]
                io.IONotificationPortGetRunLoopSource.restype = C.c_void_p
                io.IODeregisterForSystemPower.argtypes = [C.POINTER(C.c_uint)]
                io.IONotificationPortDestroy.argtypes = [C.c_void_p]
                io.IOServiceClose.argtypes = [C.c_uint]
                cf.CFRunLoopGetCurrent.restype = C.c_void_p
                cf.CFRunLoopAddSource.argtypes = [C.c_void_p, C.c_void_p, C.c_void_p]
                cf.CFRunLoopStop.argtypes = [C.c_void_p]
                cf.CFRunLoopRunInMode.argtypes = [C.c_void_p, C.c_double, C.c_bool]
                cf.CFRunLoopRunInMode.restype = C.c_int
                def event(context, service, message, argument):
                    try:
                        dispatch('darwin', message, suspend, resume)
                    finally:
                        if message in (0xe0000270, 0xe0000280):
                            io.IOAllowPowerChange(root, argument or 0)
                callback = callback_type(event)
                port, notifier = C.c_void_p(), C.c_uint()
                root = io.IORegisterForSystemPower(None, C.byref(port), callback, C.byref(notifier))
                if not root:
                    raise OSError('Sleep notification registration failed')
                loop = cf.CFRunLoopGetCurrent()
                mode = C.c_void_p.in_dll(cf, 'kCFRunLoopDefaultMode')
                cf.CFRunLoopAddSource(loop, io.IONotificationPortGetRunLoopSource(port), mode)
                stopped = threading.Event()
                self.close_native = lambda: (stopped.set(), cf.CFRunLoopStop(loop))
                self.active = True
                ready.set()
                while not stopped.is_set():
                    cf.CFRunLoopRunInMode(mode, .5, False)
            except Exception as exc:
                self.error = type(exc).__name__ + ': ' + str(exc)
            finally:
                if root:
                    io.IODeregisterForSystemPower(C.byref(notifier))
                    io.IONotificationPortDestroy(port)
                    io.IOServiceClose(root)
                self.active = False
                ready.set()
        self.thread = threading.Thread(target=observe, daemon=True, name='sleep-observer')
        self.thread.start()
        if not ready.wait(5):
            self.error = 'Sleep notification registration timed out'
