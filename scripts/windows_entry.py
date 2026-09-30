"""Console entry point for the Windows stdio bundle."""

import ctypes
from multiprocessing import freeze_support

from p4_history.cli import main
from p4_history.server import create_server  # noqa: F401 -- preload native dependencies

if __name__ == "__main__":
    freeze_support()
    # Git and p4 must use their own DLLs, not PyInstaller's inherited search directory.
    # Python 3.8+ resolves extension DLLs through its explicit DLL directories.
    if not ctypes.windll.kernel32.SetDllDirectoryW(None):
        raise ctypes.WinError()
    main()
