"""macOS default-output-device control via CoreAudio (ctypes).

PC2Sonos only works when the Mac's default output is BlackHole, since
that's the device it captures from. Rather than sending people into
System Settings > Sound every login, this switches it programmatically
using the public CoreAudio HAL API (AudioObjectGetPropertyData /
AudioObjectSetPropertyData on kAudioHardwarePropertyDefaultOutputDevice)
-- no private APIs, no root.

Everything fails soft: on any error the functions return (False, reason)
and the caller logs a hint for the human instead. Imports of the
CoreAudio framework happen inside functions so the Linux test harness
can import this module.
"""

import ctypes
import sys


def _fourcc(s):
    return int.from_bytes(s.encode("ascii"), "big")


kAudioObjectSystemObject = 1
kAudioHardwarePropertyDevices = _fourcc("dev#")
kAudioHardwarePropertyDefaultOutputDevice = _fourcc("dOut")
kAudioHardwarePropertyDefaultSystemOutputDevice = _fourcc("sOut")
kAudioObjectPropertyName = _fourcc("lnam")
kAudioDevicePropertyStreamConfiguration = _fourcc("slay")
kAudioObjectPropertyScopeGlobal = _fourcc("glob")
kAudioObjectPropertyScopeOutput = _fourcc("outp")
kAudioObjectPropertyElementMain = 0
kCFStringEncodingUTF8 = 0x08000100


class _PropertyAddress(ctypes.Structure):
    _fields_ = [("mSelector", ctypes.c_uint32),
                ("mScope", ctypes.c_uint32),
                ("mElement", ctypes.c_uint32)]


_libs = None


def _load():
    """(CoreAudio, CoreFoundation) handles with argtypes set. Raises
    OSError off-macOS."""
    global _libs
    if _libs is not None:
        return _libs
    if sys.platform != "darwin":
        raise OSError("CoreAudio is only available on macOS")
    ca = ctypes.cdll.LoadLibrary("/System/Library/Frameworks/CoreAudio.framework/CoreAudio")
    cf = ctypes.cdll.LoadLibrary("/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation")
    ca.AudioObjectGetPropertyDataSize.restype = ctypes.c_int32
    ca.AudioObjectGetPropertyDataSize.argtypes = [
        ctypes.c_uint32, ctypes.POINTER(_PropertyAddress), ctypes.c_uint32,
        ctypes.c_void_p, ctypes.POINTER(ctypes.c_uint32)]
    ca.AudioObjectGetPropertyData.restype = ctypes.c_int32
    ca.AudioObjectGetPropertyData.argtypes = [
        ctypes.c_uint32, ctypes.POINTER(_PropertyAddress), ctypes.c_uint32,
        ctypes.c_void_p, ctypes.POINTER(ctypes.c_uint32), ctypes.c_void_p]
    ca.AudioObjectSetPropertyData.restype = ctypes.c_int32
    ca.AudioObjectSetPropertyData.argtypes = [
        ctypes.c_uint32, ctypes.POINTER(_PropertyAddress), ctypes.c_uint32,
        ctypes.c_void_p, ctypes.c_uint32, ctypes.c_void_p]
    cf.CFStringGetCString.restype = ctypes.c_bool
    cf.CFStringGetCString.argtypes = [ctypes.c_void_p, ctypes.c_char_p, ctypes.c_long, ctypes.c_uint32]
    cf.CFRelease.restype = None
    cf.CFRelease.argtypes = [ctypes.c_void_p]
    _libs = (ca, cf)
    return _libs


def _addr(selector, scope=kAudioObjectPropertyScopeGlobal):
    return _PropertyAddress(selector, scope, kAudioObjectPropertyElementMain)


def _get_data(obj, addr):
    ca, _ = _load()
    size = ctypes.c_uint32(0)
    status = ca.AudioObjectGetPropertyDataSize(obj, ctypes.byref(addr), 0, None, ctypes.byref(size))
    if status != 0:
        raise OSError(f"AudioObjectGetPropertyDataSize failed ({status})")
    buf = ctypes.create_string_buffer(size.value)
    status = ca.AudioObjectGetPropertyData(obj, ctypes.byref(addr), 0, None, ctypes.byref(size), buf)
    if status != 0:
        raise OSError(f"AudioObjectGetPropertyData failed ({status})")
    return buf.raw[: size.value]


def _device_ids():
    raw = _get_data(kAudioObjectSystemObject, _addr(kAudioHardwarePropertyDevices))
    n = len(raw) // 4
    return list((ctypes.c_uint32 * n).from_buffer_copy(raw))


def _device_name(dev):
    _, cf = _load()
    raw = _get_data(dev, _addr(kAudioObjectPropertyName))
    cfstr = ctypes.c_void_p.from_buffer_copy(raw).value
    if not cfstr:
        return ""
    try:
        buf = ctypes.create_string_buffer(512)
        if cf.CFStringGetCString(cfstr, buf, 512, kCFStringEncodingUTF8):
            return buf.value.decode("utf-8", "replace")
        return ""
    finally:
        cf.CFRelease(cfstr)


def _output_channels(dev):
    """Sum of channels across the device's output streams (AudioBufferList)."""
    try:
        raw = _get_data(dev, _addr(kAudioDevicePropertyStreamConfiguration, kAudioObjectPropertyScopeOutput))
    except OSError:
        return 0
    if len(raw) < 4:
        return 0
    n_buffers = int.from_bytes(raw[0:4], sys.byteorder)
    total = 0
    off = 8  # UInt32 mNumberBuffers + 4 bytes padding before the first AudioBuffer on 64-bit
    for _ in range(n_buffers):
        if off + 4 > len(raw):
            break
        total += int.from_bytes(raw[off:off + 4], sys.byteorder)
        off += 16  # AudioBuffer: UInt32 mNumberChannels, UInt32 mDataByteSize, void* mData
    return total


def list_output_devices():
    """[(device_id, name)] for every CoreAudio device with output channels."""
    out = []
    for dev in _device_ids():
        try:
            if _output_channels(dev) > 0:
                out.append((dev, _device_name(dev)))
        except OSError:
            continue
    return out


def current_default_output():
    """(device_id, name) of the default output, or (None, '')."""
    try:
        raw = _get_data(kAudioObjectSystemObject, _addr(kAudioHardwarePropertyDefaultOutputDevice))
        dev = int.from_bytes(raw[:4], sys.byteorder)
        return dev, _device_name(dev)
    except Exception:
        return None, ""


def set_default_output(name_substr):
    """Make the first output device whose name contains name_substr the
    default output (and the default system/alert output, so UI sounds
    don't leak out of the real speakers ahead of the delayed feed).
    Returns (ok, detail)."""
    try:
        ca, _ = _load()
        target = None
        for dev, name in list_output_devices():
            if name_substr.lower() in name.lower():
                target = (dev, name)
                break
        if target is None:
            return False, f"no output device matching '{name_substr}'"
        dev_id = ctypes.c_uint32(target[0])
        for selector in (kAudioHardwarePropertyDefaultOutputDevice,
                         kAudioHardwarePropertyDefaultSystemOutputDevice):
            addr = _addr(selector)
            status = ca.AudioObjectSetPropertyData(kAudioObjectSystemObject, ctypes.byref(addr), 0, None,
                                                   4, ctypes.byref(dev_id))
            if status != 0:
                return False, f"AudioObjectSetPropertyData failed ({status})"
        return True, target[1]
    except Exception as e:
        return False, f"{type(e).__name__}: {e}"


def ensure_blackhole_is_default(substr="BlackHole"):
    """If BlackHole exists and isn't the default output, make it so.
    Returns (changed, status_line, previous_name)."""
    try:
        _, current = current_default_output()
        if substr.lower() in current.lower():
            return False, f"default output already OK ({current})", current
        ok, detail = set_default_output(substr)
        if ok:
            return True, f"default output switched to '{detail}' (was '{current or 'unknown'}')", current
        return False, f"could not switch default output: {detail}", current
    except Exception as e:
        return False, f"default-output check failed: {type(e).__name__}: {e}", ""


def restore_default_output(previous_name, avoid_substr="BlackHole"):
    """Put the default output back to `previous_name` (or the first real
    output if that's gone) so quitting PC2Sonos doesn't leave the Mac
    silently playing into BlackHole. Returns a status line."""
    try:
        if previous_name and avoid_substr.lower() not in previous_name.lower():
            ok, detail = set_default_output(previous_name)
            if ok:
                return f"default output restored to '{detail}'"
        for _, name in list_output_devices():
            if avoid_substr.lower() not in name.lower():
                ok, detail = set_default_output(name)
                if ok:
                    return f"default output set to '{detail}'"
        return "no non-virtual output device to restore to"
    except Exception as e:
        return f"restore failed: {type(e).__name__}: {e}"
