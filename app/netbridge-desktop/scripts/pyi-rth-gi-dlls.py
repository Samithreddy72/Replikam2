"""PyInstaller runtime hook: point GStreamer's PyGObject at the DLLs inside the bundle.

The official Windows GStreamer build patches gi/__init__.py to locate its DLLs from
PYGI_DLL_DIRS, else from <site-packages>\\..\\..\\bin. That directory does not exist in a
frozen app, so `import gi` raised ImportError and the engine silently used the legacy
player. Always the bundle: a GStreamer installed on the presenter's machine must never be
mixed into this process. PYGI_DLL_PATH is upstream PyGObject's (3.57+) name for the same.
"""
import os
import sys

if sys.platform == "win32" and getattr(sys, "_MEIPASS", None):
    os.environ["PYGI_DLL_DIRS"] = sys._MEIPASS
    os.environ["PYGI_DLL_PATH"] = sys._MEIPASS
