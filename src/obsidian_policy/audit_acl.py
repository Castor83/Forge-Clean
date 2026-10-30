"""Windows-native DACL hardening for Obsidian audit storage."""

from __future__ import annotations

import ctypes
from ctypes import wintypes
import os
from pathlib import Path
import re


_OWNER_SECURITY_INFORMATION = 0x00000001
_DACL_SECURITY_INFORMATION = 0x00000004
_PROTECTED_DACL_SECURITY_INFORMATION = 0x80000000
_SE_FILE_OBJECT = 1
_SDDL_REVISION_1 = 1
_TOKEN_QUERY = 0x0008
_TOKEN_USER = 1
_ERROR_INSUFFICIENT_BUFFER = 122
_FILE_ATTRIBUTE_REPARSE_POINT = 0x00000400
_INVALID_FILE_ATTRIBUTES = 0xFFFFFFFF
_RECOVERY_MARKER_SUFFIX = ".pending"
_GENERIC_WRITE = 0x40000000
_FILE_SHARE_READ = 0x00000001
_FILE_SHARE_WRITE = 0x00000002
_FILE_SHARE_DELETE = 0x00000004
_OPEN_EXISTING = 3
_FILE_FLAG_BACKUP_SEMANTICS = 0x02000000
_SYSTEM_SID = "S-1-5-18"


class AuditAclError(RuntimeError):
    """The audit path cannot be proven to have the required Windows DACL."""


def harden_audit_storage(path: Path) -> None:
    """Create if needed, apply, and verify strict DACLs for an audit path."""
    _require_windows_path(path)
    if not path.parent.is_dir():
        raise AuditAclError("audit parent directory must exist")
    _reject_reparse_point(path.parent, "audit parent directory")
    _require_dedicated_directory(path)
    if path.is_symlink():
        raise AuditAclError("audit file must not be a reparse point")
    if path.exists():
        if not path.is_file():
            raise AuditAclError("audit path must be a regular file")
        _reject_reparse_point(path, "audit file")

    user_sid = _current_user_sid()
    _verify_owner(path.parent, user_sid)
    if path.exists():
        _verify_owner(path, user_sid)
    _set_strict_dacl(path.parent, user_sid, is_directory=True)
    _verify_strict_dacl(path.parent, user_sid, is_directory=True)

    if not path.exists():
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
        try:
            descriptor = os.open(path, flags, 0o600)
        except OSError as exc:
            raise AuditAclError("cannot create the hardened audit file") from exc
        else:
            os.close(descriptor)
    _reject_reparse_point(path, "audit file")
    _verify_owner(path, user_sid)
    _set_strict_dacl(path, user_sid, is_directory=False)
    _verify_strict_dacl(path, user_sid, is_directory=False)


def ensure_audit_storage_is_secure(path: Path) -> None:
    """Initialize a new audit path or fail if an existing DACL has changed."""
    _require_windows_path(path)
    marker = _recovery_marker_path(path)
    if (marker.exists() or marker.is_symlink()) and not path.exists():
        raise AuditAclError("audit file is missing while recovery is pending")
    if path.exists():
        verify_audit_storage_acl(path)
        return
    harden_audit_storage(path)


def verify_audit_storage_acl(path: Path) -> bool:
    """Return true only when directory and file have the exact required DACL."""
    _require_windows_path(path)
    if not path.parent.is_dir() or not path.is_file():
        raise AuditAclError("audit directory and file must exist")
    _reject_reparse_point(path.parent, "audit parent directory")
    _require_dedicated_directory(path)
    _reject_reparse_point(path, "audit file")
    user_sid = _current_user_sid()
    _verify_strict_dacl(path.parent, user_sid, is_directory=True)
    _verify_strict_dacl(path, user_sid, is_directory=False)
    return True


def harden_audit_recovery_marker(path: Path) -> None:
    """Apply and verify the strict file DACL on an existing recovery marker."""
    marker = _recovery_marker_path(path)
    if not marker.is_file() or marker.is_symlink():
        raise AuditAclError("audit recovery marker must be a regular file")
    _reject_reparse_point(marker, "audit recovery marker")
    user_sid = _current_user_sid()
    _verify_owner(marker, user_sid)
    _set_strict_dacl(marker, user_sid, is_directory=False)
    _verify_strict_dacl(marker, user_sid, is_directory=False)


def verify_audit_recovery_marker_acl(path: Path) -> bool:
    """Verify an existing recovery marker without repairing its DACL."""
    marker = _recovery_marker_path(path)
    if not marker.is_file() or marker.is_symlink():
        raise AuditAclError("audit recovery marker must be a regular file")
    _reject_reparse_point(marker, "audit recovery marker")
    _verify_strict_dacl(marker, _current_user_sid(), is_directory=False)
    return True


def flush_audit_directory(path: Path) -> None:
    """Flush the dedicated directory after creating the write-ahead marker."""
    _require_windows_path(path)
    _advapi32, kernel32 = _libraries()
    handle = kernel32.CreateFileW(
        str(path.parent),
        _GENERIC_WRITE,
        _FILE_SHARE_READ | _FILE_SHARE_WRITE | _FILE_SHARE_DELETE,
        None,
        _OPEN_EXISTING,
        _FILE_FLAG_BACKUP_SEMANTICS,
        None,
    )
    invalid_handle = ctypes.c_void_p(-1).value
    if handle == invalid_handle:
        raise AuditAclError(_windows_error("cannot open the audit directory"))
    try:
        if not kernel32.FlushFileBuffers(handle):
            raise AuditAclError(_windows_error("cannot flush the audit directory"))
    finally:
        kernel32.CloseHandle(handle)


def _require_windows_path(path: Path) -> None:
    if not isinstance(path, Path):
        raise AuditAclError("audit path must be a pathlib.Path")
    if os.name != "nt":
        raise AuditAclError("Windows DACL support is required for audit storage")


def _require_dedicated_directory(path: Path) -> None:
    allowed_entries = {path, _recovery_marker_path(path)}
    other_entries = [
        entry for entry in path.parent.iterdir() if entry not in allowed_entries
    ]
    if other_entries:
        raise AuditAclError(
            "audit parent directory must be dedicated to the audit file"
        )


def _recovery_marker_path(path: Path) -> Path:
    return path.with_name(path.name + _RECOVERY_MARKER_SUFFIX)


def _current_user_sid() -> str:
    advapi32, kernel32 = _libraries()
    token = wintypes.HANDLE()
    if not advapi32.OpenProcessToken(
        kernel32.GetCurrentProcess(),
        _TOKEN_QUERY,
        ctypes.byref(token),
    ):
        raise AuditAclError(_windows_error("cannot open the current process token"))
    try:
        required = wintypes.DWORD()
        advapi32.GetTokenInformation(
            token,
            _TOKEN_USER,
            None,
            0,
            ctypes.byref(required),
        )
        if ctypes.get_last_error() != _ERROR_INSUFFICIENT_BUFFER:
            raise AuditAclError(_windows_error("cannot size the current user SID"))
        buffer = ctypes.create_string_buffer(required.value)
        if not advapi32.GetTokenInformation(
            token,
            _TOKEN_USER,
            buffer,
            required,
            ctypes.byref(required),
        ):
            raise AuditAclError(_windows_error("cannot read the current user SID"))
        sid_pointer = ctypes.cast(buffer, ctypes.POINTER(ctypes.c_void_p))[0]
        sid_text = wintypes.LPWSTR()
        if not advapi32.ConvertSidToStringSidW(
            sid_pointer,
            ctypes.byref(sid_text),
        ):
            raise AuditAclError(_windows_error("cannot format the current user SID"))
        try:
            return sid_text.value
        finally:
            kernel32.LocalFree(sid_text)
    finally:
        kernel32.CloseHandle(token)


def _set_strict_dacl(path: Path, user_sid: str, *, is_directory: bool) -> None:
    inheritance = "OICI" if is_directory else ""
    sddl = (
        f"D:P(A;{inheritance};FA;;;SY)"
        f"(A;{inheritance};FA;;;{user_sid})"
    )
    _set_dacl_sddl(path, sddl)


def _set_dacl_sddl(path: Path, sddl: str) -> None:
    advapi32, kernel32 = _libraries()
    descriptor = ctypes.c_void_p()
    if not advapi32.ConvertStringSecurityDescriptorToSecurityDescriptorW(
        sddl,
        _SDDL_REVISION_1,
        ctypes.byref(descriptor),
        None,
    ):
        raise AuditAclError(_windows_error("cannot build the required audit DACL"))
    try:
        security_information = (
            _DACL_SECURITY_INFORMATION | _PROTECTED_DACL_SECURITY_INFORMATION
        )
        if not advapi32.SetFileSecurityW(
            str(path),
            security_information,
            descriptor,
        ):
            raise AuditAclError(_windows_error(f"cannot harden audit ACL: {path}"))
    finally:
        kernel32.LocalFree(descriptor)


def _verify_strict_dacl(path: Path, user_sid: str, *, is_directory: bool) -> None:
    _verify_owner(path, user_sid)
    actual = _read_dacl_sddl(path)
    dacl_prefix, separator, ace_text = actual.partition("(")
    if separator != "(" or "P" not in dacl_prefix:
        raise AuditAclError(f"audit DACL is not protected: {path}")
    aces = re.findall(r"\(([^()]*)\)", "(" + ace_text)
    expected_flags = "OICI" if is_directory else ""
    expected_trustees = {"SY", user_sid}
    actual_trustees: set[str] = set()
    if len(aces) != 2:
        raise AuditAclError(f"audit DACL must contain exactly two ACEs: {path}")
    for ace in aces:
        fields = ace.split(";")
        if len(fields) != 6:
            raise AuditAclError(f"audit DACL contains an invalid ACE: {path}")
        ace_type, flags, rights, object_guid, inherit_guid, trustee = fields
        if (
            ace_type != "A"
            or flags != expected_flags
            or rights not in {"FA", "0x1f01ff"}
            or object_guid
            or inherit_guid
            or trustee not in expected_trustees
        ):
            raise AuditAclError(f"audit DACL contains an unexpected ACE: {path}")
        actual_trustees.add(trustee)
    if actual_trustees != expected_trustees:
        raise AuditAclError(f"audit DACL trustees do not match policy: {path}")


def _verify_owner(path: Path, user_sid: str) -> None:
    # Owners retain implicit WRITE_DAC even without an allow ACE in the DACL.
    if _read_owner_sid(path) not in {user_sid, _SYSTEM_SID}:
        raise AuditAclError(f"audit owner must be the current user or SYSTEM: {path}")


def _read_owner_sid(path: Path) -> str:
    advapi32, kernel32 = _libraries()
    owner = ctypes.c_void_p()
    descriptor = ctypes.c_void_p()
    result = advapi32.GetNamedSecurityInfoW(
        str(path),
        _SE_FILE_OBJECT,
        _OWNER_SECURITY_INFORMATION,
        ctypes.byref(owner),
        None,
        None,
        None,
        ctypes.byref(descriptor),
    )
    if result != 0:
        raise AuditAclError(f"cannot read audit owner: Windows error {result}")
    try:
        sid_text = wintypes.LPWSTR()
        if not advapi32.ConvertSidToStringSidW(owner, ctypes.byref(sid_text)):
            raise AuditAclError(_windows_error("cannot format the audit owner SID"))
        try:
            return sid_text.value
        finally:
            kernel32.LocalFree(sid_text)
    finally:
        kernel32.LocalFree(descriptor)


def _read_dacl_sddl(path: Path) -> str:
    advapi32, kernel32 = _libraries()
    dacl = ctypes.c_void_p()
    descriptor = ctypes.c_void_p()
    result = advapi32.GetNamedSecurityInfoW(
        str(path),
        _SE_FILE_OBJECT,
        _DACL_SECURITY_INFORMATION,
        None,
        None,
        ctypes.byref(dacl),
        None,
        ctypes.byref(descriptor),
    )
    if result != 0:
        raise AuditAclError(f"cannot read audit DACL: Windows error {result}")
    try:
        sddl = wintypes.LPWSTR()
        if not advapi32.ConvertSecurityDescriptorToStringSecurityDescriptorW(
            descriptor,
            _SDDL_REVISION_1,
            _DACL_SECURITY_INFORMATION,
            ctypes.byref(sddl),
            None,
        ):
            raise AuditAclError(_windows_error("cannot format the audit DACL"))
        try:
            return sddl.value
        finally:
            kernel32.LocalFree(sddl)
    finally:
        kernel32.LocalFree(descriptor)


def _reject_reparse_point(path: Path, label: str) -> None:
    _advapi32, kernel32 = _libraries()
    attributes = kernel32.GetFileAttributesW(str(path))
    if attributes == _INVALID_FILE_ATTRIBUTES:
        raise AuditAclError(_windows_error(f"cannot inspect {label}"))
    if attributes & _FILE_ATTRIBUTE_REPARSE_POINT:
        raise AuditAclError(f"{label} must not be a reparse point")


def _windows_error(message: str) -> str:
    return f"{message}: {ctypes.WinError(ctypes.get_last_error())}"


def _libraries():
    if os.name != "nt":
        raise AuditAclError("Windows DACL support is required for audit storage")
    advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    _configure_signatures(advapi32, kernel32)
    return advapi32, kernel32


def _configure_signatures(advapi32, kernel32) -> None:
    advapi32.OpenProcessToken.argtypes = (
        wintypes.HANDLE,
        wintypes.DWORD,
        ctypes.POINTER(wintypes.HANDLE),
    )
    advapi32.OpenProcessToken.restype = wintypes.BOOL
    advapi32.GetTokenInformation.argtypes = (
        wintypes.HANDLE,
        ctypes.c_int,
        ctypes.c_void_p,
        wintypes.DWORD,
        ctypes.POINTER(wintypes.DWORD),
    )
    advapi32.GetTokenInformation.restype = wintypes.BOOL
    advapi32.ConvertSidToStringSidW.argtypes = (
        ctypes.c_void_p,
        ctypes.POINTER(wintypes.LPWSTR),
    )
    advapi32.ConvertSidToStringSidW.restype = wintypes.BOOL
    advapi32.ConvertStringSecurityDescriptorToSecurityDescriptorW.argtypes = (
        wintypes.LPCWSTR,
        wintypes.DWORD,
        ctypes.POINTER(ctypes.c_void_p),
        ctypes.POINTER(wintypes.DWORD),
    )
    advapi32.ConvertStringSecurityDescriptorToSecurityDescriptorW.restype = (
        wintypes.BOOL
    )
    advapi32.SetFileSecurityW.argtypes = (
        wintypes.LPCWSTR,
        wintypes.DWORD,
        ctypes.c_void_p,
    )
    advapi32.SetFileSecurityW.restype = wintypes.BOOL
    advapi32.GetNamedSecurityInfoW.argtypes = (
        wintypes.LPWSTR,
        ctypes.c_int,
        wintypes.DWORD,
        ctypes.POINTER(ctypes.c_void_p),
        ctypes.POINTER(ctypes.c_void_p),
        ctypes.POINTER(ctypes.c_void_p),
        ctypes.POINTER(ctypes.c_void_p),
        ctypes.POINTER(ctypes.c_void_p),
    )
    advapi32.GetNamedSecurityInfoW.restype = wintypes.DWORD
    advapi32.ConvertSecurityDescriptorToStringSecurityDescriptorW.argtypes = (
        ctypes.c_void_p,
        wintypes.DWORD,
        wintypes.DWORD,
        ctypes.POINTER(wintypes.LPWSTR),
        ctypes.POINTER(wintypes.DWORD),
    )
    advapi32.ConvertSecurityDescriptorToStringSecurityDescriptorW.restype = (
        wintypes.BOOL
    )
    kernel32.GetCurrentProcess.restype = wintypes.HANDLE
    kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)
    kernel32.CloseHandle.restype = wintypes.BOOL
    kernel32.LocalFree.argtypes = (ctypes.c_void_p,)
    kernel32.LocalFree.restype = ctypes.c_void_p
    kernel32.GetFileAttributesW.argtypes = (wintypes.LPCWSTR,)
    kernel32.GetFileAttributesW.restype = wintypes.DWORD
    kernel32.CreateFileW.argtypes = (
        wintypes.LPCWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
        ctypes.c_void_p,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.HANDLE,
    )
    kernel32.CreateFileW.restype = wintypes.HANDLE
    kernel32.FlushFileBuffers.argtypes = (wintypes.HANDLE,)
    kernel32.FlushFileBuffers.restype = wintypes.BOOL
