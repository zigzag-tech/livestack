"""Windows process facts and Job Objects for supervision (kernel32 via ctypes).

Stdlib only and importable as a sibling module: `bounded_exec.py` runs as a
script and imports it by file name.

What this replaces from Linux: a cgroup. On Windows the attempt is a named Job
Object: the kernel puts every descendant of a member process in the job (no
breakaway is granted), enforces its commit limit, process count and CPU rate,
and terminates every member on TerminateJobObject. The job's name is the
deterministic identity a restarted worker uses to find it, the systemd unit
name's role (openspec/changes/windows-host-worker, design "Supervision").
"""
from __future__ import annotations

import ctypes
from ctypes import wintypes
import time

# winnt.h
JOB_OBJECT_ALL_ACCESS = 0x1F001F
JOB_OBJECT_LIMIT_ACTIVE_PROCESS = 0x00000008
JOB_OBJECT_LIMIT_PRIORITY_CLASS = 0x00000020
JOB_OBJECT_LIMIT_JOB_MEMORY = 0x00000200
JOB_OBJECT_LIMIT_DIE_ON_UNHANDLED_EXCEPTION = 0x00000400
JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x00002000
JOB_OBJECT_CPU_RATE_CONTROL_ENABLE = 0x1
JOB_OBJECT_CPU_RATE_CONTROL_HARD_CAP = 0x4
JOB_OBJECT_MSG_ACTIVE_PROCESS_LIMIT = 3
JOB_OBJECT_MSG_JOB_MEMORY_LIMIT = 10
BELOW_NORMAL_PRIORITY_CLASS = 0x00004000
JobObjectBasicAccountingInformation = 1
JobObjectBasicProcessIdList = 3
JobObjectAssociateCompletionPortInformation = 7
JobObjectExtendedLimitInformation = 9
JobObjectCpuRateControlInformation = 15
PROCESS_TERMINATE = 0x0001
PROCESS_SET_QUOTA = 0x0100
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
SYNCHRONIZE = 0x00100000
TH32CS_SNAPTHREAD = 0x00000004
THREAD_SUSPEND_RESUME = 0x0002
ERROR_FILE_NOT_FOUND = 2
ERROR_INVALID_PARAMETER = 87
STILL_ACTIVE = 259
INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value
# Bounds: one attempt's process list.
MAX_JOB_PIDS = 4096
JOB_PREFIX = 'Global\\livestack-harmony-work-'


class _IoCounters(ctypes.Structure):
    _fields_ = [(n, ctypes.c_ulonglong) for n in (
        'ReadOperationCount', 'WriteOperationCount', 'OtherOperationCount',
        'ReadTransferCount', 'WriteTransferCount', 'OtherTransferCount')]


class _BasicLimit(ctypes.Structure):
    _fields_ = [('PerProcessUserTimeLimit', ctypes.c_longlong), ('PerJobUserTimeLimit', ctypes.c_longlong),
                ('LimitFlags', wintypes.DWORD), ('MinimumWorkingSetSize', ctypes.c_size_t),
                ('MaximumWorkingSetSize', ctypes.c_size_t), ('ActiveProcessLimit', wintypes.DWORD),
                ('Affinity', ctypes.c_size_t), ('PriorityClass', wintypes.DWORD),
                ('SchedulingClass', wintypes.DWORD)]


class _ExtendedLimit(ctypes.Structure):
    _fields_ = [('BasicLimitInformation', _BasicLimit), ('IoInfo', _IoCounters),
                ('ProcessMemoryLimit', ctypes.c_size_t), ('JobMemoryLimit', ctypes.c_size_t),
                ('PeakProcessMemoryUsed', ctypes.c_size_t), ('PeakJobMemoryUsed', ctypes.c_size_t)]


class _BasicAccounting(ctypes.Structure):
    _fields_ = [('TotalUserTime', ctypes.c_longlong), ('TotalKernelTime', ctypes.c_longlong),
                ('ThisPeriodTotalUserTime', ctypes.c_longlong), ('ThisPeriodTotalKernelTime', ctypes.c_longlong),
                ('TotalPageFaultCount', wintypes.DWORD), ('TotalProcesses', wintypes.DWORD),
                ('ActiveProcesses', wintypes.DWORD), ('TotalTerminatedProcesses', wintypes.DWORD)]


class _CpuRate(ctypes.Structure):
    _fields_ = [('ControlFlags', wintypes.DWORD), ('CpuRate', wintypes.DWORD)]


class _PidList(ctypes.Structure):
    _fields_ = [('NumberOfAssignedProcesses', wintypes.DWORD), ('NumberOfProcessIdsInList', wintypes.DWORD),
                ('ProcessIdList', ctypes.c_size_t*MAX_JOB_PIDS)]


class _CompletionPort(ctypes.Structure):
    _fields_ = [('CompletionKey', ctypes.c_void_p), ('CompletionPort', wintypes.HANDLE)]


class _MemoryStatus(ctypes.Structure):
    _fields_ = [('dwLength', wintypes.DWORD), ('dwMemoryLoad', wintypes.DWORD),
                ('ullTotalPhys', ctypes.c_ulonglong), ('ullAvailPhys', ctypes.c_ulonglong),
                ('ullTotalPageFile', ctypes.c_ulonglong), ('ullAvailPageFile', ctypes.c_ulonglong),
                ('ullTotalVirtual', ctypes.c_ulonglong), ('ullAvailVirtual', ctypes.c_ulonglong),
                ('ullAvailExtendedVirtual', ctypes.c_ulonglong)]


class _ThreadEntry(ctypes.Structure):
    _fields_ = [('dwSize', wintypes.DWORD), ('cntUsage', wintypes.DWORD), ('th32ThreadID', wintypes.DWORD),
                ('th32OwnerProcessID', wintypes.DWORD), ('tpBasePri', wintypes.LONG),
                ('tpDeltaPri', wintypes.LONG), ('dwFlags', wintypes.DWORD)]


_K = None


def _k32():
    global _K
    if _K is None:
        k = ctypes.WinDLL('kernel32', use_last_error=True)
        H, B, D, P = wintypes.HANDLE, wintypes.BOOL, wintypes.DWORD, ctypes.c_void_p
        for name, res, args in [
                ('CreateJobObjectW', H, [P, wintypes.LPCWSTR]),
                ('OpenJobObjectW', H, [D, B, wintypes.LPCWSTR]),
                ('SetInformationJobObject', B, [H, ctypes.c_int, P, D]),
                ('QueryInformationJobObject', B, [H, ctypes.c_int, P, D, ctypes.POINTER(D)]),
                ('AssignProcessToJobObject', B, [H, H]),
                ('TerminateJobObject', B, [H, ctypes.c_uint]),
                ('IsProcessInJob', B, [H, H, ctypes.POINTER(B)]),
                ('OpenProcess', H, [D, B, D]),
                ('TerminateProcess', B, [H, ctypes.c_uint]),
                ('GetExitCodeProcess', B, [H, ctypes.POINTER(D)]),
                ('GetCurrentProcess', H, []),
                ('CloseHandle', B, [H]),
                ('CreateIoCompletionPort', H, [H, H, ctypes.c_size_t, D]),
                ('GetQueuedCompletionStatus', B, [H, ctypes.POINTER(D), ctypes.POINTER(ctypes.c_size_t),
                                                  ctypes.POINTER(P), D]),
                ('GlobalMemoryStatusEx', B, [ctypes.POINTER(_MemoryStatus)]),
                ('GetSystemTimes', B, [ctypes.POINTER(ctypes.c_ulonglong)]*3),
                ('CreateToolhelp32Snapshot', H, [D, D]),
                ('Thread32First', B, [H, ctypes.POINTER(_ThreadEntry)]),
                ('Thread32Next', B, [H, ctypes.POINTER(_ThreadEntry)]),
                ('OpenThread', H, [D, B, D]),
                ('ResumeThread', D, [H])]:
            fn = getattr(k, name)
            fn.restype, fn.argtypes = res, args
        _K = k
    return _K


def _fail(what):
    error = ctypes.get_last_error()
    raise OSError(error, f'{what} failed: {ctypes.FormatError(error).strip()}')


def job_name(prefix16, attempt_id):
    return JOB_PREFIX + prefix16 + '-' + attempt_id


class Job:
    """An owned handle on a named Job Object. Closing the last handle of a job
    created with kill_on_close terminates every member (a dead supervisor
    cannot leave the attempt running)."""

    def __init__(self, handle, name):
        self.handle, self.name = handle, name

    @classmethod
    def create(cls, name, *, memory_bytes, tasks, cpu, kill_on_close=True):
        """A NEW job with the attempt's limits; refuses an existing name."""
        k = _k32()
        ctypes.set_last_error(0)
        handle = k.CreateJobObjectW(None, name)
        if not handle:
            _fail('CreateJobObject')
        if ctypes.get_last_error() == 183:  # ERROR_ALREADY_EXISTS: someone else's job
            k.CloseHandle(handle)
            raise FileExistsError(name)
        job = cls(handle, name)
        try:
            job.set_limits(memory_bytes=memory_bytes, tasks=tasks, cpu=cpu, kill_on_close=kill_on_close)
        except BaseException:
            job.close()
            raise
        return job

    @classmethod
    def current(cls):
        """The calling process's own (innermost) job, for queries only."""
        return cls(None, None)

    @classmethod
    def open(cls, name):
        """The existing job, or None when no such job exists."""
        handle = _k32().OpenJobObjectW(JOB_OBJECT_ALL_ACCESS, False, name)
        if not handle:
            if ctypes.get_last_error() in (ERROR_FILE_NOT_FOUND, ERROR_INVALID_PARAMETER):
                return None
            _fail('OpenJobObject')
        return cls(handle, name)

    def set_limits(self, *, memory_bytes, tasks, cpu, kill_on_close=True):
        import os
        info = _ExtendedLimit()
        flags = (JOB_OBJECT_LIMIT_JOB_MEMORY | JOB_OBJECT_LIMIT_ACTIVE_PROCESS |
                 JOB_OBJECT_LIMIT_DIE_ON_UNHANDLED_EXCEPTION | JOB_OBJECT_LIMIT_PRIORITY_CLASS)
        if kill_on_close:
            flags |= JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        info.BasicLimitInformation.LimitFlags = flags
        info.BasicLimitInformation.ActiveProcessLimit = int(tasks)
        # Below normal so interactive tenants of the host keep priority.
        info.BasicLimitInformation.PriorityClass = BELOW_NORMAL_PRIORITY_CLASS
        info.JobMemoryLimit = int(memory_bytes)
        if not _k32().SetInformationJobObject(self.handle, JobObjectExtendedLimitInformation,
                                              ctypes.byref(info), ctypes.sizeof(info)):
            _fail('SetInformationJobObject(limits)')
        # CpuRate is in 1/100 % of the WHOLE machine: N cores of C = N/C*10000.
        rate = _CpuRate(JOB_OBJECT_CPU_RATE_CONTROL_ENABLE | JOB_OBJECT_CPU_RATE_CONTROL_HARD_CAP,
                        max(1, min(10000, int(round(float(cpu)/(os.cpu_count() or 1)*10000)))))
        if not _k32().SetInformationJobObject(self.handle, JobObjectCpuRateControlInformation,
                                              ctypes.byref(rate), ctypes.sizeof(rate)):
            _fail('SetInformationJobObject(cpu rate)')

    def limits(self):
        """What the KERNEL enforces for this job (not what a file claims)."""
        info, rate = _ExtendedLimit(), _CpuRate()
        k = _k32()
        if not k.QueryInformationJobObject(self.handle, JobObjectExtendedLimitInformation,
                                           ctypes.byref(info), ctypes.sizeof(info), None):
            _fail('QueryInformationJobObject(limits)')
        if not k.QueryInformationJobObject(self.handle, JobObjectCpuRateControlInformation,
                                           ctypes.byref(rate), ctypes.sizeof(rate), None):
            _fail('QueryInformationJobObject(cpu rate)')
        flags = info.BasicLimitInformation.LimitFlags
        return dict(
            memory_bytes=info.JobMemoryLimit if flags & JOB_OBJECT_LIMIT_JOB_MEMORY else None,
            tasks=info.BasicLimitInformation.ActiveProcessLimit if flags & JOB_OBJECT_LIMIT_ACTIVE_PROCESS else None,
            cpu_rate=rate.CpuRate if rate.ControlFlags & JOB_OBJECT_CPU_RATE_CONTROL_HARD_CAP else None,
            kill_on_close=bool(flags & JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE),
            peak_memory_bytes=info.PeakJobMemoryUsed)

    def accounting(self):
        info = _BasicAccounting()
        if not _k32().QueryInformationJobObject(self.handle, JobObjectBasicAccountingInformation,
                                                ctypes.byref(info), ctypes.sizeof(info), None):
            _fail('QueryInformationJobObject(accounting)')
        return dict(active=info.ActiveProcesses, total=info.TotalProcesses,
                    cpu_usage_usec=(info.TotalUserTime+info.TotalKernelTime)//10)

    def pids(self):
        info = _PidList()
        if not _k32().QueryInformationJobObject(self.handle, JobObjectBasicProcessIdList,
                                                ctypes.byref(info), ctypes.sizeof(info), None):
            _fail('QueryInformationJobObject(pids)')
        return [int(info.ProcessIdList[i]) for i in range(min(info.NumberOfProcessIdsInList, MAX_JOB_PIDS))]

    def assign(self, process_handle):
        if not _k32().AssignProcessToJobObject(self.handle, process_handle):
            _fail('AssignProcessToJobObject')

    def contains(self, process_handle):
        result = wintypes.BOOL()
        if not _k32().IsProcessInJob(process_handle, self.handle, ctypes.byref(result)):
            _fail('IsProcessInJob')
        return bool(result.value)

    def terminate(self, code=1):
        if not _k32().TerminateJobObject(self.handle, code):
            _fail('TerminateJobObject')

    def kill_others(self, keep, *, rounds=20):
        """Terminate every member not in `keep`; True once only those remain."""
        for _ in range(rounds):
            others = [p for p in self.pids() if p not in keep]
            if not others:
                return True
            for pid in others:
                terminate_pid(pid)
            time.sleep(.05)
        return not [p for p in self.pids() if p not in keep]

    def watch(self):
        """A completion port receiving the kernel's limit notifications."""
        k = _k32()
        port = k.CreateIoCompletionPort(INVALID_HANDLE_VALUE, None, 0, 1)
        if not port:
            _fail('CreateIoCompletionPort')
        info = _CompletionPort(None, port)
        if not k.SetInformationJobObject(self.handle, JobObjectAssociateCompletionPortInformation,
                                         ctypes.byref(info), ctypes.sizeof(info)):
            k.CloseHandle(port)
            _fail('SetInformationJobObject(completion port)')
        return port

    def close(self):
        if self.handle:
            _k32().CloseHandle(self.handle)
            self.handle = None


def limit_messages(port):
    """Drain pending notifications without blocking: a set of message ids."""
    k = _k32()
    found = set()
    for _ in range(256):
        message, key, overlapped = wintypes.DWORD(), ctypes.c_size_t(), ctypes.c_void_p()
        if not k.GetQueuedCompletionStatus(port, ctypes.byref(message), ctypes.byref(key),
                                           ctypes.byref(overlapped), 0):
            break
        found.add(message.value)
    return found


def close_handle(handle):
    if handle:
        _k32().CloseHandle(handle)


def open_process(pid, access=PROCESS_QUERY_LIMITED_INFORMATION | SYNCHRONIZE):
    handle = _k32().OpenProcess(access, False, int(pid))
    return handle or None


def terminate_pid(pid, code=1):
    handle = open_process(pid, PROCESS_TERMINATE | PROCESS_QUERY_LIMITED_INFORMATION)
    if handle is None:
        return False
    try:
        return bool(_k32().TerminateProcess(handle, code))
    finally:
        close_handle(handle)


def pid_alive(pid):
    handle = open_process(pid)
    if handle is None:
        return False
    try:
        code = wintypes.DWORD()
        return bool(_k32().GetExitCodeProcess(handle, ctypes.byref(code))) and code.value == STILL_ACTIVE
    finally:
        close_handle(handle)


def resume_process(pid):
    """Resume every thread of a process created CREATE_SUSPENDED."""
    k = _k32()
    snapshot = k.CreateToolhelp32Snapshot(TH32CS_SNAPTHREAD, 0)
    if snapshot == INVALID_HANDLE_VALUE or not snapshot:
        _fail('CreateToolhelp32Snapshot')
    resumed = 0
    try:
        entry = _ThreadEntry()
        entry.dwSize = ctypes.sizeof(entry)
        ok = k.Thread32First(snapshot, ctypes.byref(entry))
        while ok:
            if entry.th32OwnerProcessID == pid:
                thread = k.OpenThread(THREAD_SUSPEND_RESUME, False, entry.th32ThreadID)
                if not thread:
                    _fail('OpenThread')
                try:
                    if k.ResumeThread(thread) == 0xFFFFFFFF:
                        _fail('ResumeThread')
                    resumed += 1
                finally:
                    k.CloseHandle(thread)
            ok = k.Thread32Next(snapshot, ctypes.byref(entry))
    finally:
        k.CloseHandle(snapshot)
    if not resumed:
        raise OSError(f'process {pid} has no thread to resume')


def memory_status():
    """{'total', 'available'} physical bytes from GlobalMemoryStatusEx. A WSL or
    Hyper-V VM's memory is already IN USE here: vmmem is a consumer like any
    other process on the physical host."""
    status = _MemoryStatus()
    status.dwLength = ctypes.sizeof(status)
    if not _k32().GlobalMemoryStatusEx(ctypes.byref(status)):
        _fail('GlobalMemoryStatusEx')
    return {'total': status.ullTotalPhys, 'available': status.ullAvailPhys}


def system_times():
    """(idle, busy) 100 ns ticks summed over all CPUs since boot."""
    idle, kernel, user = ctypes.c_ulonglong(), ctypes.c_ulonglong(), ctypes.c_ulonglong()
    if not _k32().GetSystemTimes(ctypes.byref(idle), ctypes.byref(kernel), ctypes.byref(user)):
        _fail('GetSystemTimes')
    # Kernel time includes idle time.
    return idle.value, kernel.value+user.value-idle.value


class CpuLoad:
    """Busy CPUs averaged since the previous sample (Windows has no loadavg).
    The first sample has no interval and reports None: unknown, never zero."""

    def __init__(self):
        self.previous = None

    def sample(self, cpus):
        now = system_times()
        previous, self.previous = self.previous, now
        if previous is None:
            return None
        idle, busy = now[0]-previous[0], now[1]-previous[1]
        if idle+busy <= 0:
            return None
        return cpus*busy/(idle+busy)
