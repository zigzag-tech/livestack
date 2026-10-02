"""Run the worker as a Windows service (the systemd/launchd unit's role).

Stdlib only, through advapi32 via ctypes: no pywin32, whose service host is
fragile inside virtual environments. The Service Control Manager starts this
process (`python.exe -m livestack_node.workloads.windows_service --config X`);
it must call StartServiceCtrlDispatcherW promptly. The worker loop runs on a
thread; a stop request reports SERVICE_STOPPED and ends the process. Attempts
are not ended by that: each runs in its own Job Object held by its wrapper, and
the next start reconciles it, as a systemd restart does on Linux. A worker
thread that dies ends the process WITHOUT reporting stopped, which the SCM
counts as a failure and answers with the configured restart action.

`--console` runs the same worker in the foreground, for diagnosis.
Install: see docs/windows-worker.md (sc.exe create ... + failure actions).
"""
from __future__ import annotations

import argparse
import ctypes
from ctypes import wintypes
import json
import logging
from logging.handlers import RotatingFileHandler
import os
from pathlib import Path
import threading
import traceback

SERVICE_WIN32_OWN_PROCESS = 0x10
SERVICE_STOPPED, SERVICE_START_PENDING, SERVICE_STOP_PENDING, SERVICE_RUNNING = 1, 2, 3, 4
SERVICE_ACCEPT_STOP, SERVICE_ACCEPT_SHUTDOWN = 0x1, 0x4
SERVICE_CONTROL_STOP, SERVICE_CONTROL_INTERROGATE, SERVICE_CONTROL_SHUTDOWN = 1, 4, 5
NO_ERROR, ERROR_CALL_NOT_IMPLEMENTED = 0, 120
ERROR_SERVICE_SPECIFIC_ERROR = 1066


class _Status(ctypes.Structure):
    _fields_ = [('dwServiceType', wintypes.DWORD), ('dwCurrentState', wintypes.DWORD),
                ('dwControlsAccepted', wintypes.DWORD), ('dwWin32ExitCode', wintypes.DWORD),
                ('dwServiceSpecificExitCode', wintypes.DWORD), ('dwCheckPoint', wintypes.DWORD),
                ('dwWaitHint', wintypes.DWORD)]


_MAIN = ctypes.WINFUNCTYPE(None, wintypes.DWORD, ctypes.POINTER(wintypes.LPWSTR))
_HANDLER = ctypes.WINFUNCTYPE(wintypes.DWORD, wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p, ctypes.c_void_p)


class _TableEntry(ctypes.Structure):
    _fields_ = [('lpServiceName', wintypes.LPWSTR), ('lpServiceProc', _MAIN)]


def run_worker(config_path, stopping=None):
    """The worker_service loop for one config; returns only on error."""
    from .worker import WorkloadWorker
    from .worker_service import serve
    config = json.loads(Path(config_path).read_text())
    root = Path(config['state_dir'])
    root.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(level=logging.INFO, handlers=[
        RotatingFileHandler(root/'worker.log', maxBytes=8*1024**2, backupCount=2)])
    logging.info('windows worker starting (pid %d, config %s)', os.getpid(), config_path)
    worker = WorkloadWorker(config)
    try:
        serve(worker)
    finally:
        worker.close()


class Service:
    def __init__(self, name, config_path, *, target=None):
        """target: what the service runs (default: the worker for config_path)."""
        self.name, self.config_path = name, config_path
        self.target = target or (lambda: run_worker(config_path))
        self.stop = threading.Event()
        self.failed = None
        self.status_handle = None
        api = ctypes.WinDLL('advapi32', use_last_error=True)
        self.api = api
        api.StartServiceCtrlDispatcherW.argtypes = [ctypes.POINTER(_TableEntry)]
        api.StartServiceCtrlDispatcherW.restype = wintypes.BOOL
        api.RegisterServiceCtrlHandlerExW.argtypes = [wintypes.LPCWSTR, _HANDLER, ctypes.c_void_p]
        api.RegisterServiceCtrlHandlerExW.restype = wintypes.HANDLE
        api.SetServiceStatus.argtypes = [wintypes.HANDLE, ctypes.POINTER(_Status)]
        api.SetServiceStatus.restype = wintypes.BOOL
        # Keep the callbacks referenced for the life of the process.
        self._main = _MAIN(self._service_main)
        self._handler = _HANDLER(self._control)

    def set_status(self, state, exit_code=NO_ERROR, specific=0, wait_hint=0):
        accepted = 0 if state in (SERVICE_START_PENDING, SERVICE_STOPPED) else \
            SERVICE_ACCEPT_STOP | SERVICE_ACCEPT_SHUTDOWN
        status = _Status(SERVICE_WIN32_OWN_PROCESS, state, accepted, exit_code, specific, 0, wait_hint)
        self.api.SetServiceStatus(self.status_handle, ctypes.byref(status))

    def _control(self, control, event_type, event_data, context):
        if control in (SERVICE_CONTROL_STOP, SERVICE_CONTROL_SHUTDOWN):
            self.set_status(SERVICE_STOP_PENDING, wait_hint=5000)
            self.stop.set()
            return NO_ERROR
        if control == SERVICE_CONTROL_INTERROGATE:
            return NO_ERROR
        return ERROR_CALL_NOT_IMPLEMENTED

    def _worker(self):
        try:
            self.target()
        except BaseException as error:  # noqa: BLE001 - recorded, then the process fails
            self.failed = error
            logging.error('windows service thread ended: %s', ''.join(traceback.format_exception(error))[-4000:])
        self.stop.set()

    def _service_main(self, argc, argv):
        self.status_handle = self.api.RegisterServiceCtrlHandlerExW(self.name, self._handler, None)
        if not self.status_handle:
            os._exit(2)
        self.set_status(SERVICE_START_PENDING, wait_hint=10000)
        threading.Thread(target=self._worker, name='worker', daemon=True).start()
        self.set_status(SERVICE_RUNNING)
        self.stop.wait()
        if self.failed is not None:
            # Not SERVICE_STOPPED: the SCM must see a failure and restart us.
            logging.shutdown()
            os._exit(1)
        self.set_status(SERVICE_STOPPED)

    def dispatch(self):
        table = (_TableEntry*2)(_TableEntry(self.name, self._main), _TableEntry(None, _MAIN()))
        if not self.api.StartServiceCtrlDispatcherW(table):
            error = ctypes.get_last_error()
            raise OSError(error, 'StartServiceCtrlDispatcher failed (not started by the SCM? use --console): '
                          + ctypes.FormatError(error).strip())


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--config', required=True)
    parser.add_argument('--service-name', default='LivestackWorkloadWorker')
    parser.add_argument('--console', action='store_true')
    args = parser.parse_args()
    if args.console:
        run_worker(args.config)
        return
    Service(args.service_name, args.config).dispatch()


if __name__ == '__main__':
    main()
