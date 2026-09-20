"""Raw ctypes bindings for the Win32 display APIs.

Two families are used:

* The Connecting and Configuring Displays (CCD) API in user32
  (QueryDisplayConfig / SetDisplayConfig / DisplayConfigGetDeviceInfo).
  This is what the Settings app uses. It is the only way to get real monitor
  names, to enable/disable an output, and to read or write HDR state.
* The older GDI EnumDisplaySettingsEx / ChangeDisplaySettingsEx pair, which is
  still the most reliable way to enumerate the modes a monitor supports and to
  commit resolution/refresh/position changes for several monitors atomically.
"""

import ctypes
from ctypes import POINTER, byref
from ctypes.wintypes import BOOL, DWORD, HWND, LONG, RECT, WCHAR, WORD

user32 = ctypes.WinDLL("user32", use_last_error=True)

UINT32 = ctypes.c_uint32
UINT16 = ctypes.c_uint16

CCHDEVICENAME = 32
CCHFORMNAME = 32

# ---------------------------------------------------------------- CCD structs


class LUID(ctypes.Structure):
    _fields_ = [("LowPart", DWORD), ("HighPart", LONG)]

    def key(self):
        return (self.LowPart, self.HighPart)


class DISPLAYCONFIG_PATH_SOURCE_INFO(ctypes.Structure):
    _fields_ = [
        ("adapterId", LUID),
        ("id", UINT32),
        ("modeInfoIdx", UINT32),
        ("statusFlags", UINT32),
    ]


class DISPLAYCONFIG_RATIONAL(ctypes.Structure):
    _fields_ = [("Numerator", UINT32), ("Denominator", UINT32)]

    def hz(self):
        if not self.Denominator:
            return 0.0
        return self.Numerator / self.Denominator


class DISPLAYCONFIG_PATH_TARGET_INFO(ctypes.Structure):
    _fields_ = [
        ("adapterId", LUID),
        ("id", UINT32),
        ("modeInfoIdx", UINT32),
        ("outputTechnology", UINT32),
        ("rotation", UINT32),
        ("scaling", UINT32),
        ("refreshRate", DISPLAYCONFIG_RATIONAL),
        ("scanLineOrdering", UINT32),
        ("targetAvailable", BOOL),
        ("statusFlags", UINT32),
    ]


class DISPLAYCONFIG_PATH_INFO(ctypes.Structure):
    _fields_ = [
        ("sourceInfo", DISPLAYCONFIG_PATH_SOURCE_INFO),
        ("targetInfo", DISPLAYCONFIG_PATH_TARGET_INFO),
        ("flags", UINT32),
    ]


class DISPLAYCONFIG_2DREGION(ctypes.Structure):
    _fields_ = [("cx", UINT32), ("cy", UINT32)]


class DISPLAYCONFIG_VIDEO_SIGNAL_INFO(ctypes.Structure):
    _fields_ = [
        ("pixelRate", ctypes.c_uint64),
        ("hSyncFreq", DISPLAYCONFIG_RATIONAL),
        ("vSyncFreq", DISPLAYCONFIG_RATIONAL),
        ("activeSize", DISPLAYCONFIG_2DREGION),
        ("totalSize", DISPLAYCONFIG_2DREGION),
        ("videoStandard", UINT32),
        ("scanLineOrdering", UINT32),
    ]


class DISPLAYCONFIG_TARGET_MODE(ctypes.Structure):
    _fields_ = [("targetVideoSignalInfo", DISPLAYCONFIG_VIDEO_SIGNAL_INFO)]


class POINTL(ctypes.Structure):
    _fields_ = [("x", LONG), ("y", LONG)]


class DISPLAYCONFIG_SOURCE_MODE(ctypes.Structure):
    _fields_ = [
        ("width", UINT32),
        ("height", UINT32),
        ("pixelFormat", UINT32),
        ("position", POINTL),
    ]


class DISPLAYCONFIG_DESKTOP_IMAGE_INFO(ctypes.Structure):
    _fields_ = [
        ("PathSourceSize", POINTL),
        ("DesktopImageRegion", RECT),
        ("DesktopImageClip", RECT),
    ]


class _MODE_UNION(ctypes.Union):
    _fields_ = [
        ("targetMode", DISPLAYCONFIG_TARGET_MODE),
        ("sourceMode", DISPLAYCONFIG_SOURCE_MODE),
        ("desktopImageInfo", DISPLAYCONFIG_DESKTOP_IMAGE_INFO),
    ]


class DISPLAYCONFIG_MODE_INFO(ctypes.Structure):
    _fields_ = [
        ("infoType", UINT32),
        ("id", UINT32),
        ("adapterId", LUID),
        ("u", _MODE_UNION),
    ]


class DISPLAYCONFIG_DEVICE_INFO_HEADER(ctypes.Structure):
    _fields_ = [
        ("type", UINT32),
        ("size", UINT32),
        ("adapterId", LUID),
        ("id", UINT32),
    ]


class DISPLAYCONFIG_TARGET_DEVICE_NAME(ctypes.Structure):
    _fields_ = [
        ("header", DISPLAYCONFIG_DEVICE_INFO_HEADER),
        ("flags", UINT32),
        ("outputTechnology", UINT32),
        ("edidManufactureId", UINT16),
        ("edidProductCodeId", UINT16),
        ("connectorInstance", UINT32),
        ("monitorFriendlyDeviceName", WCHAR * 64),
        ("monitorDevicePath", WCHAR * 128),
    ]


class DISPLAYCONFIG_SOURCE_DEVICE_NAME(ctypes.Structure):
    _fields_ = [
        ("header", DISPLAYCONFIG_DEVICE_INFO_HEADER),
        ("viewGdiDeviceName", WCHAR * CCHDEVICENAME),
    ]


class DISPLAYCONFIG_TARGET_PREFERRED_MODE(ctypes.Structure):
    _fields_ = [
        ("header", DISPLAYCONFIG_DEVICE_INFO_HEADER),
        ("width", UINT32),
        ("height", UINT32),
        ("targetMode", DISPLAYCONFIG_TARGET_MODE),
    ]


class DISPLAYCONFIG_GET_ADVANCED_COLOR_INFO(ctypes.Structure):
    """Bit 0 supported, 1 enabled, 2 wideColorEnforced, 3 forceDisabled."""

    _fields_ = [
        ("header", DISPLAYCONFIG_DEVICE_INFO_HEADER),
        ("value", UINT32),
        ("colorEncoding", UINT32),
        ("bitsPerColorChannel", UINT32),
    ]

    @property
    def supported(self):
        return bool(self.value & 0x1)

    @property
    def enabled(self):
        return bool(self.value & 0x2)

    @property
    def force_disabled(self):
        return bool(self.value & 0x8)


class DISPLAYCONFIG_SET_ADVANCED_COLOR_STATE(ctypes.Structure):
    _fields_ = [
        ("header", DISPLAYCONFIG_DEVICE_INFO_HEADER),
        ("value", UINT32),
    ]


class DISPLAYCONFIG_SDR_WHITE_LEVEL(ctypes.Structure):
    _fields_ = [
        ("header", DISPLAYCONFIG_DEVICE_INFO_HEADER),
        ("SDRWhiteLevel", UINT32),
    ]


# ----------------------------------------------------------------- CCD consts

QDC_ALL_PATHS = 0x00000001
QDC_ONLY_ACTIVE_PATHS = 0x00000002
QDC_DATABASE_CURRENT = 0x00000004

DISPLAYCONFIG_PATH_ACTIVE = 0x00000001
DISPLAYCONFIG_PATH_MODE_IDX_INVALID = 0xFFFFFFFF

DISPLAYCONFIG_MODE_INFO_TYPE_SOURCE = 1
DISPLAYCONFIG_MODE_INFO_TYPE_TARGET = 2
DISPLAYCONFIG_MODE_INFO_TYPE_DESKTOP_IMAGE = 3

DISPLAYCONFIG_DEVICE_INFO_GET_SOURCE_NAME = 1
DISPLAYCONFIG_DEVICE_INFO_GET_TARGET_NAME = 2
DISPLAYCONFIG_DEVICE_INFO_GET_TARGET_PREFERRED_MODE = 3
DISPLAYCONFIG_DEVICE_INFO_GET_ADVANCED_COLOR_INFO = 9
DISPLAYCONFIG_DEVICE_INFO_SET_ADVANCED_COLOR_STATE = 10
DISPLAYCONFIG_DEVICE_INFO_GET_SDR_WHITE_LEVEL = 11

SDC_TOPOLOGY_INTERNAL = 0x00000001
SDC_TOPOLOGY_CLONE = 0x00000002
SDC_TOPOLOGY_EXTEND = 0x00000004
SDC_TOPOLOGY_EXTERNAL = 0x00000008
SDC_USE_SUPPLIED_DISPLAY_CONFIG = 0x00000020
SDC_VALIDATE = 0x00000040
SDC_APPLY = 0x00000080
SDC_SAVE_TO_DATABASE = 0x00000200
SDC_ALLOW_CHANGES = 0x00000400
SDC_ALLOW_PATH_ORDER_CHANGES = 0x00002000

ERROR_SUCCESS = 0
ERROR_ACCESS_DENIED = 5
ERROR_INVALID_PARAMETER = 87
ERROR_NOT_SUPPORTED = 50
ERROR_INSUFFICIENT_BUFFER = 122
ERROR_GEN_FAILURE = 31

WIN32_ERRORS = {
    ERROR_ACCESS_DENIED: "access denied",
    ERROR_NOT_SUPPORTED: "not supported",
    ERROR_INVALID_PARAMETER: "invalid parameter",
    ERROR_GEN_FAILURE: "device failure",
}

# Output technology, so the UI can show which port a monitor is plugged into.
OUTPUT_TECHNOLOGY = {
    0: "VGA",
    1: "S-Video",
    2: "Composite",
    3: "Component",
    4: "DVI",
    5: "HDMI",
    6: "LVDS",
    8: "D-Jpn",
    9: "SDI",
    10: "DisplayPort",
    11: "DisplayPort",
    12: "UDI",
    13: "UDI",
    14: "SDTV",
    15: "Miracast",
    16: "Indirect Wired",
    17: "Indirect Virtual",
    18: "DisplayPort",
    0x80000000: "Internal",
    0xFFFFFFFF: "Other",
}

COLOR_ENCODING = {0: "RGB", 1: "YCbCr444", 2: "YCbCr422", 3: "YCbCr420",
                  4: "Intensity"}

# ---------------------------------------------------------------- GDI structs


class _DEVMODE_POS(ctypes.Structure):
    _fields_ = [
        ("dmPosition", POINTL),
        ("dmDisplayOrientation", DWORD),
        ("dmDisplayFixedOutput", DWORD),
    ]


class _DEVMODE_PRINT(ctypes.Structure):
    _fields_ = [
        ("dmOrientation", ctypes.c_short),
        ("dmPaperSize", ctypes.c_short),
        ("dmPaperLength", ctypes.c_short),
        ("dmPaperWidth", ctypes.c_short),
        ("dmScale", ctypes.c_short),
        ("dmCopies", ctypes.c_short),
        ("dmDefaultSource", ctypes.c_short),
        ("dmPrintQuality", ctypes.c_short),
    ]


class _DEVMODE_U1(ctypes.Union):
    # Anonymous at both levels so dm.dmPosition / dm.dmDisplayOrientation
    # resolve straight off DEVMODEW, the way the C headers spell them.
    _anonymous_ = ("printer", "display")
    _fields_ = [("printer", _DEVMODE_PRINT), ("display", _DEVMODE_POS)]


class _DEVMODE_U2(ctypes.Union):
    _fields_ = [("dmDisplayFlags", DWORD), ("dmNup", DWORD)]


class DEVMODEW(ctypes.Structure):
    _anonymous_ = ("u1", "u2")
    _fields_ = [
        ("dmDeviceName", WCHAR * CCHDEVICENAME),
        ("dmSpecVersion", WORD),
        ("dmDriverVersion", WORD),
        ("dmSize", WORD),
        ("dmDriverExtra", WORD),
        ("dmFields", DWORD),
        ("u1", _DEVMODE_U1),
        ("dmColor", ctypes.c_short),
        ("dmDuplex", ctypes.c_short),
        ("dmYResolution", ctypes.c_short),
        ("dmTTOption", ctypes.c_short),
        ("dmCollate", ctypes.c_short),
        ("dmFormName", WCHAR * CCHFORMNAME),
        ("dmLogPixels", WORD),
        ("dmBitsPerPel", DWORD),
        ("dmPelsWidth", DWORD),
        ("dmPelsHeight", DWORD),
        ("u2", _DEVMODE_U2),
        ("dmDisplayFrequency", DWORD),
        ("dmICMMethod", DWORD),
        ("dmICMIntent", DWORD),
        ("dmMediaType", DWORD),
        ("dmDitherType", DWORD),
        ("dmReserved1", DWORD),
        ("dmReserved2", DWORD),
        ("dmPanningWidth", DWORD),
        ("dmPanningHeight", DWORD),
    ]


class DISPLAY_DEVICEW(ctypes.Structure):
    _fields_ = [
        ("cb", DWORD),
        ("DeviceName", WCHAR * 32),
        ("DeviceString", WCHAR * 128),
        ("StateFlags", DWORD),
        ("DeviceID", WCHAR * 128),
        ("DeviceKey", WCHAR * 128),
    ]


DM_ORIENTATION = 0x00000001
DM_POSITION = 0x00000020
DM_DISPLAYORIENTATION = 0x00000080
DM_BITSPERPEL = 0x00040000
DM_PELSWIDTH = 0x00080000
DM_PELSHEIGHT = 0x00100000
DM_DISPLAYFLAGS = 0x00200000
DM_DISPLAYFREQUENCY = 0x00400000
DM_DISPLAYFIXEDOUTPUT = 0x20000000

CDS_UPDATEREGISTRY = 0x00000001
CDS_TEST = 0x00000002
CDS_SET_PRIMARY = 0x00000010
CDS_RESET = 0x40000000
CDS_NORESET = 0x10000000

ENUM_CURRENT_SETTINGS = 0xFFFFFFFF
ENUM_REGISTRY_SETTINGS = 0xFFFFFFFE

DISPLAY_DEVICE_ATTACHED_TO_DESKTOP = 0x00000001
DISPLAY_DEVICE_PRIMARY_DEVICE = 0x00000004
DISPLAY_DEVICE_MIRRORING_DRIVER = 0x00000008

DISP_CHANGE = {
    0: "successful",
    1: "restart required",
    -1: "failed",
    -2: "mode not supported by this monitor",
    -3: "settings could not be written to the registry",
    -4: "bad flags",
    -5: "bad parameter",
    -6: "bad dual view",
}

DMDO_DEFAULT = 0
DMDO_90 = 1
DMDO_180 = 2
DMDO_270 = 3

ROTATION_TO_DMDO = {0: DMDO_DEFAULT, 90: DMDO_90, 180: DMDO_180,
                    270: DMDO_270}
DMDO_TO_ROTATION = {v: k for k, v in ROTATION_TO_DMDO.items()}

# ----------------------------------------------------------------- prototypes

user32.GetDisplayConfigBufferSizes.argtypes = [UINT32, POINTER(UINT32),
                                               POINTER(UINT32)]
user32.GetDisplayConfigBufferSizes.restype = LONG

user32.QueryDisplayConfig.argtypes = [
    UINT32, POINTER(UINT32), POINTER(DISPLAYCONFIG_PATH_INFO),
    POINTER(UINT32), POINTER(DISPLAYCONFIG_MODE_INFO), ctypes.c_void_p,
]
user32.QueryDisplayConfig.restype = LONG

user32.SetDisplayConfig.argtypes = [
    UINT32, POINTER(DISPLAYCONFIG_PATH_INFO),
    UINT32, POINTER(DISPLAYCONFIG_MODE_INFO), UINT32,
]
user32.SetDisplayConfig.restype = LONG

user32.DisplayConfigGetDeviceInfo.argtypes = [
    POINTER(DISPLAYCONFIG_DEVICE_INFO_HEADER)]
user32.DisplayConfigGetDeviceInfo.restype = LONG

user32.DisplayConfigSetDeviceInfo.argtypes = [
    POINTER(DISPLAYCONFIG_DEVICE_INFO_HEADER)]
user32.DisplayConfigSetDeviceInfo.restype = LONG

user32.EnumDisplayDevicesW.argtypes = [ctypes.c_wchar_p, DWORD,
                                       POINTER(DISPLAY_DEVICEW), DWORD]
user32.EnumDisplayDevicesW.restype = BOOL

user32.EnumDisplaySettingsExW.argtypes = [ctypes.c_wchar_p, DWORD,
                                          POINTER(DEVMODEW), DWORD]
user32.EnumDisplaySettingsExW.restype = BOOL

user32.ChangeDisplaySettingsExW.argtypes = [
    ctypes.c_wchar_p, POINTER(DEVMODEW), HWND, DWORD, ctypes.c_void_p]
user32.ChangeDisplaySettingsExW.restype = LONG


def win32_error(code):
    return WIN32_ERRORS.get(code, "error {}".format(code))


def query_display_config(flags=QDC_ALL_PATHS):
    """Return (paths, modes) lists for the requested path set.

    The buffer can go stale between sizing and filling if a display is
    hotplugged, which surfaces as ERROR_INSUFFICIENT_BUFFER; just retry.
    """
    for _ in range(8):
        n_paths = UINT32()
        n_modes = UINT32()
        rc = user32.GetDisplayConfigBufferSizes(flags, byref(n_paths),
                                                byref(n_modes))
        if rc != ERROR_SUCCESS:
            raise OSError("GetDisplayConfigBufferSizes: " + win32_error(rc))
        paths = (DISPLAYCONFIG_PATH_INFO * n_paths.value)()
        modes = (DISPLAYCONFIG_MODE_INFO * n_modes.value)()
        rc = user32.QueryDisplayConfig(flags, byref(n_paths), paths,
                                       byref(n_modes), modes, None)
        if rc == ERROR_SUCCESS:
            return list(paths[:n_paths.value]), list(modes[:n_modes.value])
        if rc != ERROR_INSUFFICIENT_BUFFER:
            raise OSError("QueryDisplayConfig: " + win32_error(rc))
    raise OSError("QueryDisplayConfig kept resizing its buffer")
