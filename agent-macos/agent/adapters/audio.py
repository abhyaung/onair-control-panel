"""CoreAudio input control — a mic cut that covers *every* device.

Why this exists, found the hard way during a live call:

`set volume input volume 0` only touches the macOS **default input device**.
Applications are free to capture from a different one, and Meet was doing
exactly that — configured to use a Logitech Brio while the default was the
MacBook microphone. The panel reported "OFF", the MacBook mic was indeed muted,
and the user was fully audible through the Brio at 75%.

A master switch that mutes one arbitrary device is not a master switch. This
mutes every input device that has an input stream, and reports "live" if *any*
of them is open — so the panel errs toward warning you that you are hot.

Uses each device's dedicated mute property rather than zeroing volume: mute is
an unambiguous boolean, and restoring it cannot lose the user's gain setting.
Devices lacking a mute property fall back to volume, with the prior level kept
so unmuting restores it exactly.
"""

import ctypes
import ctypes.util
import struct

_ca = ctypes.CDLL("/System/Library/Frameworks/CoreAudio.framework/CoreAudio")
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
_SCOPE_INPUT = _fourcc("inpt")

_PROP_DEVICES = _fourcc("dev#")
_PROP_NAME = _fourcc("lnam")
_PROP_STREAMS = _fourcc("stm#")
_PROP_VOLUME = _fourcc("volm")
_PROP_MUTE = _fourcc("mute")

# Remembers per-device volume for devices with no mute property, so unmuting
# restores the exact prior gain instead of guessing.
_restore = {}


def _address(selector, scope=_SCOPE_GLOBAL, element=0):
    return _Address(selector, scope, element)


def _has(device, addr):
    return bool(_ca.AudioObjectHasProperty(device, ctypes.byref(addr)))


def _get_u32_array(device, addr):
    size = ctypes.c_uint32()
    if _ca.AudioObjectGetPropertyDataSize(device, ctypes.byref(addr), 0, None,
                                          ctypes.byref(size)):
        return []
    count = size.value // 4
    if not count:
        return []
    buf = (ctypes.c_uint32 * count)()
    if _ca.AudioObjectGetPropertyData(device, ctypes.byref(addr), 0, None,
                                      ctypes.byref(size), buf):
        return []
    return list(buf)


def name(device):
    ref = ctypes.c_void_p()
    size = ctypes.c_uint32(8)
    addr = _address(_PROP_NAME)
    if _ca.AudioObjectGetPropertyData(device, ctypes.byref(addr), 0, None,
                                      ctypes.byref(size), ctypes.byref(ref)):
        return "device %d" % device
    buf = ctypes.create_string_buffer(256)
    _cf.CFStringGetCString.argtypes = [ctypes.c_void_p, ctypes.c_char_p,
                                       ctypes.c_long, ctypes.c_uint32]
    _cf.CFStringGetCString(ref, buf, 256, 0x08000100)  # kCFStringEncodingUTF8
    return buf.value.decode("utf-8", "replace")


def input_devices():
    """Every device that actually has an input stream."""
    devices = []
    for device in _get_u32_array(_SYSTEM, _address(_PROP_DEVICES)):
        streams = _address(_PROP_STREAMS, _SCOPE_INPUT)
        size = ctypes.c_uint32()
        if _ca.AudioObjectGetPropertyDataSize(device, ctypes.byref(streams), 0,
                                              None, ctypes.byref(size)):
            continue
        if size.value:
            devices.append(device)
    return devices


def _get_mute(device):
    addr = _address(_PROP_MUTE, _SCOPE_INPUT)
    if not _has(device, addr):
        return None
    value = ctypes.c_uint32()
    size = ctypes.c_uint32(4)
    if _ca.AudioObjectGetPropertyData(device, ctypes.byref(addr), 0, None,
                                      ctypes.byref(size), ctypes.byref(value)):
        return None
    return bool(value.value)


def _set_mute(device, muted):
    addr = _address(_PROP_MUTE, _SCOPE_INPUT)
    if not _has(device, addr):
        return False
    value = ctypes.c_uint32(1 if muted else 0)
    return _ca.AudioObjectSetPropertyData(device, ctypes.byref(addr), 0, None,
                                          4, ctypes.byref(value)) == 0


def _get_volume(device):
    addr = _address(_PROP_VOLUME, _SCOPE_INPUT)
    if not _has(device, addr):
        return None
    value = ctypes.c_float()
    size = ctypes.c_uint32(4)
    if _ca.AudioObjectGetPropertyData(device, ctypes.byref(addr), 0, None,
                                      ctypes.byref(size), ctypes.byref(value)):
        return None
    return value.value


def _set_volume(device, level):
    addr = _address(_PROP_VOLUME, _SCOPE_INPUT)
    if not _has(device, addr):
        return False
    value = ctypes.c_float(max(0.0, min(1.0, level)))
    return _ca.AudioObjectSetPropertyData(device, ctypes.byref(addr), 0, None,
                                          4, ctypes.byref(value)) == 0


def status():
    """Per-device view: [{id, name, muted}], muted None when undeterminable."""
    out = []
    for device in input_devices():
        muted = _get_mute(device)
        if muted is None:
            level = _get_volume(device)
            muted = None if level is None else level <= 0.0001
        out.append({"id": device, "name": name(device), "muted": muted})
    return out


def any_live():
    """True if any *determinable* input device is open, else False, else None.

    Pessimism has a limit. Counting unreadable devices as live sounds safer, but
    an inert Continuity Camera microphone sits in the device list exposing
    neither mute nor volume — so that rule pins the panel to "live" forever and
    it stops meaning anything.

    So devices whose state cannot be read are excluded from the verdict and
    surfaced separately (`unreadable()`) instead of silently swaying it. Any
    device that *can* be read and is open still wins: one open mic means live.
    """
    known = [d["muted"] for d in status() if d["muted"] is not None]
    if not known:
        return None
    return any(m is False for m in known)


def unreadable():
    """Names of input devices whose mute state cannot be determined."""
    return [d["name"] for d in status() if d["muted"] is None]


def mute_all():
    """Mute every input device. Returns per-device results."""
    results = []
    for device in input_devices():
        if _set_mute(device, True):
            results.append((name(device), True))
            continue
        level = _get_volume(device)
        if level is not None:
            if level > 0.0001:
                _restore[device] = level
            results.append((name(device), _set_volume(device, 0.0)))
        else:
            results.append((name(device), False))
    return results


# Below this a device is effectively inaudible whatever its mute flag says.
# Not 0.0: `set volume input volume 0` lands on the device minimum rather than
# true zero — measured at 0.078 on the MacBook mic — so a stricter threshold
# leaves a mic that looks restored and captures nothing.
_SILENT = 0.12


def unmute_all():
    """Unmute every input device and make sure it is actually audible.

    Clearing the mute flag is not enough on its own: a device can be *both*
    muted and volume-zeroed, and earlier versions of this project zeroed input
    volume directly. Unmuting alone then leaves a mic that reports "not muted"
    while capturing nothing — the worst possible state, because every indicator
    says you are live.
    """
    results = []
    for device in input_devices():
        unmuted = _set_mute(device, False)

        level = _get_volume(device)
        if level is not None and level < _SILENT:
            restored = _restore.pop(device, 0.75)
            _set_volume(device, restored)
            level = _get_volume(device)

        audible = (level is None) or (level >= _SILENT)
        results.append((name(device), bool(unmuted or level is not None) and audible))
    return results
