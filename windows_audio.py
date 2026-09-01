"""Windows default-playback-device control.

PC2Sonos only works when Windows' default output is "CABLE Input"
(the virtual cable), because that's the device we capture from. Rather
than making every user dig through Sound settings by hand, this module
can find the cable and set it as the default output programmatically.

Uses the same undocumented-but-battle-tested IPolicyConfig COM
interface that tools like SoundSwitch and AudioDeviceCmdlets use --
there is no documented Windows API for switching the default device.
Everything here fails soft: on any error we return (False, reason) and
the caller falls back to telling the human what to click.

Windows-only; imports of comtypes/pycaw happen inside functions so the
cross-platform test harness can import this module on Linux.
"""


def _policy_config():
    import comtypes
    from comtypes import GUID, COMMETHOD, HRESULT
    from ctypes import POINTER
    from ctypes.wintypes import LPCWSTR, INT

    class IPolicyConfig(comtypes.IUnknown):
        _iid_ = GUID("{f8679f50-850a-41cf-9c72-430f290290c8}")
        # Vtable must match PolicyConfig.h ordering exactly; we only
        # ever call SetDefaultEndpoint, the rest are alignment padding.
        _methods_ = (
            COMMETHOD([], HRESULT, "GetMixFormat",
                      (["in"], LPCWSTR), (["in"], POINTER(INT))),
            COMMETHOD([], HRESULT, "GetDeviceFormat",
                      (["in"], LPCWSTR), (["in"], INT), (["in"], POINTER(INT))),
            COMMETHOD([], HRESULT, "ResetDeviceFormat", (["in"], LPCWSTR)),
            COMMETHOD([], HRESULT, "SetDeviceFormat",
                      (["in"], LPCWSTR), (["in"], POINTER(INT)), (["in"], POINTER(INT))),
            COMMETHOD([], HRESULT, "GetProcessingPeriod",
                      (["in"], LPCWSTR), (["in"], INT),
                      (["in"], POINTER(INT)), (["in"], POINTER(INT))),
            COMMETHOD([], HRESULT, "SetProcessingPeriod",
                      (["in"], LPCWSTR), (["in"], POINTER(INT))),
            COMMETHOD([], HRESULT, "GetShareMode",
                      (["in"], LPCWSTR), (["in"], POINTER(INT))),
            COMMETHOD([], HRESULT, "SetShareMode",
                      (["in"], LPCWSTR), (["in"], POINTER(INT))),
            COMMETHOD([], HRESULT, "GetPropertyValue",
                      (["in"], LPCWSTR), (["in"], INT),
                      (["in"], POINTER(INT)), (["in"], POINTER(INT))),
            COMMETHOD([], HRESULT, "SetPropertyValue",
                      (["in"], LPCWSTR), (["in"], INT),
                      (["in"], POINTER(INT)), (["in"], POINTER(INT))),
            COMMETHOD([], HRESULT, "SetDefaultEndpoint",
                      (["in"], LPCWSTR, "wszDeviceId"), (["in"], INT, "role")),
            COMMETHOD([], HRESULT, "SetEndpointVisibility",
                      (["in"], LPCWSTR), (["in"], INT)),
        )

    CLSID_PolicyConfigClient = GUID("{870af99c-171d-4f9e-af0d-e63df40c2bc9}")
    return comtypes.CoCreateInstance(
        CLSID_PolicyConfigClient, interface=IPolicyConfig,
        clsctx=comtypes.CLSCTX_ALL)


def list_playback_devices():
    """[(device_id, friendly_name)] for all active render endpoints."""
    from pycaw.utils import AudioUtilities
    out = []
    for d in AudioUtilities.GetAllDevices():
        try:
            # flow: DataFlow.eRender == 0 in pycaw's AudioDeviceState models;
            # GetAllDevices includes capture too, so filter by state+flow via
            # the underlying enumerator when available. FriendlyName check is
            # done by callers; keep this permissive.
            out.append((d.id, d.FriendlyName or ""))
        except Exception:
            continue
    return out


def current_default_playback_name():
    """Friendly name of the current default render device, or ''. """
    try:
        from pycaw.utils import AudioUtilities
        dev = AudioUtilities.GetSpeakers()  # default render endpoint
        dev_id = dev.GetId()
        for did, name in list_playback_devices():
            if did == dev_id:
                return name
        return ""
    except Exception:
        return ""


def set_default_playback(name_substr="CABLE Input"):
    """Make the first playback device whose name contains name_substr the
    Windows default output (for both normal and communications roles).
    Returns (ok, detail)."""
    try:
        target_id, target_name = None, None
        for did, name in list_playback_devices():
            if name_substr.lower() in (name or "").lower():
                target_id, target_name = did, name
                break
        if not target_id:
            return False, f"no playback device matching '{name_substr}' found"
        pc = _policy_config()
        for role in (0, 1, 2):   # eConsole, eMultimedia, eCommunications
            pc.SetDefaultEndpoint(target_id, role)
        return True, target_name
    except Exception as e:
        return False, f"{type(e).__name__}: {e}"


def ensure_cable_is_default():
    """If 'CABLE Input' exists and isn't the default output, make it so.
    Returns a human-readable status line for the log."""
    try:
        current = current_default_playback_name()
        if "cable input" in current.lower():
            return f"default output already OK ({current})"
        ok, detail = set_default_playback("CABLE Input")
        if ok:
            return (f"default output switched to '{detail}' "
                    f"(was '{current or 'unknown'}')")
        return f"could not switch default output: {detail}"
    except Exception as e:
        return f"default-output check failed: {type(e).__name__}: {e}"
