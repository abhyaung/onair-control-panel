"""CoreMediaIO camera state — is any camera actually capturing?

The camera tile originally read its state from Meet's DOM, which made it
dependent on Chrome's "Allow JavaScript from Apple Events" setting. With that
off the tile showed `no signal` while the camera was plainly live in a call —
the panel failing at the one question it exists to answer.

macOS knows the answer directly. CoreMediaIO mirrors CoreAudio's property API,
and `kCMIODevicePropertyDeviceIsRunningSomewhere` reports whether a device is
capturing for *any* process. That is better than the DOM in every way: it needs
no permission, no browser, and covers apps we know nothing about.

Note the signature difference from CoreAudio — CMIOObjectGetPropertyData takes
an extra `dataUsed` out-parameter.

State only. Turning a camera off still means driving the app's own control (or
HARD CUT), because macOS exposes no way to close another process's stream.
"""

import ctypes
import struct

_cm = ctypes.CDLL("/System/Library/Frameworks/CoreMediaIO.framework/CoreMediaIO")
_cf = ctypes.CDLL("/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation")


def _fourcc(text):
    return struct.unpack(">I", text.encode())[0]


class _Address(ctypes.Structure):
    _fields_ = [
        ("mSelector", ctypes.c_uint32),
        ("mScope", ctypes.c_uint32),
        ("mElement", ctypes.c_uint32),
    ]


_SYSTEM = 1
_SCOPE_GLOBAL = _fourcc("glob")
_PROP_DEVICES = _fourcc("dev#")
_PROP_NAME = _fourcc("lnam")
_PROP_RUNNING = _fourcc("gone")   # kCMIODevicePropertyDeviceIsRunningSomewhere


def _name(device):
    ref = ctypes.c_void_p()
    size = ctypes.c_uint32(8)
    used = ctypes.c_uint32()
    addr = _Address(_PROP_NAME, _SCOPE_GLOBAL, 0)
    if _cm.CMIOObjectGetPropertyData(device, ctypes.byref(addr), 0, None,
                                     size, ctypes.byref(used), ctypes.byref(ref)):
        return "camera %d" % device
    buf = ctypes.create_string_buffer(256)
    _cf.CFStringGetCString.argtypes = [ctypes.c_void_p, ctypes.c_char_p,
                                       ctypes.c_long, ctypes.c_uint32]
    _cf.CFStringGetCString(ref, buf, 256, 0x08000100)
    return buf.value.decode("utf-8", "replace")


def _in_use(device):
    addr = _Address(_PROP_RUNNING, _SCOPE_GLOBAL, 0)
    if not _cm.CMIOObjectHasProperty(device, ctypes.byref(addr)):
        return None
    value = ctypes.c_uint32()
    size = ctypes.c_uint32(4)
    used = ctypes.c_uint32()
    if _cm.CMIOObjectGetPropertyData(device, ctypes.byref(addr), 0, None,
                                     size, ctypes.byref(used), ctypes.byref(value)):
        return None
    return bool(value.value)


def devices():
    """[{name, in_use}] for every camera macOS knows about."""
    addr = _Address(_PROP_DEVICES, _SCOPE_GLOBAL, 0)
    size = ctypes.c_uint32()
    if _cm.CMIOObjectGetPropertyDataSize(_SYSTEM, ctypes.byref(addr), 0, None,
                                         ctypes.byref(size)):
        return []
    count = size.value // 4
    if not count:
        return []
    ids = (ctypes.c_uint32 * count)()
    used = ctypes.c_uint32()
    if _cm.CMIOObjectGetPropertyData(_SYSTEM, ctypes.byref(addr), 0, None,
                                     size, ctypes.byref(used), ids):
        return []
    return [{"name": _name(d), "in_use": _in_use(d)} for d in ids]


def any_live():
    """True if any camera is capturing, False if none, None if undeterminable."""
    known = [d["in_use"] for d in devices() if d["in_use"] is not None]
    if not known:
        return None
    return any(known)


def live_names():
    return [d["name"] for d in devices() if d["in_use"]]
