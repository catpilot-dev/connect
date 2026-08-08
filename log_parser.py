"""Log parsing utilities for Connect on Device.

Parses qlog/rlog files for route metadata extraction, GPS coordinates,
segment distances, and drive event generation. Prefers qlog (~400KB)
over rlog (~8MB) — both use the same capnp format.
Self-contained module using only stdlib + lazy cereal/zstandard imports.
"""

import logging
import math

logger = logging.getLogger("connect.log_parser")


def _iter_log(log_path: str):
    """Iterate over qlog/rlog messages using cereal directly (avoids LogReader path validation)."""
    from cereal import log as capnp_log
    import zstandard as zstd

    if log_path.endswith(".zst"):
        with open(log_path, "rb") as f:
            data = zstd.ZstdDecompressor().stream_reader(f).read()
    else:
        data = open(log_path, "rb").read()

    return capnp_log.Event.read_multiple_bytes(data)


def _iter_log_head(log_path: str, max_bytes: int = 5_000_000):
    """Iterate over the first max_bytes of decompressed log data.

    Reads only a bounded prefix — enough for initData (~msg 1),
    carParams (~msg 100), and often first GPS (~msg 500-5000) — while keeping
    memory usage bounded.
    """
    from cereal import log as capnp_log
    import zstandard as zstd

    if log_path.endswith(".zst"):
        with open(log_path, "rb") as f:
            data = zstd.ZstdDecompressor().stream_reader(f).read(max_bytes)
    else:
        with open(log_path, "rb") as f:
            data = f.read(max_bytes)

    return capnp_log.Event.read_multiple_bytes(data)


def _haversine_dist(lat1, lon1, lat2, lon2):
    """Distance in meters between two GPS points."""
    dlat = math.radians(lat2 - lat1)
    dlon = math.radians(lon2 - lon1)
    a = (math.sin(dlat / 2) ** 2
         + math.cos(math.radians(lat1)) * math.cos(math.radians(lat2))
         * math.sin(dlon / 2) ** 2)
    return 6371000 * 2 * math.asin(math.sqrt(a))


def _parse_log_metadata(log_path: str) -> dict:
    """Parse initData + first GPS + partial segment distance from a qlog/rlog file.

    Uses _iter_log_head() to read only the first ~5MB of decompressed data,
    keeping memory usage bounded. This covers initData, carParams, and often
    first GPS fix. Distance is partial (within the 5MB window only).
    """
    meta = {}
    has_init = False
    has_gps = False
    has_gps_time = False
    has_car = False
    last_lat = last_lng = None
    seg_dist = 0.0

    try:
        for ev in _iter_log_head(log_path):
            w = ev.which()
            if w == "initData" and not has_init:
                init = ev.initData
                meta["dongle_id"] = init.dongleId
                meta["git_commit"] = init.gitCommit
                meta["git_branch"] = init.gitBranch
                meta["git_remote"] = init.gitRemote
                meta["version"] = init.version
                meta["device_type"] = str(init.deviceType)
                wtn = getattr(init, "wallTimeNanos", 0)
                if wtn:
                    meta["wall_time_nanos"] = wtn
                    meta["create_time"] = wtn / 1e9
                has_init = True
            elif w == "carParams" and not has_car:
                try:
                    meta["car_fingerprint"] = ev.carParams.carFingerprint
                except Exception:
                    pass
                has_car = True
            elif w == "gpsLocationExternal":
                gps = ev.gpsLocationExternal
                lat, lng = gps.latitude, gps.longitude
                has_fix = bool(gps.flags & 1)
                has_coords = lat != 0 and lng != 0
                # Accept unfixed GPS for location (good enough for timezone)
                if has_coords and not has_gps:
                    meta["start_lat"] = lat
                    meta["start_lng"] = lng
                    has_gps = True
                # Capture GPS time from first fixed position
                if has_fix and has_coords and not has_gps_time:
                    gps_ms = getattr(gps, "unixTimestampMillis", 0)
                    if gps_ms > 0:
                        meta["gps_time"] = gps_ms / 1000.0
                        has_gps_time = True
                # Only use fixed GPS for distance calculation (unfixed can jitter)
                if has_fix and has_coords:
                    if last_lat is not None:
                        seg_dist += _haversine_dist(last_lat, last_lng, lat, lng)
                    last_lat, last_lng = lat, lng
    except Exception as e:
        logger.debug("rlog parse error: %s", e)

    # gps_time is stored separately — route naming uses GPS time exclusively.
    # wallTimeNanos/create_time kept as-is for reference only.

    if seg_dist > 0:
        meta["segment_distance_m"] = seg_dist

    return meta


def _find_first_gps(log_path: str) -> tuple | None:
    """Fast GPS-only scan — returns (lat, lng) or None.
    Accepts unfixed GPS (flags=0) if coordinates are non-zero,
    since that's sufficient for timezone estimation.
    Uses _iter_log_head() to cap memory at ~5MB."""
    try:
        for ev in _iter_log_head(log_path):
            if ev.which() == "gpsLocationExternal":
                gps = ev.gpsLocationExternal
                if gps.latitude != 0 and gps.longitude != 0:
                    return (gps.latitude, gps.longitude)
    except Exception:
        pass
    return None


def _find_first_gps_time(log_path: str) -> float | None:
    """Fast scan for first GPS time (unix seconds) from a fixed position.
    Returns None if no fixed GPS with valid timestamp found."""
    try:
        for ev in _iter_log_head(log_path):
            if ev.which() == "gpsLocationExternal":
                gps = ev.gpsLocationExternal
                if gps.flags & 1 and gps.latitude != 0 and gps.longitude != 0:
                    ms = getattr(gps, "unixTimestampMillis", 0)
                    if ms > 0:
                        return ms / 1000.0
    except Exception:
        pass
    return None


def _find_last_gps(log_path: str) -> tuple | None:
    """Scan full rlog and return last fixed GPS (lat, lng) or None."""
    last = None
    try:
        for ev in _iter_log(log_path):
            if ev.which() == "gpsLocationExternal":
                gps = ev.gpsLocationExternal
                if gps.flags & 1 and gps.latitude != 0 and gps.longitude != 0:
                    last = (gps.latitude, gps.longitude)
    except Exception:
        pass
    return last


def _segment_gps_distance(log_path: str) -> float:
    """Calculate GPS distance in meters for a single segment's rlog."""
    last_lat = last_lng = None
    dist = 0.0
    try:
        for ev in _iter_log(log_path):
            if ev.which() == "gpsLocationExternal":
                gps = ev.gpsLocationExternal
                if gps.flags & 1:
                    lat, lng = gps.latitude, gps.longitude
                    if last_lat is not None:
                        dist += _haversine_dist(last_lat, last_lng, lat, lng)
                    last_lat, last_lng = lat, lng
    except Exception:
        pass
    return dist


def _generate_coords_json(log_path: str, segment_num: int) -> list:
    """Generate GPS path data (coords.json) from an rlog file."""
    coords = []
    base_mono = None
    last_lat = last_lng = None
    total_dist = 0.0

    try:
        for ev in _iter_log(log_path):
            # Skip initData for base_mono — it has openpilot start time, not segment start
            if base_mono is None and ev.which() != "initData":
                base_mono = ev.logMonoTime

            if ev.which() == "gpsLocationExternal":
                gps = ev.gpsLocationExternal
                if not (gps.flags & 1):
                    continue

                lat, lng = gps.latitude, gps.longitude
                if base_mono is not None:
                    t = segment_num * 60.0 + (ev.logMonoTime - base_mono) / 1e9
                else:
                    t = segment_num * 60.0

                if last_lat is not None:
                    total_dist += _haversine_dist(last_lat, last_lng, lat, lng)

                last_lat, last_lng = lat, lng
                coords.append({
                    "t": round(t, 3),
                    "lng": round(lng, 7),
                    "lat": round(lat, 7),
                    "speed": round(gps.speed, 2),
                    "dist": round(total_dist, 1),
                })
    except Exception as e:
        logger.debug("coords generation error for %s: %s", log_path, e)

    return coords


def _extract_bookmarks(log_path: str, segment_num: int) -> list:
    """Extract userBookmark timestamps from an rlog file.

    Returns sorted list of route_offset_millis integers for each bookmark
    found in the segment.
    """
    bookmarks = []
    base_mono = None

    try:
        for ev in _iter_log(log_path):
            if base_mono is None and ev.which() != "initData":
                base_mono = ev.logMonoTime

            if ev.which() == "userBookmark":
                if base_mono is not None:
                    offset_ms = (ev.logMonoTime - base_mono) / 1e6
                else:
                    offset_ms = 0
                route_offset_ms = segment_num * 60000 + offset_ms
                bookmarks.append(round(route_offset_ms))
    except Exception as e:
        logger.debug("bookmark extraction error for %s: %s", log_path, e)

    return sorted(bookmarks)


def _extract_bookmark_epochs(log_path: str, segment_num: int) -> list:
    """Extract (route_offset_millis, wall_epoch) per userBookmark in a log.

    wall_epoch is the exact tap time: the bookmark's logMonoTime converted
    mono→wall against the segment's first fixed gpsLocationExternal (any fix
    works — both clocks advance together). None when the segment has no fix;
    callers fall back to create_time + offset, which carries the route's
    GPS-fix lag.
    """
    taps = []  # (logMonoTime, route_offset_millis)
    base_mono = None
    gps_ref = None  # (logMonoTime, unix_seconds)

    try:
        for ev in _iter_log(log_path):
            w = ev.which()
            if base_mono is None and w != "initData":
                base_mono = ev.logMonoTime

            if gps_ref is None and w == "gpsLocationExternal":
                gps = ev.gpsLocationExternal
                ms = getattr(gps, "unixTimestampMillis", 0)
                if (gps.flags & 1) and ms > 0:
                    gps_ref = (ev.logMonoTime, ms / 1000.0)

            if w == "userBookmark":
                offset_ms = (ev.logMonoTime - base_mono) / 1e6 if base_mono is not None else 0
                taps.append((ev.logMonoTime, round(segment_num * 60000 + offset_ms)))
    except Exception as e:
        logger.debug("bookmark epoch extraction error for %s: %s", log_path, e)

    result = []
    for mono, offset in taps:
        epoch = gps_ref[1] + (mono - gps_ref[0]) / 1e9 if gps_ref else None
        result.append((offset, epoch))
    return sorted(result)


def _generate_events_json(log_path: str, segment_num: int) -> list:
    """Generate drive events (events.json) from an rlog file."""
    events = []
    base_mono = None
    last_state = None
    last_enabled = None
    last_alert = None

    try:
        for ev in _iter_log(log_path):
            if base_mono is None and ev.which() != "initData":
                base_mono = ev.logMonoTime

            w = ev.which()

            # Bookmark events → user_flag markers on timeline
            if w == "userBookmark":
                if base_mono is not None:
                    offset_ms = (ev.logMonoTime - base_mono) / 1e6
                else:
                    offset_ms = 0
                route_offset_ms = segment_num * 60000 + offset_ms
                events.append({
                    "type": "user_flag",
                    "route_offset_millis": round(route_offset_ms),
                })
                continue

            state = enabled = alert_status = None

            # Support both controlsState (older) and selfdriveState (0.10.x+)
            if w == "selfdriveState":
                sd = ev.selfdriveState
                state = str(sd.state)
                enabled = bool(sd.enabled)
                alert_status = sd.alertStatus.raw if hasattr(sd, "alertStatus") else 0
            elif w == "controlsState":
                cs = ev.controlsState
                if hasattr(cs, "state"):
                    state = str(cs.state)
                    enabled = bool(cs.enabled)
                    alert_status = cs.alertStatus.raw if hasattr(cs, "alertStatus") else 0

            if state is not None and (state != last_state or enabled != last_enabled or alert_status != last_alert):
                if base_mono is not None:
                    offset_ms = (ev.logMonoTime - base_mono) / 1e6
                else:
                    offset_ms = 0
                route_offset_ms = segment_num * 60000 + offset_ms
                events.append({
                    "type": "state",
                    "time": ev.logMonoTime / 1e9,
                    "offset_millis": round(offset_ms),
                    "route_offset_millis": round(route_offset_ms),
                    "data": {
                        "state": state,
                        "enabled": enabled,
                        "alertStatus": alert_status,
                    },
                })
                last_state = state
                last_enabled = enabled
                last_alert = alert_status
    except Exception as e:
        logger.debug("events generation error for %s: %s", log_path, e)

    return events


def extract_signal_catalog(seg_log_pairs: list) -> dict:
    """Scan rlogs and return inventory of all message types with field names.

    Args:
        seg_log_pairs: list of (seg_num, log_path) tuples.

    Returns:
        {msgType: {count: int, fields: [str], freq_hz: float, kind: "numeric"|"snapshot"}}
    """
    catalog = {}  # {msgType: {count, fields, kind}}
    seg_count = len(seg_log_pairs)

    for _seg_num, log_path in seg_log_pairs:
        try:
            for ev in _iter_log(log_path):
                w = ev.which()
                if w not in catalog:
                    # First occurrence — capture field names via to_dict
                    try:
                        msg_obj = getattr(ev, w)
                        d = msg_obj.to_dict(verbose=True) if hasattr(msg_obj, 'to_dict') else {}
                        fields = list(d.keys()) if isinstance(d, dict) else []
                    except Exception:
                        fields = []
                    catalog[w] = {"count": 1, "fields": fields}
                else:
                    catalog[w]["count"] += 1
        except Exception as e:
            logger.debug("signal catalog error for %s: %s", log_path, e)

    # Classify and compute frequency
    duration_sec = max(seg_count * 60, 1)
    result = {}
    for msg_type, info in catalog.items():
        freq_hz = round(info["count"] / duration_sec, 2)
        # Heuristic: <=2 per segment → snapshot, otherwise numeric
        avg_per_seg = info["count"] / max(seg_count, 1)
        kind = "snapshot" if avg_per_seg <= 2 else "numeric"
        result[msg_type] = {
            "count": info["count"],
            "fields": info["fields"],
            "freq_hz": freq_hz,
            "kind": kind,
        }

    return result


def extract_signal_data(seg_log_pairs: list, msg_type: str, max_samples: int = 3000) -> list:
    """Extract all fields for a specific message type from rlogs.

    Args:
        seg_log_pairs: list of (seg_num, log_path) tuples.
        msg_type: cereal message type name (e.g. "carState", "initData").
        max_samples: max samples to return (downsampled if exceeded).

    Returns:
        [{t: float, ...all_fields_from_to_dict...}, ...]
    """
    raw = []

    for seg_num, log_path in seg_log_pairs:
        base_mono = None
        try:
            for ev in _iter_log(log_path):
                if base_mono is None and ev.which() != "initData":
                    base_mono = ev.logMonoTime

                if ev.which() != msg_type:
                    continue

                t = seg_num * 60.0
                if base_mono is not None:
                    t += (ev.logMonoTime - base_mono) / 1e9

                try:
                    msg_obj = getattr(ev, msg_type)
                    d = msg_obj.to_dict(verbose=True) if hasattr(msg_obj, 'to_dict') else {}
                    if not isinstance(d, dict):
                        d = {"value": d}
                except Exception:
                    d = {}

                d["t"] = round(t, 3)
                raw.append(d)
        except Exception as e:
            logger.debug("signal data error for %s/%s: %s", log_path, msg_type, e)

    # Downsample if needed (for numeric types with many samples)
    if len(raw) > max_samples:
        stride = len(raw) / max_samples
        raw = [raw[int(i * stride)] for i in range(max_samples)]

    logger.info("Extracted %d signal samples for %s from %d segments", len(raw), msg_type, len(seg_log_pairs))
    return raw


def _sanitize_for_json(obj):
    """Recursively convert non-JSON-serializable types (bytes, etc.) to strings."""
    if isinstance(obj, bytes):
        # Short bytes as hex, long bytes as truncated
        if len(obj) <= 64:
            return obj.hex()
        return f"<{len(obj)} bytes>"
    if isinstance(obj, dict):
        return {k: _sanitize_for_json(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_sanitize_for_json(v) for v in obj]
    if isinstance(obj, float) and (math.isnan(obj) or math.isinf(obj)):
        return None
    return obj


def extract_all_signals(seg_log_pairs: list, max_samples: int = 3000) -> dict:
    """Single-pass extraction of catalog + data for ALL message types.

    Reads each log file once, collecting to_dict() for every message.
    Returns: {
        catalog: {msgType: {count, fields, freq_hz, kind}},
        data: {msgType: [{t, ...fields...}, ...]}
    }
    """
    catalog_counts = {}   # {msgType: int}
    catalog_fields = {}   # {msgType: [str]}
    all_data = {}         # {msgType: [{t, ...}, ...]}
    seg_count = len(seg_log_pairs)

    for seg_num, log_path in seg_log_pairs:
        base_mono = None
        try:
            for ev in _iter_log(log_path):
                w = ev.which()
                if base_mono is None and w != "initData":
                    base_mono = ev.logMonoTime

                t = seg_num * 60.0
                if base_mono is not None:
                    t += (ev.logMonoTime - base_mono) / 1e9

                # Catalog: count + capture fields on first occurrence
                if w not in catalog_counts:
                    catalog_counts[w] = 1
                    try:
                        msg_obj = getattr(ev, w)
                        d = msg_obj.to_dict(verbose=True) if hasattr(msg_obj, 'to_dict') else {}
                        catalog_fields[w] = list(d.keys()) if isinstance(d, dict) else []
                    except Exception:
                        d = {}
                        catalog_fields[w] = []
                else:
                    catalog_counts[w] += 1
                    try:
                        msg_obj = getattr(ev, w)
                        d = msg_obj.to_dict(verbose=True) if hasattr(msg_obj, 'to_dict') else {}
                    except Exception:
                        d = {}

                if not isinstance(d, dict):
                    d = {"value": d}
                d = _sanitize_for_json(d)
                d["t"] = round(t, 3)

                if w not in all_data:
                    all_data[w] = []
                all_data[w].append(d)
        except Exception as e:
            logger.debug("extract_all_signals error for %s: %s", log_path, e)

    # Build catalog with classification
    duration_sec = max(seg_count * 60, 1)
    catalog = {}
    for msg_type, count in catalog_counts.items():
        freq_hz = round(count / duration_sec, 2)
        avg_per_seg = count / max(seg_count, 1)
        kind = "snapshot" if avg_per_seg <= 2 else "numeric"
        catalog[msg_type] = {
            "count": count,
            "fields": catalog_fields.get(msg_type, []),
            "freq_hz": freq_hz,
            "kind": kind,
        }

    # Downsample numeric types that exceed max_samples
    for msg_type, samples in all_data.items():
        if len(samples) > max_samples:
            stride = len(samples) / max_samples
            all_data[msg_type] = [samples[int(i * stride)] for i in range(max_samples)]

    total_samples = sum(len(v) for v in all_data.values())
    logger.info("Extracted %d total signal samples across %d types from %d segments",
                total_samples, len(catalog), seg_count)

    return {"catalog": catalog, "data": all_data}


def extract_dashboard_telemetry(seg_log_pairs: list) -> list:
    """Extract dashboard telemetry from rlog/qlog files at ~5Hz.

    Args:
        seg_log_pairs: list of (seg_num, log_path) tuples. seg_num is the
            actual segment number used for route-relative time offsets.

    Single-pass read of each log, tracking latest state from carState,
    carControl, selfdriveState, and peripheralState. For rlog (~20Hz carState),
    emits every 4th sample (~5Hz). For qlog (~2Hz carState), keeps every sample.

    Returns list of dicts with all dashboard fields.
    """
    samples = []
    cs_count = 0

    # Latest state accumulators
    steer_cmd = 0.0
    accel_cmd = 0.0
    sd_state = ""
    sd_enabled = False
    voltage = 0.0

    for seg_num, log_path in seg_log_pairs:
        base_mono = None
        # qlog is already sparse (~2Hz) — keep every carState
        # rlog is dense (~20Hz) — downsample to ~5Hz
        is_qlog = "qlog" in log_path.rsplit("/", 1)[-1]
        downsample = 1 if is_qlog else 4
        # Track which accumulator types we've seen this segment.
        # Don't emit samples until all are populated — prevents zeros
        # at segment boundaries when loaded via per-segment API calls.
        seen = set()
        try:
            for ev in _iter_log(log_path):
                if base_mono is None and ev.which() != "initData":
                    base_mono = ev.logMonoTime

                w = ev.which()

                if w == "carControl":
                    cc = ev.carControl
                    act = cc.actuators
                    steer_cmd = float(getattr(act, "torque", getattr(act, "steer", 0)))
                    accel_cmd = float(act.accel)
                    seen.add("cc")

                elif w == "selfdriveState":
                    sd = ev.selfdriveState
                    sd_state = str(sd.state)
                    sd_enabled = bool(sd.enabled)
                    seen.add("sd")

                elif w == "peripheralState":
                    ps = ev.peripheralState
                    voltage = float(ps.voltage) / 1000.0  # millivolts → volts
                    seen.add("ps")

                elif w == "carState":
                    cs_count += 1
                    if cs_count % downsample != 0:
                        continue
                    if base_mono is None:
                        continue
                    # Skip until accumulators are populated (avoids zero-jumps)
                    if len(seen) < 3:
                        continue

                    cs = ev.carState
                    t = seg_num * 60.0 + (ev.logMonoTime - base_mono) / 1e9
                    cruise = cs.cruiseState

                    samples.append({
                        "t": round(t, 3),
                        "coolantTemp": round(float(getattr(cs, "coolantTemp", 0)), 1),
                        "oilTemp": round(float(getattr(cs, "oilTemp", 0)), 1),
                        "vEgo": round(float(cs.vEgo), 3),
                        "steeringAngleDeg": round(float(cs.steeringAngleDeg), 2),
                        "gasPressed": bool(cs.gasPressed),
                        "brakePressed": bool(cs.brakePressed),
                        "cruiseSpeed": round(float(cruise.speed), 2),
                        "cruiseEnabled": bool(cruise.enabled),
                        "steerCmd": round(steer_cmd, 4),
                        "accelCmd": round(accel_cmd, 4),
                        "sdState": sd_state,
                        "sdEnabled": sd_enabled,
                        "voltage": round(voltage, 2),
                    })
        except Exception as e:
            logger.warning("Dashboard telemetry extraction error for %s: %s", log_path, e)

    logger.info("Extracted %d dashboard telemetry samples from %d segments", len(samples), len(seg_log_pairs))
    return samples


def _iter_log_full(log_path: str):
    """Iterate every event in a log file (no head limit).

    _iter_log_head reads a bounded prefix, which is enough for initData/GPS but
    not for per-frame data spanning a whole segment.
    """
    from cereal import log as capnp_log
    import zstandard as zstd

    if log_path.endswith(".zst"):
        with open(log_path, "rb") as f:
            data = zstd.ZstdDecompressor().stream_reader(f).read()
    else:
        with open(log_path, "rb") as f:
            data = f.read()

    return capnp_log.Event.read_multiple_bytes(data)


def extract_frame_times(rlog_path: str) -> list[float] | None:
    """Wall-clock capture time of each encoded fcamera frame in one segment.

    Returns a list indexed by position in the segment's fcamera.hevc — element i
    is the true capture time of video frame i, in epoch seconds. None if the log
    lacks the needed events.

    The video carries no timestamps of its own, so synthesizing them from an
    assumed frame rate drifts away from reality. The log has the exact chain:

      roadEncodeIdx   encoded-frame index -> frameId  (frame order in the .hevc)
      roadCameraState frameId -> timestampSof         (capture time, boot-mono)
      gpsLocationExternal / clocks                    (boot-mono -> wall clock)

    Use rlog, not qlog: qlog decimates roadCameraState to roughly every 20th
    frameId, so most frames' capture times are simply absent there.
    """
    sof_by_frame: dict[int, float] = {}
    encode_order: list[int] = []
    mono_to_wall = None
    clocks_fallback = None

    try:
        for ev in _iter_log_full(rlog_path):
            w = ev.which()
            if w == "roadCameraState":
                cs = ev.roadCameraState
                sof_by_frame[cs.frameId] = cs.timestampSof / 1e9
            elif w == "roadEncodeIdx":
                encode_order.append(ev.roadEncodeIdx.frameId)
            elif w == "gpsLocationExternal" and mono_to_wall is None:
                gps = ev.gpsLocationExternal
                gps_ms = getattr(gps, "unixTimestampMillis", 0)
                if gps_ms > 0 and (gps.flags & 1):
                    mono_to_wall = gps_ms / 1000.0 - ev.logMonoTime / 1e9
            elif w == "clocks" and clocks_fallback is None:
                wall_ns = getattr(ev.clocks, "wallTimeNanos", 0)
                if wall_ns:
                    clocks_fallback = wall_ns / 1e9 - ev.logMonoTime / 1e9
    except Exception as e:
        logger.debug("frame time parse error (%s): %s", rlog_path, e)
        return None

    # GPS is the same clock create_time/gps_time already use; clocks (RTC) is
    # the fallback when a segment has no fix.
    offset = mono_to_wall if mono_to_wall is not None else clocks_fallback
    if offset is None or not encode_order:
        return None

    times = [
        None if sof_by_frame.get(fid) is None else sof_by_frame[fid] + offset
        for fid in encode_order
    ]

    # A segment's first encoded frames are captured just before its own rlog
    # starts (encodeId 0 maps to frameId 2, whose roadCameraState lands in the
    # previous segment), so leading holes are normal. Fill any hole from the
    # neighbouring known frames rather than dropping it.
    known = [i for i, t in enumerate(times) if t is not None]
    if not known:
        return None
    step = ((times[known[-1]] - times[known[0]]) / (known[-1] - known[0])
            if len(known) > 1 else 0.05)
    for i, t in enumerate(times):
        if t is not None:
            continue
        before = next((k for k in reversed(known) if k < i), None)
        after = next((k for k in known if k > i), None)
        if before is not None and after is not None:
            span = times[after] - times[before]
            times[i] = times[before] + span * (i - before) / (after - before)
        elif before is not None:
            times[i] = times[before] + step * (i - before)
        else:
            times[i] = times[after] - step * (after - i)

    return [round(t, 3) for t in times]
