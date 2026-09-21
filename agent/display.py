"""High level monitor enumeration and control.

Everything the HTTP API exposes goes through here. Monitors are identified by
a stable key derived from the monitor device path (which encodes the EDID and
the physical connector), so a key keeps pointing at the same panel across
reboots and across enable/disable cycles -- unlike GDI names such as
\\\\.\\DISPLAY3, which get reshuffled whenever the topology changes.
"""

import hashlib
import threading
import time
from ctypes import byref, sizeof

import ccd

# Serialises every mutating operation. Two concurrent SetDisplayConfig calls
# race in the driver and can leave the desktop in a half-applied state.
_lock = threading.RLock()

# How long to let the driver settle after a monitor is attached or detached
# before reading its modes back, and how many times a rollback re-tries the
# geometry pass before admitting defeat.
SETTLE_SECONDS = 0.6
LAYOUT_RETRIES = 3


class DisplayError(Exception):
    """A display operation failed for a reason worth showing the user."""


# ----------------------------------------------------------------- enumerate


def _device_info(struct_type, info_type, adapter_id, target_id):
    info = struct_type()
    info.header.type = info_type
    info.header.size = sizeof(info)
    info.header.adapterId = adapter_id
    info.header.id = target_id
    rc = ccd.user32.DisplayConfigGetDeviceInfo(byref(info.header))
    return info if rc == ccd.ERROR_SUCCESS else None


def _target_name(adapter_id, target_id):
    return _device_info(ccd.DISPLAYCONFIG_TARGET_DEVICE_NAME,
                        ccd.DISPLAYCONFIG_DEVICE_INFO_GET_TARGET_NAME,
                        adapter_id, target_id)


def _source_gdi_name(adapter_id, source_id):
    info = _device_info(ccd.DISPLAYCONFIG_SOURCE_DEVICE_NAME,
                        ccd.DISPLAYCONFIG_DEVICE_INFO_GET_SOURCE_NAME,
                        adapter_id, source_id)
    return info.viewGdiDeviceName if info else ""


def _hdr_info(adapter_id, target_id):
    info = _device_info(ccd.DISPLAYCONFIG_GET_ADVANCED_COLOR_INFO,
                        ccd.DISPLAYCONFIG_DEVICE_INFO_GET_ADVANCED_COLOR_INFO,
                        adapter_id, target_id)
    if info is None:
        return {"supported": False, "enabled": False, "forceDisabled": False,
                "encoding": None, "bitsPerChannel": None, "readable": False}
    return {
        "supported": info.supported,
        "enabled": info.enabled,
        "forceDisabled": info.force_disabled,
        "encoding": ccd.COLOR_ENCODING.get(info.colorEncoding),
        "bitsPerChannel": info.bitsPerColorChannel,
        "readable": True,
    }


def _sdr_white_level(adapter_id, target_id):
    info = _device_info(ccd.DISPLAYCONFIG_SDR_WHITE_LEVEL,
                        ccd.DISPLAYCONFIG_DEVICE_INFO_GET_SDR_WHITE_LEVEL,
                        adapter_id, target_id)
    if info is None or not info.SDRWhiteLevel:
        return None
    # Raw value is SDR white in units of 1/1000 nit scaled so 1000 == 80 nits.
    return round(info.SDRWhiteLevel / 1000.0 * 80.0)


def _preferred_mode(adapter_id, target_id):
    info = _device_info(ccd.DISPLAYCONFIG_TARGET_PREFERRED_MODE,
                        ccd.DISPLAYCONFIG_DEVICE_INFO_GET_TARGET_PREFERRED_MODE,
                        adapter_id, target_id)
    if info is None or not info.width:
        return None
    signal = info.targetMode.targetVideoSignalInfo
    return {"width": int(info.width), "height": int(info.height),
            "hz": round(signal.vSyncFreq.hz(), 3)}


def _stable_key(device_path, output_technology, target_id):
    """A durable id for a monitor, used in URLs and saved profiles.

    The device path is the good case: it carries the EDID hardware id and the
    connector, so it survives reboots and re-plugs into the same port.

    The fallback deliberately avoids the adapter LUID even though it would
    disambiguate better -- Windows reassigns LUIDs on every boot, so a key
    built from one would change each time the machine restarts and strand
    every profile that referenced it.
    """
    if device_path:
        seed = device_path
    else:
        seed = "target:{}:{}".format(output_technology, target_id)
    return hashlib.sha1(seed.encode("utf-8", "replace")).hexdigest()[:12]


def _gdi_devices():
    """Map GDI device name -> {string, primary, attached} for every adapter."""
    result = {}
    index = 0
    while True:
        dev = ccd.DISPLAY_DEVICEW()
        dev.cb = sizeof(dev)
        if not ccd.user32.EnumDisplayDevicesW(None, index, byref(dev), 0):
            break
        index += 1
        if dev.StateFlags & ccd.DISPLAY_DEVICE_MIRRORING_DRIVER:
            continue
        result[dev.DeviceName] = {
            "adapter": dev.DeviceString,
            "primary": bool(dev.StateFlags & ccd.DISPLAY_DEVICE_PRIMARY_DEVICE),
            "attached": bool(
                dev.StateFlags & ccd.DISPLAY_DEVICE_ATTACHED_TO_DESKTOP),
        }
    return result


def _current_devmode(gdi_name):
    dm = ccd.DEVMODEW()
    dm.dmSize = sizeof(dm)
    ok = ccd.user32.EnumDisplaySettingsExW(gdi_name,
                                           ccd.ENUM_CURRENT_SETTINGS,
                                           byref(dm), 0)
    return dm if ok else None


def list_modes(gdi_name):
    """Every 32-bit colour mode the driver reports for this output."""
    modes = set()
    index = 0
    while True:
        dm = ccd.DEVMODEW()
        dm.dmSize = sizeof(dm)
        if not ccd.user32.EnumDisplaySettingsExW(gdi_name, index,
                                                 byref(dm), 0):
            break
        index += 1
        if dm.dmBitsPerPel != 32:
            continue
        if dm.dmDisplayFixedOutput != 0:
            # Scaled/centered variants of a mode the panel already reports.
            continue
        modes.add((int(dm.dmPelsWidth), int(dm.dmPelsHeight),
                   int(dm.dmDisplayFrequency)))
    out = [{"width": w, "height": h, "hz": hz} for w, h, hz in modes]
    out.sort(key=lambda m: (-m["width"] * m["height"], -m["hz"]))
    return out


def _source_key(path):
    return (path.sourceInfo.adapterId.key(), int(path.sourceInfo.id))


def _active_sources(paths, excluding=None):
    """Sources currently driving a display.

    Two active paths sharing a source are a clone pair -- they present the
    same desktop surface. Extending means every active path owns a distinct
    source, so this is the set to avoid when lighting up a new display.
    """
    used = set()
    for path in paths:
        if not path.flags & ccd.DISPLAYCONFIG_PATH_ACTIVE:
            continue
        target = path.targetInfo
        if excluding is not None:
            if (target.adapterId.key(), int(target.id)) == excluding:
                continue
        used.add(_source_key(path))
    return used


def _collect_paths():
    """All CCD paths, with the best (active-preferred) path per target."""
    paths, modes = ccd.query_display_config(ccd.QDC_ALL_PATHS)
    best = {}
    for path in paths:
        target = path.targetInfo
        key = (target.adapterId.key(), target.id)
        active = bool(path.flags & ccd.DISPLAYCONFIG_PATH_ACTIVE)
        if key not in best or (active and not best[key][1]):
            best[key] = (path, active)
    return paths, modes, best


def list_monitors():
    """Describe every monitor Windows can see, enabled or not."""
    with _lock:
        _, _, best = _collect_paths()
        gdi = _gdi_devices()

        monitors = []
        for (adapter_key, target_id), (path, active) in best.items():
            target = path.targetInfo
            name_info = _target_name(target.adapterId, target.id)
            device_path = name_info.monitorDevicePath if name_info else ""
            friendly = (name_info.monitorFriendlyDeviceName
                        if name_info else "") or ""

            # Outputs with no device path are connectors with nothing plugged
            # in. The GPU reports dozens of them; they are not monitors.
            if not active and not device_path:
                continue

            gdi_name = _source_gdi_name(path.sourceInfo.adapterId,
                                        path.sourceInfo.id) if active else ""
            gdi_meta = gdi.get(gdi_name, {})

            entry = {
                "key": _stable_key(device_path, target.outputTechnology,
                                   target.id),
                "name": friendly or "Display",
                "port": ccd.OUTPUT_TECHNOLOGY.get(target.outputTechnology,
                                                  "Unknown"),
                "adapter": gdi_meta.get("adapter", ""),
                "active": active,
                "primary": bool(gdi_meta.get("primary")),
                "gdiName": gdi_name,
                "devicePath": device_path,
                "native": _preferred_mode(target.adapterId, target.id),
                "hdr": _hdr_info(target.adapterId, target.id),
                "sdrWhiteNits": _sdr_white_level(target.adapterId, target.id),
                "modes": [],
                "rect": None,
                "current": None,
                "rotation": 0,
                # Active displays sharing a sourceGroup are duplicating each
                # other. adapterKey says which displays *could* be paired:
                # cloning only works within one graphics adapter.
                "sourceGroup": ("{}:{}".format(
                    path.sourceInfo.adapterId.key(), path.sourceInfo.id)
                    if active else None),
                "adapterKey": str(adapter_key),
                "duplicateOf": None,
                "_adapterId": adapter_key,
                "_targetId": int(target.id),
            }

            if active and gdi_name:
                dm = _current_devmode(gdi_name)
                if dm is not None:
                    entry["rect"] = {
                        "x": int(dm.dmPosition.x), "y": int(dm.dmPosition.y),
                        "width": int(dm.dmPelsWidth),
                        "height": int(dm.dmPelsHeight),
                    }
                    rotation = ccd.DMDO_TO_ROTATION.get(
                        int(dm.dmDisplayOrientation), 0)
                    entry["rotation"] = rotation
                    # dmPelsWidth/Height are in rotated desktop space. Report
                    # the mode in panel space so it lines up with the entries
                    # in "modes", which the driver reports unrotated.
                    mode_w, mode_h = int(dm.dmPelsWidth), int(dm.dmPelsHeight)
                    if rotation % 180:
                        mode_w, mode_h = mode_h, mode_w
                    entry["current"] = {
                        "width": mode_w,
                        "height": mode_h,
                        "hz": int(dm.dmDisplayFrequency),
                        "exactHz": round(target.refreshRate.hz(), 3),
                        "bitsPerPixel": int(dm.dmBitsPerPel),
                    }
                entry["modes"] = list_modes(gdi_name)

            entry["present"] = True
            monitors.append(entry)

        disambiguate(monitors)
        monitors.sort(key=lambda m: (
            not m["active"], m["rect"]["x"] if m["rect"] else 0, m["label"]))
        _mark_duplicates(monitors)
        return monitors


def _mark_duplicates(monitors):
    """Point each cloned display at the one it is duplicating.

    The first display in a clone group is treated as the original purely so
    the UI has something stable to name; Windows itself has no such notion.
    """
    groups = {}
    for mon in monitors:
        if not mon["active"] or not mon["sourceGroup"]:
            continue
        groups.setdefault(mon["sourceGroup"], []).append(mon)
    for members in groups.values():
        if len(members) < 2:
            continue
        original = members[0]
        for clone in members[1:]:
            clone["duplicateOf"] = original["key"]


def disambiguate(monitors):
    """Two identical panels report the same friendly name; make labels unique.

    Public because the server calls it again after folding in monitors it
    remembers but cannot currently see -- otherwise a name that only needs a
    suffix because of an absent twin would change every time that twin was
    switched off.
    """
    counts = {}
    for mon in monitors:
        # Panels that report no EDID name at all still need something to be
        # called; the port suffix below is what actually tells them apart.
        mon["name"] = mon.get("name") or "Display"
        counts[mon["name"]] = counts.get(mon["name"], 0) + 1

    # Number each group by something fixed to the panel, not by the order
    # they happen to appear in. Numbering by position would renumber every
    # identical monitor whenever one was switched on, moved, or woke up --
    # so "#1" would stop meaning the same physical screen.
    numbers = {}
    for name, count in counts.items():
        if count < 2:
            continue
        group = [mon for mon in monitors if mon["name"] == name]
        group.sort(key=lambda m: (m.get("devicePath") or "", m["key"]))
        for index, mon in enumerate(group, 1):
            numbers[mon["key"]] = index

    for mon in monitors:
        number = numbers.get(mon["key"])
        if number is None:
            mon["label"] = mon["name"]
        else:
            mon["label"] = "{} ({} #{})".format(
                mon["name"], mon.get("port") or "Unknown", number)


def _find(monitors, key):
    for mon in monitors:
        if mon["key"] == key:
            return mon
    raise DisplayError("No monitor with id {}".format(key))


def _luid(adapter_key):
    luid = ccd.LUID()
    luid.LowPart, luid.HighPart = adapter_key
    return luid


# -------------------------------------------------------------------- mutate


def set_hdr(key, enabled):
    """Turn HDR (advanced colour) on or off for one monitor."""
    with _lock:
        mon = _find(list_monitors(), key)
        if not mon["active"]:
            raise DisplayError(
                "{} is disabled; enable it before changing HDR."
                .format(mon["label"]))
        if not mon["hdr"]["supported"]:
            raise DisplayError(
                "{} does not report HDR support on this connection."
                .format(mon["label"]))
        if mon["hdr"]["forceDisabled"]:
            raise DisplayError(
                "Windows has HDR force-disabled for {} (usually a driver or "
                "cable bandwidth limit).".format(mon["label"]))

        state = ccd.DISPLAYCONFIG_SET_ADVANCED_COLOR_STATE()
        state.header.type = ccd.DISPLAYCONFIG_DEVICE_INFO_SET_ADVANCED_COLOR_STATE
        state.header.size = sizeof(state)
        state.header.adapterId = _luid(mon["_adapterId"])
        state.header.id = mon["_targetId"]
        state.value = 1 if enabled else 0

        rc = ccd.user32.DisplayConfigSetDeviceInfo(byref(state.header))
        if rc != ccd.ERROR_SUCCESS:
            raise DisplayError("Could not set HDR on {}: {}".format(
                mon["label"], ccd.win32_error(rc)))
        return list_monitors()


def set_enabled(key, enabled):
    """Attach or detach a monitor from the desktop."""
    with _lock:
        monitors = list_monitors()
        mon = _find(monitors, key)
        if mon["active"] == enabled:
            return monitors
        if not enabled and sum(1 for m in monitors if m["active"]) <= 1:
            raise DisplayError(
                "Refusing to disable the only active display -- you would "
                "have no way to see the desktop.")

        paths, modes = ccd.query_display_config(ccd.QDC_ALL_PATHS)
        target_key = (mon["_adapterId"], mon["_targetId"])
        invalid = ccd.DISPLAYCONFIG_PATH_MODE_IDX_INVALID

        touched = False
        if enabled:
            # Pick a path whose source is not already driving another
            # display. Taking the first available path instead would often
            # land on a source that is already in use, and Windows reads
            # "two active paths, one source" as a request to duplicate.
            used = _active_sources(paths)
            candidates = [p for p in paths
                          if (p.targetInfo.adapterId.key(),
                              int(p.targetInfo.id)) == target_key
                          and not p.flags & ccd.DISPLAYCONFIG_PATH_ACTIVE]
            candidates.sort(key=lambda p: int(p.sourceInfo.id))
            chosen = next((p for p in candidates
                           if _source_key(p) not in used), None)
            if chosen is None and candidates:
                # Every source on this adapter is spoken for, so the only
                # way to light this display is alongside an existing one.
                chosen = candidates[0]
            if chosen is not None:
                chosen.flags |= ccd.DISPLAYCONFIG_PATH_ACTIVE
                # The mode entries describe the *old* topology, so a path we
                # change must not point into them; Windows rejects the whole
                # call otherwise. Invalid indices tell it to work out the
                # modes for this path itself.
                chosen.sourceInfo.modeInfoIdx = invalid
                chosen.targetInfo.modeInfoIdx = invalid
                touched = True
        else:
            for path in paths:
                tgt = path.targetInfo
                if (tgt.adapterId.key(), int(tgt.id)) != target_key:
                    continue
                if not path.flags & ccd.DISPLAYCONFIG_PATH_ACTIVE:
                    continue
                path.flags &= ~ccd.DISPLAYCONFIG_PATH_ACTIVE
                path.sourceInfo.modeInfoIdx = invalid
                path.targetInfo.modeInfoIdx = invalid
                touched = True

        if not touched:
            raise DisplayError(
                "No display path available for {}.".format(mon["label"]))

        if not enabled:
            _rehome_to_origin(paths, modes)

        path_array = (ccd.DISPLAYCONFIG_PATH_INFO * len(paths))(*paths)
        mode_array = (ccd.DISPLAYCONFIG_MODE_INFO * len(modes))(*modes)
        flags = (ccd.SDC_APPLY | ccd.SDC_USE_SUPPLIED_DISPLAY_CONFIG
                 | ccd.SDC_ALLOW_CHANGES | ccd.SDC_SAVE_TO_DATABASE)
        rc = ccd.user32.SetDisplayConfig(len(paths), path_array, len(modes),
                                         mode_array, flags)
        if rc != ccd.ERROR_SUCCESS:
            raise DisplayError("Could not {} {}: {}".format(
                "enable" if enabled else "disable", mon["label"],
                ccd.win32_error(rc)))
        return list_monitors()


def set_mirror(key, mirror_of):
    """Duplicate a display onto another, or give it its own desktop again.

    ``mirror_of`` is the key of the display to duplicate, or None to extend.
    Duplication is a source-level idea: two active paths pointing at one
    source show the same surface. Extending gives the display a source of
    its own.
    """
    with _lock:
        monitors = list_monitors()
        mon = _find(monitors, key)
        if not mon["active"]:
            raise DisplayError(
                "{} is switched off; turn it on first.".format(mon["label"]))

        other = None
        if mirror_of:
            other = _find(monitors, mirror_of)
            if other["key"] == mon["key"]:
                raise DisplayError("A display cannot duplicate itself.")
            if not other["active"]:
                raise DisplayError(
                    "{} is switched off, so there is nothing to duplicate."
                    .format(other["label"]))
            if other["adapterKey"] != mon["adapterKey"]:
                raise DisplayError(
                    "{} and {} are on different graphics adapters, and "
                    "Windows can only duplicate displays driven by the same "
                    "one.".format(mon["label"], other["label"]))
            if mon["sourceGroup"] == other["sourceGroup"]:
                return monitors  # already duplicating it
        elif not any(m is not mon and m["active"]
                     and m["sourceGroup"] == mon["sourceGroup"]
                     for m in monitors):
            return monitors  # already has its own desktop

        paths, modes = ccd.query_display_config(ccd.QDC_ALL_PATHS)
        invalid = ccd.DISPLAYCONFIG_PATH_MODE_IDX_INVALID
        target_key = (mon["_adapterId"], mon["_targetId"])

        path = next((p for p in paths
                     if (p.targetInfo.adapterId.key(),
                         int(p.targetInfo.id)) == target_key
                     and p.flags & ccd.DISPLAYCONFIG_PATH_ACTIVE), None)
        if path is None:
            raise DisplayError(
                "No active path for {}.".format(mon["label"]))

        if other is not None:
            source = next((p.sourceInfo for p in paths
                           if (p.targetInfo.adapterId.key(),
                               int(p.targetInfo.id))
                           == (other["_adapterId"], other["_targetId"])
                           and p.flags & ccd.DISPLAYCONFIG_PATH_ACTIVE), None)
            if source is None:
                raise DisplayError(
                    "No active path for {}.".format(other["label"]))
            path.sourceInfo.adapterId = source.adapterId
            path.sourceInfo.id = source.id
        else:
            used = _active_sources(paths, excluding=target_key)
            candidates = sorted(
                {int(p.sourceInfo.id) for p in paths
                 if (p.targetInfo.adapterId.key(),
                     int(p.targetInfo.id)) == target_key
                 and (p.sourceInfo.adapterId.key(),
                      int(p.sourceInfo.id)) not in used})
            if not candidates:
                raise DisplayError(
                    "No spare display source left on this adapter, so {} "
                    "cannot have its own desktop.".format(mon["label"]))
            path.sourceInfo.id = candidates[0]

        path.sourceInfo.modeInfoIdx = invalid
        path.targetInfo.modeInfoIdx = invalid

        path_array = (ccd.DISPLAYCONFIG_PATH_INFO * len(paths))(*paths)
        mode_array = (ccd.DISPLAYCONFIG_MODE_INFO * len(modes))(*modes)
        flags = (ccd.SDC_APPLY | ccd.SDC_USE_SUPPLIED_DISPLAY_CONFIG
                 | ccd.SDC_ALLOW_CHANGES | ccd.SDC_SAVE_TO_DATABASE)
        rc = ccd.user32.SetDisplayConfig(len(paths), path_array, len(modes),
                                         mode_array, flags)
        if rc != ccd.ERROR_SUCCESS:
            raise DisplayError("Could not {}: {}".format(
                "duplicate {} onto {}".format(mon["label"], other["label"])
                if other else "give {} its own desktop".format(mon["label"]),
                ccd.win32_error(rc)))
        return list_monitors()


def _rehome_to_origin(paths, modes):
    """Make sure some surviving display still sits at (0, 0).

    Windows will not accept a desktop with no source at the origin, so
    switching off the primary fails outright unless the remaining displays
    are slid across to take its place. Shifting every source by the same
    amount keeps their relative arrangement intact.
    """
    invalid = ccd.DISPLAYCONFIG_PATH_MODE_IDX_INVALID
    positions = []
    for path in paths:
        if not path.flags & ccd.DISPLAYCONFIG_PATH_ACTIVE:
            continue
        index = path.sourceInfo.modeInfoIdx
        if index == invalid or index >= len(modes):
            continue
        mode = modes[index]
        if mode.infoType != ccd.DISPLAYCONFIG_MODE_INFO_TYPE_SOURCE:
            continue
        positions.append((mode.u.sourceMode.position.x,
                          mode.u.sourceMode.position.y))

    if not positions or (0, 0) in positions:
        return

    # Promote the top-left survivor, which keeps the desktop where the user
    # expects it rather than jumping to whichever path happens to be first.
    dx, dy = min(positions)
    for mode in modes:
        if mode.infoType == ccd.DISPLAYCONFIG_MODE_INFO_TYPE_SOURCE:
            mode.u.sourceMode.position.x -= dx
            mode.u.sourceMode.position.y -= dy


def _stage(gdi_name, dm, flags):
    rc = ccd.user32.ChangeDisplaySettingsExW(gdi_name, byref(dm), None,
                                             flags, None)
    return rc


def apply_layout(changes, commit=True, validate=True):
    """Apply position / resolution / refresh / rotation / primary in one shot.

    ``changes`` maps a monitor key to any of: x, y, width, height, hz,
    rotation, primary. Every change is staged with CDS_NORESET first and then
    committed together, so the desktop reflows once instead of once per
    monitor.

    ``validate`` checks requested modes against the driver's list before
    trying them, which gives a far better error message than Windows does.
    Rollbacks turn it off: a monitor that has just been switched back on can
    briefly under-report its modes, and refusing to restore a mode the
    monitor was demonstrably running a moment ago is worse than trying it.
    CDS_TEST still rejects anything genuinely impossible.
    """
    with _lock:
        monitors = list_monitors()
        active = {m["key"]: m for m in monitors if m["active"]}

        for key in changes:
            if key not in active:
                mon = _find(monitors, key)
                raise DisplayError(
                    "{} is disabled; enable it before changing its layout."
                    .format(mon["label"]))

        # Work out the final geometry of every active monitor, whether or not
        # it was named in the request, so we can normalise the origin.
        # Duplicated displays share one GDI device and therefore one
        # rectangle, so only the first of a clone group is planned; staging
        # the same device twice would just fight with itself.
        plan = {}
        planned_devices = set()
        # Displays the caller actually asked about go first, so when a clone
        # group is collapsed below it is their values that survive.
        ordered = sorted(active.items(), key=lambda kv: kv[0] not in changes)
        for key, mon in ordered:
            if mon["gdiName"] in planned_devices:
                continue
            planned_devices.add(mon["gdiName"])
            change = changes.get(key, {})
            rect = mon["rect"] or {"x": 0, "y": 0, "width": 0, "height": 0}
            current = mon["current"] or {}
            width = int(change.get("width", current.get("width", 0)))
            height = int(change.get("height", current.get("height", 0)))
            hz = int(change.get("hz", current.get("hz", 0)))
            rotation = int(change.get("rotation", mon["rotation"]))
            if rotation not in ccd.ROTATION_TO_DMDO:
                raise DisplayError(
                    "Rotation must be 0, 90, 180 or 270 (got {}).".format(
                        rotation))
            if validate and width and height and mon["modes"]:
                if not any(m["width"] == width and m["height"] == height
                           and m["hz"] == hz for m in mon["modes"]):
                    raise DisplayError(
                        "{} does not support {}x{} @ {} Hz.".format(
                            mon["label"], width, height, hz))
            plan[key] = {
                "mon": mon,
                "x": int(change.get("x", rect["x"])),
                "y": int(change.get("y", rect["y"])),
                "width": width,
                "height": height,
                "hz": hz,
                "rotation": rotation,
                "primary": bool(change.get("primary", mon["primary"])),
            }

        primaries = [k for k, p in plan.items() if p["primary"]]
        if len(primaries) > 1:
            requested = [k for k in changes if changes[k].get("primary")]
            if len(requested) != 1:
                raise DisplayError("Exactly one monitor must be primary.")
            for key in primaries:
                plan[key]["primary"] = key == requested[0]
            primaries = requested
        if not primaries:
            raise DisplayError("One monitor must be primary.")

        # Windows requires the primary display to sit at the origin; every
        # other monitor is positioned relative to it.
        origin = plan[primaries[0]]
        off_x, off_y = origin["x"], origin["y"]
        for entry in plan.values():
            entry["x"] -= off_x
            entry["y"] -= off_y

        # Two passes, each using the API that handles it reliably.
        #
        # Modes -- resolution, refresh, rotation -- go through
        # ChangeDisplaySettingsEx. Arrangement -- positions and which display
        # is primary -- goes through CCD, where "primary" is simply whichever
        # source sits at (0, 0). ChangeDisplaySettingsEx's CDS_SET_PRIMARY
        # refuses outright to move the primary between displays on different
        # graphics adapters, which is an ordinary setup; CCD does it fine.
        staged = []
        for key, entry in plan.items():
            mon = entry["mon"]
            current = mon["current"] or {}
            needs_mode = (
                (entry["width"] and (entry["width"], entry["height"])
                 != (current.get("width"), current.get("height")))
                or (entry["hz"] and entry["hz"] != current.get("hz"))
                or entry["rotation"] != mon["rotation"])
            if not needs_mode:
                continue
            dm = _current_devmode(mon["gdiName"])
            if dm is None:
                raise DisplayError(
                    "Could not read current settings for {}.".format(
                        mon["label"]))
            dm.dmFields = ccd.DM_DISPLAYORIENTATION
            dm.dmDisplayOrientation = ccd.ROTATION_TO_DMDO[entry["rotation"]]

            if entry["width"] and entry["height"]:
                # Incoming width/height are panel space (as listed in
                # "modes"); dmPelsWidth/Height want rotated desktop space.
                width, height = entry["width"], entry["height"]
                if entry["rotation"] % 180:
                    width, height = height, width
                dm.dmPelsWidth = width
                dm.dmPelsHeight = height
                dm.dmFields |= ccd.DM_PELSWIDTH | ccd.DM_PELSHEIGHT
            if entry["hz"]:
                dm.dmDisplayFrequency = entry["hz"]
                dm.dmFields |= ccd.DM_DISPLAYFREQUENCY

            staged.append((mon, dm, ccd.CDS_UPDATEREGISTRY | ccd.CDS_NORESET))

        # No per-display CDS_TEST pre-flight: it judges each display alone
        # against the layout as it stands now, so it rejects changes that are
        # only valid as a whole. Impossible modes are caught above against the
        # driver's mode list, and the staging calls report anything refused.
        if staged:
            # What each display is running now, captured before anything is
            # written, so a failure part-way can put the registry back.
            live = {mon["gdiName"]: _current_devmode(mon["gdiName"])
                    for mon, _dm, _flags in staged}
            done = []
            for mon, dm, flags in staged:
                rc = _stage(mon["gdiName"], dm, flags)
                if rc != 0:
                    # Never commit here. The registry may hold older values
                    # than the screens are actually running -- a refresh rate
                    # chosen in Windows Settings, say -- and committing would
                    # apply those. Rewrite what was already staged back to the
                    # live settings instead, and leave the screens untouched.
                    for undo in done:
                        original = live.get(undo["gdiName"])
                        if original is not None:
                            _stage(undo["gdiName"], original,
                                   ccd.CDS_UPDATEREGISTRY | ccd.CDS_NORESET)
                    raise DisplayError(
                        "Windows refused the new mode for {} ({}).".format(
                            mon["label"], ccd.DISP_CHANGE.get(rc, rc)))
                done.append(mon)
            if commit:
                rc = ccd.user32.ChangeDisplaySettingsExW(None, None, None, 0,
                                                         None)
                if rc != 0:
                    raise DisplayError(
                        "Windows refused the new modes ({}).".format(
                            ccd.DISP_CHANGE.get(rc, rc)))

        if commit:
            _arrange(plan)
        return list_monitors()


def _arrange(plan):
    """Put every display where the plan says, primary at the origin.

    Done through CCD by moving source modes: the display whose source lands
    on (0, 0) is the primary, so no separate "set primary" call exists to
    fail. Duplicated displays share a source and so move together.
    """
    wanted = {(entry["mon"]["_adapterId"], entry["mon"]["_targetId"]):
              (entry["x"], entry["y"]) for entry in plan.values()}

    paths, modes = ccd.query_display_config(ccd.QDC_ONLY_ACTIVE_PATHS)
    invalid = ccd.DISPLAYCONFIG_PATH_MODE_IDX_INVALID
    moved = False
    for path in paths:
        key = (path.targetInfo.adapterId.key(), int(path.targetInfo.id))
        if key not in wanted:
            continue
        index = path.sourceInfo.modeInfoIdx
        if index == invalid or index >= len(modes):
            continue
        source = modes[index].u.sourceMode
        x, y = wanted[key]
        if (source.position.x, source.position.y) != (x, y):
            source.position.x, source.position.y = x, y
            moved = True

    if not moved:
        return

    path_array = (ccd.DISPLAYCONFIG_PATH_INFO * len(paths))(*paths)
    mode_array = (ccd.DISPLAYCONFIG_MODE_INFO * len(modes))(*modes)
    rc = ccd.user32.SetDisplayConfig(
        len(paths), path_array, len(modes), mode_array,
        ccd.SDC_APPLY | ccd.SDC_USE_SUPPLIED_DISPLAY_CONFIG
        | ccd.SDC_ALLOW_CHANGES | ccd.SDC_SAVE_TO_DATABASE)
    if rc != ccd.ERROR_SUCCESS:
        raise DisplayError("Windows refused the new arrangement ({}).".format(
            ccd.win32_error(rc)))


# ------------------------------------------------------------------ snapshot


def snapshot():
    """Capture enough state to put the desktop back exactly as it was."""
    with _lock:
        monitors = list_monitors()
        return {
            "active": [m["key"] for m in monitors if m["active"]],
            "inactive": [m["key"] for m in monitors if not m["active"]],
            "layout": {
                m["key"]: {
                    "x": m["rect"]["x"], "y": m["rect"]["y"],
                    "width": m["current"]["width"],
                    "height": m["current"]["height"],
                    "hz": m["current"]["hz"],
                    "rotation": m["rotation"],
                    "primary": m["primary"],
                }
                for m in monitors if m["active"] and m["rect"] and m["current"]
            },
            "hdr": {m["key"]: m["hdr"]["enabled"] for m in monitors
                    if m["active"] and m["hdr"]["supported"]},
            "mirror": {m["key"]: m["duplicateOf"] for m in monitors
                       if m["active"]},
        }


def _layout_differences(snap):
    """Which monitors are not where the snapshot says they should be."""
    live = {m["key"]: m for m in list_monitors() if m["active"]}
    off = []
    for key, wanted in snap["layout"].items():
        mon = live.get(key)
        if mon is None or not mon["rect"] or not mon["current"]:
            continue
        actual = {
            "x": mon["rect"]["x"], "y": mon["rect"]["y"],
            "width": mon["current"]["width"],
            "height": mon["current"]["height"],
            "hz": mon["current"]["hz"], "rotation": mon["rotation"],
            "primary": mon["primary"],
        }
        if any(actual[field] != wanted[field] for field in wanted):
            off.append((key, mon["label"], actual))
    return off


def restore(snap):
    """Put the desktop back the way the snapshot describes it.

    This is the safety net the whole app leans on, so it does not give up
    after one try. Switching a monitor back on takes the driver a moment to
    settle, and a layout applied too early can quietly land on the wrong mode
    -- so the geometry pass runs, checks its own work, and retries.

    Returns a list of human-readable problems; empty means fully restored.
    """
    with _lock:
        errors = []
        current = {m["key"]: m for m in list_monitors()}
        topology_changed = False

        for key in snap["active"]:
            if key in current and not current[key]["active"]:
                try:
                    set_enabled(key, True)
                    topology_changed = True
                except DisplayError as exc:
                    errors.append(str(exc))
        for key in snap["inactive"]:
            if key in current and current[key]["active"]:
                try:
                    set_enabled(key, False)
                    topology_changed = True
                except DisplayError as exc:
                    errors.append(str(exc))

        # Put clone/extend relationships back before geometry: they decide
        # which displays even have a rectangle of their own.
        for key, mirror_of in snap.get("mirror", {}).items():
            live = {m["key"]: m for m in list_monitors()}
            mon = live.get(key)
            if mon is None or not mon["active"]:
                continue
            if mon["duplicateOf"] == mirror_of:
                continue
            try:
                set_mirror(key, mirror_of)
                topology_changed = True
            except DisplayError as exc:
                errors.append(str(exc))

        if topology_changed:
            # Give the driver a beat to republish modes for anything that was
            # just reattached, otherwise the geometry pass below reads a
            # half-populated mode list.
            time.sleep(SETTLE_SECONDS)

        if snap["layout"]:
            last_error = None
            for attempt in range(LAYOUT_RETRIES):
                live = {m["key"] for m in list_monitors() if m["active"]}
                layout = {k: v for k, v in snap["layout"].items()
                          if k in live}
                if not layout:
                    break
                try:
                    apply_layout(layout, validate=False)
                    last_error = None
                except DisplayError as exc:
                    last_error = str(exc)
                if not _layout_differences(snap):
                    break
                if attempt + 1 < LAYOUT_RETRIES:
                    time.sleep(SETTLE_SECONDS)
            if last_error:
                errors.append(last_error)
            for _, label, actual in _layout_differences(snap):
                errors.append(
                    "{} did not go back to {}x{} @ {} Hz at ({}, {})".format(
                        label, actual["width"], actual["height"],
                        actual["hz"], actual["x"], actual["y"]))

        live = {m["key"]: m for m in list_monitors()}
        for key, enabled in snap["hdr"].items():
            mon = live.get(key)
            if mon and mon["active"] and mon["hdr"]["enabled"] != enabled:
                try:
                    set_hdr(key, enabled)
                except DisplayError as exc:
                    errors.append(str(exc))
        return errors
