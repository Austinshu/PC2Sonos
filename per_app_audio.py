"""Per-application audio capture on Windows, via the "process loopback"
WASAPI extension (Windows 10 21H2+ / best on Windows 11): instead of
recording whatever the system's default output device is playing (the
normal whole-desktop path in audio_engine.py, via VB-Cable), this asks the
OS for just one process's (and, optionally, its child processes') audio,
regardless of what device it's actually rendering to.

There is no existing Python package for this -- pycaw (already a
dependency, used elsewhere for volume/session control) doesn't expose it,
and pyaudiowpatch's loopback support is desktop-wide only. This defines
the missing pieces (IAudioCaptureClient, the activation completion-handler
callback, and the AUDIOCLIENT_ACTIVATION_PARAMS blob) directly via
ctypes/comtypes and reuses pycaw's existing IAudioClient/WAVEFORMATEX
definitions for everything else, the same way pycaw itself is built.

Not every process can be captured this way -- DRM-protected playback,
some elevated processes, and apps that opened their stream in exclusive
mode are excluded by Windows itself (ActivateAudioInterfaceAsync then
fails or the resulting stream is silent). Callers should fall back to
whole-system capture when this raises.
"""

import ctypes
import threading
import time
from ctypes import HRESULT, POINTER, byref
from ctypes import c_uint32 as UINT32
from ctypes.wintypes import DWORD

from comtypes import COMMETHOD, GUID, IUnknown
from comtypes.hresult import S_OK
from comtypes.server.localserver import COMObject
from pycaw.pycaw import IAudioClient, WAVEFORMATEX
from pycaw.constants import AUDCLNT_SHAREMODE
from pycaw.utils import AudioUtilities

VIRTUAL_AUDIO_DEVICE_PROCESS_LOOPBACK = "VAD\\Process_Loopback"
_ACTIVATION_TYPE_PROCESS_LOOPBACK = 1
PROCESS_LOOPBACK_MODE_INCLUDE_TREE = 0
PROCESS_LOOPBACK_MODE_EXCLUDE_TREE = 1
_VT_BLOB = 0x41  # VARENUM.VT_BLOB
_AUDCLNT_STREAMFLAGS_LOOPBACK = 0x00020000
_AUDCLNT_BUFFERFLAGS_SILENT = 0x2
_WAVE_FORMAT_IEEE_FLOAT = 0x0003


# ---- structures Windows expects on the wire (mmdeviceapi.h / audioclient.h) ----

class _ProcessLoopbackParams(ctypes.Structure):
    _fields_ = [("TargetProcessId", DWORD),
                ("ProcessLoopbackMode", ctypes.c_int)]


class _ActivationParamsUnion(ctypes.Union):
    _fields_ = [("ProcessLoopbackParams", _ProcessLoopbackParams)]


class _ActivationParams(ctypes.Structure):
    _fields_ = [("ActivationType", ctypes.c_int),
                ("union", _ActivationParamsUnion)]


class _Blob(ctypes.Structure):
    _fields_ = [("cbSize", ctypes.c_ulong),
                ("pBlobData", POINTER(ctypes.c_byte))]


class _PropvariantUnion(ctypes.Union):
    _fields_ = [("blob", _Blob), ("_pad", ctypes.c_uint64 * 2)]


class _PropvariantBlob(ctypes.Structure):
    """A PROPVARIANT carrying a VT_BLOB -- just enough of the real
    PROPVARIANT layout (vt + 3 reserved WORDs + a value union) to pass our
    activation params to ActivateAudioInterfaceAsync; we never read one
    back, so every other variant type pycaw's own PROPVARIANT knows about
    is irrelevant here."""
    _fields_ = [("vt", ctypes.c_ushort),
                ("reserved1", ctypes.c_ushort),
                ("reserved2", ctypes.c_ushort),
                ("reserved3", ctypes.c_ushort),
                ("union", _PropvariantUnion)]


# ---- COM interfaces missing from pycaw ----

class IAudioCaptureClient(IUnknown):
    _iid_ = GUID("{C8ADBD64-E71E-48A0-A4DE-185C395CD317}")
    _methods_ = (
        COMMETHOD([], HRESULT, "GetBuffer",
                  (["out"], POINTER(POINTER(ctypes.c_byte)), "ppData"),
                  (["out"], POINTER(UINT32), "pNumFramesToRead"),
                  (["out"], POINTER(DWORD), "pdwFlags"),
                  (["out"], POINTER(ctypes.c_uint64), "pu64DevicePosition"),
                  (["out"], POINTER(ctypes.c_uint64), "pu64QPCPosition")),
        COMMETHOD([], HRESULT, "ReleaseBuffer",
                  (["in"], UINT32, "NumFramesRead")),
        COMMETHOD([], HRESULT, "GetNextPacketSize",
                  (["out"], POINTER(UINT32), "pNumFramesInNextPacket")),
    )


class IActivateAudioInterfaceAsyncOperation(IUnknown):
    _iid_ = GUID("{72A22D78-CDE4-431D-B8CC-843A71199B6D}")
    _methods_ = (
        COMMETHOD([], HRESULT, "GetActivateResult",
                  (["out"], POINTER(HRESULT), "activateResult"),
                  (["out"], POINTER(POINTER(IUnknown)), "activatedInterface")),
    )


class IActivateAudioInterfaceCompletionHandler(IUnknown):
    _iid_ = GUID("{94EA2B94-E9CC-49E0-C0FF-EE64CA8F5B90}")
    _methods_ = (
        COMMETHOD([], HRESULT, "ActivateCompleted",
                  (["in"], POINTER(IActivateAudioInterfaceAsyncOperation), "activateOperation")),
    )


class _CompletionHandler(COMObject):
    """Implements the one-shot callback ActivateAudioInterfaceAsync calls
    (on some Windows-internal thread) once activation finishes. Exists only
    for the lifetime of one activate_process_loopback() call."""
    _com_interfaces_ = [IActivateAudioInterfaceCompletionHandler]

    def __init__(self):
        super().__init__()
        self.done = threading.Event()
        self.hr = None
        self.iface = None

    def IActivateAudioInterfaceCompletionHandler_ActivateCompleted(self, activateOperation):
        try:
            hr, iface = activateOperation.GetActivateResult()
            self.hr = hr
            self.iface = iface
        except Exception:
            self.hr = -1
        finally:
            self.done.set()
        return S_OK


_mmdevapi = ctypes.WinDLL("Mmdevapi.dll")
_ActivateAudioInterfaceAsync = _mmdevapi.ActivateAudioInterfaceAsync
_ActivateAudioInterfaceAsync.restype = HRESULT
_ActivateAudioInterfaceAsync.argtypes = [
    ctypes.c_wchar_p,
    POINTER(GUID),
    POINTER(_PropvariantBlob),
    POINTER(IActivateAudioInterfaceCompletionHandler),
    POINTER(ctypes.c_void_p),
]


def list_audio_sessions():
    """Processes with an audio session open in this login session (whether
    or not they're making sound right now) -- the dashboard's app picker.
    Returns [{"pid": int, "name": str}], deduplicated by name.

    Called from a Flask request thread (see webapp.py), which -- like
    capture_loop's dedicated thread -- has never touched COM before, so
    this needs its own CoInitialize/CoUninitialize around the call just
    as much as capture_loop does."""
    import comtypes
    comtypes.CoInitialize()
    try:
        seen = {}
        for session in AudioUtilities.GetAllSessions():
            proc = session.Process
            if proc is None:
                continue  # the "system sounds" pseudo-session
            try:
                name = proc.name()
                pid = proc.pid
            except Exception:
                continue
            seen[name] = pid
        return [{"pid": pid, "name": name} for name, pid in sorted(seen.items())]
    finally:
        comtypes.CoUninitialize()


def activate_process_loopback_client(pid, include_tree=True, timeout=5.0):
    """Returns an initialized, started IAudioClient capturing just `pid`
    (and its child processes, if include_tree). Raises OSError/TimeoutError
    if this process/Windows version can't be captured this way -- callers
    should fall back to whole-system capture rather than propagate that."""
    params = _ActivationParams()
    params.ActivationType = _ACTIVATION_TYPE_PROCESS_LOOPBACK
    params.union.ProcessLoopbackParams.TargetProcessId = pid
    params.union.ProcessLoopbackParams.ProcessLoopbackMode = (
        PROCESS_LOOPBACK_MODE_INCLUDE_TREE if include_tree
        else PROCESS_LOOPBACK_MODE_EXCLUDE_TREE)

    prop = _PropvariantBlob()
    prop.vt = _VT_BLOB
    prop.union.blob.cbSize = ctypes.sizeof(params)
    prop.union.blob.pBlobData = ctypes.cast(ctypes.pointer(params), POINTER(ctypes.c_byte))

    handler = _CompletionHandler()
    operation = ctypes.c_void_p()
    hr = _ActivateAudioInterfaceAsync(
        VIRTUAL_AUDIO_DEVICE_PROCESS_LOOPBACK, IAudioClient._iid_,
        byref(prop), handler, byref(operation))
    if hr != 0:
        raise OSError(f"ActivateAudioInterfaceAsync failed: 0x{hr & 0xFFFFFFFF:08x}")

    if not handler.done.wait(timeout=timeout):
        raise TimeoutError(f"process-loopback activation timed out for pid {pid}")
    if handler.hr != 0 or handler.iface is None:
        raise OSError(f"process-loopback activation failed for pid {pid}: "
                       f"hr=0x{(handler.hr or 0) & 0xFFFFFFFF:08x}")

    audio_client = handler.iface.QueryInterface(IAudioClient)

    # GetMixFormat() is not implemented on a process-loopback IAudioClient
    # (confirmed: it raises E_NOTIMPL) -- unlike normal WASAPI capture,
    # there's no "ask the device what it wants" here. Microsoft's own
    # ApplicationLoopback sample hardcodes a format for exactly this
    # reason; 48kHz/stereo/float32 is what Windows' audio engine itself
    # runs at internally on every machine this has been observed on.
    fmt = WAVEFORMATEX()
    fmt.wFormatTag = _WAVE_FORMAT_IEEE_FLOAT
    fmt.nChannels = 2
    fmt.nSamplesPerSec = 48000
    fmt.wBitsPerSample = 32
    fmt.nBlockAlign = fmt.nChannels * fmt.wBitsPerSample // 8
    fmt.nAvgBytesPerSec = fmt.nSamplesPerSec * fmt.nBlockAlign
    fmt.cbSize = 0
    audio_client.Initialize(
        AUDCLNT_SHAREMODE.AUDCLNT_SHAREMODE_SHARED.value,
        _AUDCLNT_STREAMFLAGS_LOOPBACK,
        10_000_000,  # 1s buffer, in 100ns units
        0, byref(fmt), None)
    capture_iface = audio_client.GetService(IAudioCaptureClient._iid_)
    capture_client = capture_iface.QueryInterface(IAudioCaptureClient)
    audio_client.Start()
    return audio_client, capture_client, fmt


def capture_loop(pid, stop_event, on_chunk, include_tree=True):
    """Runs until stop_event is set or the target process's audio can't be
    read anymore (it exited, Windows revoked the stream, etc.) -- in which
    case this just returns; the caller decides whether/how to recover.
    on_chunk(pcm_bytes, sample_rate, channels, sample_width) is called for
    every buffer, already converted to integer PCM (from whatever float/int
    format the process-loopback endpoint actually delivered) so callers
    never need to know which format Windows picked.

    Meant to run on its own dedicated thread (audio_engine.py starts it
    that way). COM is apartment-per-thread -- every COM call in this
    module fails with "CoInitialize has not been called" unless the
    calling thread has its own initialized apartment, so this owns that
    lifecycle itself rather than assuming the caller already did it."""
    import comtypes
    comtypes.CoInitialize()
    try:
        audio_client, capture_client, fmt = activate_process_loopback_client(pid, include_tree)
    except Exception:
        comtypes.CoUninitialize()
        raise
    frame_bytes = fmt.nBlockAlign
    channels = fmt.nChannels
    rate = fmt.nSamplesPerSec
    bits = fmt.wBitsPerSample
    try:
        while not stop_event.is_set():
            try:
                packet_frames = capture_client.GetNextPacketSize()
            except Exception:
                return
            if not packet_frames:
                time.sleep(0.005)
                continue
            data_ptr, num_frames, flags, _dev_pos, _qpc_pos = capture_client.GetBuffer()
            try:
                if num_frames:
                    n_bytes = num_frames * frame_bytes
                    if flags & _AUDCLNT_BUFFERFLAGS_SILENT or not data_ptr:
                        raw = b"\x00" * n_bytes
                    else:
                        raw = bytes((ctypes.c_byte * n_bytes).from_address(
                            ctypes.addressof(data_ptr.contents)))
                    pcm, out_width = _to_pcm(raw, bits)
                    on_chunk(pcm, rate, channels, out_width)
            finally:
                capture_client.ReleaseBuffer(num_frames)
    finally:
        try:
            audio_client.Stop()
        except Exception:
            pass
        comtypes.CoUninitialize()


def _to_pcm(raw, bits):
    """Process-loopback endpoints normally deliver 32-bit float samples
    (bits==32); this scales those to 16-bit signed PCM so the result drops
    straight into the same broadcaster/render/stream pipeline that whole-
    system capture already feeds (which assumes 16-bit PCM throughout).
    16-bit sources pass through unchanged; anything else (rare) also gets
    treated as float32, since that's what every observed Windows build
    actually sends here."""
    if bits == 16:
        return raw, 2
    import numpy as np
    samples = np.frombuffer(raw, dtype=np.float32)
    pcm16 = np.clip(samples * 32767.0, -32768, 32767).astype(np.int16)
    return pcm16.tobytes(), 2
