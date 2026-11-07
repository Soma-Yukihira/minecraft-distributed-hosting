"""Windows Job Object: the Minecraft process dies with the agent, and its CPU can be capped.

``JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE``: when the last handle to the job closes - including when
the agent process crashes or is killed - Windows terminates every process in the job. A
Minecraft server can therefore never keep running (and writing the world) without the agent
that renews its lease. This is a key part of split-brain prevention.
"""

from __future__ import annotations

import ctypes
import sys
from ctypes import wintypes

JobObjectExtendedLimitInformation = 9
JobObjectCpuRateControlInformation = 15
JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x2000
JOB_OBJECT_CPU_RATE_CONTROL_ENABLE = 0x1
JOB_OBJECT_CPU_RATE_CONTROL_HARD_CAP = 0x4
PROCESS_SET_QUOTA = 0x0100
PROCESS_TERMINATE = 0x0001
PROCESS_SUSPEND_RESUME = 0x0800
#: Start the process suspended so it is inside the job before running a single instruction
#: (otherwise anything it spawns in the meantime would escape the job).
CREATE_SUSPENDED = 0x00000004


class _BasicLimit(ctypes.Structure):
    _fields_ = [
        ("PerProcessUserTimeLimit", ctypes.c_int64),
        ("PerJobUserTimeLimit", ctypes.c_int64),
        ("LimitFlags", wintypes.DWORD),
        ("MinimumWorkingSetSize", ctypes.c_size_t),
        ("MaximumWorkingSetSize", ctypes.c_size_t),
        ("ActiveProcessLimit", wintypes.DWORD),
        ("Affinity", ctypes.c_size_t),
        ("PriorityClass", wintypes.DWORD),
        ("SchedulingClass", wintypes.DWORD),
    ]


class _IoCounters(ctypes.Structure):
    _fields_ = [(name, ctypes.c_uint64) for name in (
        "ReadOperationCount", "WriteOperationCount", "OtherOperationCount",
        "ReadTransferCount", "WriteTransferCount", "OtherTransferCount")]


class _ExtendedLimit(ctypes.Structure):
    _fields_ = [
        ("BasicLimitInformation", _BasicLimit),
        ("IoInfo", _IoCounters),
        ("ProcessMemoryLimit", ctypes.c_size_t),
        ("JobMemoryLimit", ctypes.c_size_t),
        ("PeakProcessMemoryUsed", ctypes.c_size_t),
        ("PeakJobMemoryUsed", ctypes.c_size_t),
    ]


class _CpuRate(ctypes.Structure):
    _fields_ = [("ControlFlags", wintypes.DWORD), ("CpuRate", wintypes.DWORD)]


class JobError(OSError):
    pass


class JobObject:
    def __init__(self, *, cpu_limit_percent: int | None = None) -> None:
        if sys.platform != "win32":
            raise JobError("Job Objects are Windows-only")
        self._k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        self._k32.CreateJobObjectW.restype = wintypes.HANDLE
        self._k32.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
        self._k32.SetInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p,
                                                     wintypes.DWORD]
        self._k32.OpenProcess.restype = wintypes.HANDLE
        self._k32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        self._k32.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
        self._k32.CloseHandle.argtypes = [wintypes.HANDLE]
        self._k32.TerminateJobObject.argtypes = [wintypes.HANDLE, wintypes.UINT]

        self.handle = self._k32.CreateJobObjectW(None, None)
        if not self.handle:
            raise JobError(f"CreateJobObject failed: {ctypes.get_last_error()}")
        info = _ExtendedLimit()
        info.BasicLimitInformation.LimitFlags = JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        self._set(JobObjectExtendedLimitInformation, info)
        if cpu_limit_percent is not None:
            if not 5 <= cpu_limit_percent <= 100:
                raise ValueError("CPU limit must be between 5 and 100 %")
            rate = _CpuRate(JOB_OBJECT_CPU_RATE_CONTROL_ENABLE | JOB_OBJECT_CPU_RATE_CONTROL_HARD_CAP,
                            cpu_limit_percent * 100)
            self._set(JobObjectCpuRateControlInformation, rate)

    def _set(self, info_class: int, struct: ctypes.Structure) -> None:
        if not self._k32.SetInformationJobObject(self.handle, info_class, ctypes.byref(struct),
                                                 ctypes.sizeof(struct)):
            raise JobError(f"SetInformationJobObject({info_class}) failed: {ctypes.get_last_error()}")

    def assign(self, pid: int) -> None:
        process = self._k32.OpenProcess(PROCESS_SET_QUOTA | PROCESS_TERMINATE, False, pid)
        if not process:
            raise JobError(f"OpenProcess({pid}) failed: {ctypes.get_last_error()}")
        try:
            if not self._k32.AssignProcessToJobObject(self.handle, process):
                raise JobError(f"AssignProcessToJobObject failed: {ctypes.get_last_error()}")
        finally:
            self._k32.CloseHandle(process)

    def assign_and_resume(self, pid: int) -> None:
        """For a process created with ``CREATE_SUSPENDED``: put it in the job, then let it run."""
        self.assign(pid)
        ntdll = ctypes.WinDLL("ntdll")
        ntdll.NtResumeProcess.argtypes = [wintypes.HANDLE]
        process = self._k32.OpenProcess(PROCESS_SUSPEND_RESUME, False, pid)
        if not process:
            raise JobError(f"OpenProcess({pid}) for resume failed: {ctypes.get_last_error()}")
        try:
            status = ntdll.NtResumeProcess(process)
            if status != 0:
                raise JobError(f"NtResumeProcess failed: 0x{status & 0xFFFFFFFF:08x}")
        finally:
            self._k32.CloseHandle(process)

    def terminate(self, exit_code: int = 1) -> None:
        if self.handle:
            self._k32.TerminateJobObject(self.handle, exit_code)

    def close(self) -> None:
        """Closing the last handle kills every process still in the job."""
        if self.handle:
            self._k32.CloseHandle(self.handle)
            self.handle = None

    def __del__(self) -> None:  # pragma: no cover - safety net
        try:
            self.close()
        except Exception:  # noqa: BLE001
            pass
