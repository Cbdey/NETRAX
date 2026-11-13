"""Find a webcam's index by its NAME (13 Nov).

Windows DirectShow numbers webcams in the order it finds them, and that order changed when the USB webcam was
plugged in or out (5 Nov: USB = 0, laptop = 1; 11 Nov: laptop = 0, USB = 1). cameras.yaml can therefore say
    source: 0                        # fallback index
    device: HP Wide Vision           # part of the name Windows shows (Device Manager > Cameras)
and the reader opens the camera whose name contains that text ("!HP Wide Vision" = the first camera whose name does
NOT contain it, e.g. the USB webcam whatever its name is). A named camera that is not connected is reported as such -
the reader never opens another camera's index instead. OpenCV's DirectShow backend uses this same order.
names() lists the cameras:  python -m edge.webcams
"""
import os

if os.name == "nt":
    from ctypes import POINTER, c_ulong, byref
    import comtypes
    from comtypes import COMMETHOD, GUID, HRESULT, IUnknown
    from comtypes.persist import IPropertyBag

    _SYS_DEV_ENUM = GUID("{62BE5D10-60EB-11d0-BD3B-00A0C911CE86}")
    _VIDEO_INPUT = GUID("{860BB310-5D01-11d0-BD3B-00A0C911CE86}")

    class IMoniker(IUnknown):                    # only the vtable prefix we need (IPersist, IPersistStream, 2 methods)
        _iid_ = GUID("{0000000F-0000-0000-C000-000000000046}")
        _methods_ = [
            COMMETHOD([], HRESULT, "GetClassID", (["out"], POINTER(GUID), "c")),
            COMMETHOD([], HRESULT, "IsDirty"),
            COMMETHOD([], HRESULT, "Load", (["in"], POINTER(IUnknown), "s")),
            COMMETHOD([], HRESULT, "Save", (["in"], POINTER(IUnknown), "s"), (["in"], c_ulong, "d")),
            COMMETHOD([], HRESULT, "GetSizeMax", (["out"], POINTER(c_ulong * 2), "n")),
            COMMETHOD([], HRESULT, "BindToObject", (["in"], POINTER(IUnknown), "b"), (["in"], POINTER(IUnknown), "m"),
                      (["in"], POINTER(GUID), "i"), (["out"], POINTER(POINTER(IUnknown)), "o")),
            COMMETHOD([], HRESULT, "BindToStorage", (["in"], POINTER(IUnknown), "b"), (["in"], POINTER(IUnknown), "m"),
                      (["in"], POINTER(GUID), "i"), (["out"], POINTER(POINTER(IUnknown)), "o")),
        ]

    class IEnumMoniker(IUnknown):
        _iid_ = GUID("{00000102-0000-0000-C000-000000000046}")
        _methods_ = [
            COMMETHOD([], HRESULT, "Next", (["in"], c_ulong, "n"), (["out"], POINTER(POINTER(IMoniker)), "m"),
                      (["out"], POINTER(c_ulong), "f")),
        ]

    class ICreateDevEnum(IUnknown):
        _iid_ = GUID("{29840822-5B84-11D0-BD3B-00A0C911CE86}")
        _methods_ = [
            COMMETHOD([], HRESULT, "CreateClassEnumerator", (["in"], POINTER(GUID), "c"),
                      (["out"], POINTER(POINTER(IEnumMoniker)), "e"), (["in"], c_ulong, "f")),
        ]


def names():
    """DirectShow video input names, in OpenCV's CAP_DSHOW index order. [] if unknown / not Windows."""
    if os.name != "nt":
        return []
    import comtypes.client
    try:
        comtypes.CoInitialize()                  # reader threads are not COM-initialised
    except OSError:
        pass
    out = []
    try:
        dev = comtypes.client.CreateObject(_SYS_DEV_ENUM, interface=ICreateDevEnum)
        en = dev.CreateClassEnumerator(byref(_VIDEO_INPUT), 0)
        if not en:                               # S_FALSE: no camera at all
            return []
        while True:
            try:
                mon, got = en.Next(1)
            except comtypes.COMError:
                break
            if not got or not mon:
                break
            bag = mon.BindToStorage(None, None, byref(IPropertyBag._iid_)).QueryInterface(IPropertyBag)
            try:
                out.append(str(bag.Read("FriendlyName", pErrorLog=None)))
            except comtypes.COMError:
                out.append("?")
    except (OSError, comtypes.COMError, ValueError):
        return []
    return out


def index_of(device, fallback=None):
    """Index of the first camera whose name contains `device` (case-insensitive).
    None = that camera is not connected (never another camera's index); fallback only if the names cannot be read."""
    if not device:
        return fallback
    found = names()
    if not found or found == ["?"] * len(found):
        return fallback
    want = str(device).lower()
    neg = want.startswith("!")               # "!HP Wide Vision" = the first camera that is NOT the laptop camera
    want = want.lstrip("!").strip()
    for i, n in enumerate(found):
        if (want not in n.lower()) if neg else (want in n.lower()):
            return i
    return None


if __name__ == "__main__":
    for i, n in enumerate(names()):
        print(i, n)
