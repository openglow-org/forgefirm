"""camera.* - the lid camera pipeline through forgectrl."""
import json

from ..catalog import test
from .. import hw
from ..baseline import LID_LAMP_ATTR

_CAM_COVERS = [("forgectrl", "src/cam.*"), ("forgectrl", "src/camhealth.*"),
               ("forgectrl", "src/debayer.*"), ("forgectrl", "src/vpu_jpeg.*"),
               ("forgectrl", "src/vpu_h264.*"), ("forgectrl", "src/mp4mux.*"),
               ("forgectrl", "src/gpu_debayer.*"), ("forgectrl", "src/ipu_copy.*"),
               ("forgectrl", "src/main.c"),
               ("python3-gfhardware", "gfhardware/src/**"), ("python3-gfhardware", "gfhardware/cam*")]

# The privacy gate spans the camera path and the lid read it depends on,
# in both processes that can reach a sensor.
_PRIVACY_COVERS = _CAM_COVERS + [("forgectrl", "src/status.*"),
                                 ("python3-gfhardware", "gfhardware/switches.py"),
                                 ("forgefirm-app", "forgefirm-app/ffmachine.py")]

# The sensors the machine ships with, and the geometry each one implies.
# A machine reports exactly one of these; anything else means the sensor
# bound to a driver the capture path has no profile for.
_SENSOR_GEOMETRY = {
    "OV5648": (2592, 1944),
    "OV8856": (3264, 2448),
}


def _jpeg_size(data):
    """(width, height) from a JPEG's first SOFn marker, or None."""
    i = 2
    n = len(data)
    while i + 3 < n:
        if data[i] != 0xFF:
            i += 1
            continue
        marker = data[i + 1]
        if marker in (0xD8, 0xD9) or 0xD0 <= marker <= 0xD7:
            i += 2
            continue
        seglen = (data[i + 2] << 8) | data[i + 3]
        # SOFn, excluding the non-frame markers in the same range
        if 0xC0 <= marker <= 0xCF and marker not in (0xC4, 0xC8, 0xCC):
            if i + 9 > n:
                return None
            return ((data[i + 7] << 8) | data[i + 8],
                    (data[i + 5] << 8) | data[i + 6])
        i += 2 + seglen
    return None


@test("camera.snapshot", title="Lid camera snapshot and stream", subsystem="camera",
      kind="auto", est_min=2,
      covers=_CAM_COVERS, requires=["forgectrl.panel-serves"],
      steps=["Setup: lid closed."],
      description="/cam/snapshot returns a JPEG of a plausible size (a black frame compresses "
                  "far smaller), a second snapshot with the lid lamp switched off differs from it "
                  "and is smaller (the capture is live, not a frame the pipeline is re-serving, "
                  "and the lamp lights what the camera sees), and the MJPEG stream starts and "
                  "stops.")
def snapshot(ctx):
    fc = ctx.forgectrl
    ev = ctx.evidence
    st, body = fc.get("/cam/status")
    ctx.check(st == 200 and isinstance(body, dict), "GET /cam/status -> %s", st)
    ev["cam_status"] = body
    ctx.log("cam status: %s", body)

    st, data = fc.get("/cam/snapshot", params={"cam": "lid", "res": "half"}, raw=True)
    ctx.log("GET /cam/snapshot?cam=lid&res=half -> %s (%d bytes)", st, len(data) if data else 0)
    ctx.check(st == 200, "snapshot -> %s %s", st, data[:120] if data else "")
    ctx.check(data[:2] == b"\xff\xd8" and data[-2:] == b"\xff\xd9", "snapshot is not a complete JPEG")
    ev["snapshot_bytes"] = len(data)
    ctx.check(len(data) > 20000, "snapshot is only %d bytes - a dark or empty frame?", len(data))

    # Live, and lit: the same frame with the lid lamp off must be a
    # different and smaller picture. The lamp is put back where it was.
    lamp0 = hw.sysfs_read(LID_LAMP_ATTR)
    ev["lid_lamp"] = lamp0
    ctx.check(lamp0 is not None, "the lid lamp (%s) is not readable", LID_LAMP_ATTR)
    try:
        hw.sysfs_write(LID_LAMP_ATTR, "0")
        ctx.sleep(1.5)
        st, dark = fc.get("/cam/snapshot", params={"cam": "lid", "res": "half"}, raw=True)
    finally:
        hw.sysfs_write(LID_LAMP_ATTR, lamp0)
    ctx.check(st == 200 and dark and dark[:2] == b"\xff\xd8", "snapshot with the lamp off -> %s", st)
    ev["snapshot_dark_bytes"] = len(dark)
    ctx.log("lamp %s -> %d bytes; lamp off -> %d bytes", lamp0, len(data), len(dark))
    ctx.check(dark != data, "the snapshot with the lamp off is byte-identical to the lit one: the "
                            "capture is not live")
    ctx.check(len(dark) < len(data), "the frame with the lamp off is not smaller than the lit one "
                                     "(%d vs %d bytes): the lamp is not lighting what the camera "
                                     "sees", len(dark), len(data))

    st, data = fc.get("/cam/snapshot", params={"cam": "lid", "res": "full"}, raw=True)
    ctx.log("GET /cam/snapshot?cam=lid&res=full -> %s (%d bytes)", st, len(data) if data else 0)
    ctx.check(st == 200 and data[:2] == b"\xff\xd8", "full-resolution snapshot -> %s", st)
    ev["snapshot_full_bytes"] = len(data)

    # the stream: fetch a little of it and let it close
    import urllib.request
    req = urllib.request.Request(fc.base + "/cam/stream", headers={"Host": fc.host_header()})
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            ctype = r.headers.get("Content-Type", "")
            chunk = r.read(65536)
    except Exception as e:  # noqa: BLE001
        ctx.fail("stream did not open: %s", e)
    ev["stream_ctype"] = ctype
    ctx.log("stream: %s, first %d bytes", ctype, len(chunk))
    ctx.check("multipart" in ctype and b"\xff\xd8" in chunk, "stream is not an MJPEG multipart")
    ctx.sleep(2)
    st, body = fc.get("/cam/status")
    ev["cam_status_after"] = body
    ctx.log("cam status after: %s", body)
    ctx.log("PASS: half-res snapshot %d bytes, %d with the lamp off; full-res %d; stream %s",
            ev["snapshot_bytes"], len(dark), len(data), ctype)


@test("camera.sensor-profile", title="Camera geometry follows the fitted sensor", subsystem="camera",
      kind="auto", est_min=1,
      covers=_CAM_COVERS, requires=["forgectrl.panel-serves"],
      description="/cam/status names the sensor that bound (OV5648 on a 5 MP machine, OV8856 on an "
                  "8 MP 'HD' one) and reports the geometry that sensor implies, and the JPEGs it "
                  "actually returns are that size. A machine that reports 'unknown', or whose frames "
                  "do not match what it advertises, is running the wrong capture profile.")
def sensor_profile(ctx):
    fc = ctx.forgectrl
    ev = ctx.evidence

    st, body = fc.get("/cam/status")
    ctx.check(st == 200 and isinstance(body, dict), "GET /cam/status -> %s", st)
    sensor = body.get("sensor")
    ev["sensor"] = sensor
    ctx.log("sensor: %s", sensor)
    ctx.check(sensor in _SENSOR_GEOMETRY,
              "sensor %r is not one this build has a capture profile for", sensor)

    want_w, want_h = _SENSOR_GEOMETRY[sensor]
    snap = body.get("snapshot") or {}
    stream = body.get("stream") or {}
    ev["geometry"] = {"snapshot": snap, "stream": stream}
    ctx.log("advertised: snapshot %sx%s, stream %sx%s",
            snap.get("width"), snap.get("height"), stream.get("width"), stream.get("height"))
    ctx.check(snap.get("width") == want_w and snap.get("height") == want_h,
              "%s should advertise %dx%d snapshots, got %sx%s",
              sensor, want_w, want_h, snap.get("width"), snap.get("height"))
    ctx.check(stream.get("width") == want_w // 2 and stream.get("height") == want_h // 2,
              "%s should advertise %dx%d stream frames, got %sx%s",
              sensor, want_w // 2, want_h // 2, stream.get("width"), stream.get("height"))

    # What it advertises is what it delivers.
    for res, (w, h) in (("full", (want_w, want_h)), ("half", (want_w // 2, want_h // 2))):
        st, data = fc.get("/cam/snapshot", params={"cam": "lid", "res": res}, raw=True)
        ctx.check(st == 200 and data and data[:2] == b"\xff\xd8",
                  "%s snapshot -> %s", res, st)
        got = _jpeg_size(data)
        ev["jpeg_%s" % res] = got
        ctx.log("%s snapshot: %s (%d bytes)", res, got, len(data))
        ctx.check(got == (w, h), "%s snapshot is %s, not %dx%d", res, got, w, h)


@test("camera.h264-stream", title="H.264 live stream", subsystem="camera",
      kind="auto", est_min=2,
      covers=_CAM_COVERS, requires=["forgectrl.panel-serves"],
      steps=["Setup: lid closed."],
      description="/cam/h264 answers a fragmented MP4: a codec header naming the SPS profile, an "
                  "init segment (ftyp+moov) followed by media fragments (moof+mdat) within a few "
                  "seconds, and /cam/status reporting the H.264 encoder active with this client "
                  "counted. Also records which demosaic path (GPU or CPU) served the stream. On a "
                  "machine whose image lacks Mesa or whose encoder refused, the endpoint must "
                  "answer 503 rather than hang - that is a pass for the endpoint but is recorded "
                  "in the evidence.")
def h264_stream(ctx):
    import urllib.error
    import urllib.request

    fc = ctx.forgectrl
    ev = ctx.evidence

    req = urllib.request.Request(fc.base + "/cam/h264?cam=lid",
                                 headers={"Host": fc.host_header()})
    try:
        r = urllib.request.urlopen(req, timeout=20)
    except urllib.error.HTTPError as e:
        ev["status"] = e.code
        ev["body"] = e.read()[:200].decode("utf-8", "replace")
        ctx.check(e.code == 503, "H.264 refused with %s, not 503: %s", e.code, ev["body"])
        ctx.log("H.264 unavailable on this machine (503): %s", ev["body"])
        return
    with r:
        codec = r.headers.get("X-H264-Codec", "")
        ctype = r.headers.get("Content-Type", "")
        ev["codec"] = codec
        ev["content_type"] = ctype
        ctx.log("codec %s, content-type %s", codec, ctype)
        ctx.check(ctype == "video/mp4", "content type is %r", ctype)
        ctx.check(codec.startswith("avc1."), "codec header is %r", codec)

        # Init segment, then at least one media fragment.
        data = b""
        while len(data) < 512 * 1024:
            chunk = r.read(65536)
            if not chunk:
                break
            data += chunk
            if b"moof" in data and b"mdat" in data:
                break
    ev["bytes"] = len(data)
    ctx.check(data[4:8] == b"ftyp", "stream does not start with ftyp")
    ctx.check(b"moov" in data, "no moov (init segment)")
    ctx.check(b"avcC" in data, "no avcC in the init segment")
    ctx.check(b"moof" in data and b"mdat" in data,
              "no media fragment arrived in %d bytes", len(data))

    st, body = fc.get("/cam/status")
    ctx.check(st == 200 and isinstance(body, dict), "GET /cam/status -> %s", st)
    ev["cam_status"] = body
    ctx.log("convert=%s h264=%s", body.get("convert"), body.get("h264"))
    h = body.get("h264") or {}
    ctx.check(h.get("active") is True, "/cam/status does not report the H.264 encoder active: %s",
              body)


@test("camera.frame-health", title="Capture delivers whole frames", subsystem="camera",
      kind="auto", est_min=1,
      covers=_CAM_COVERS, requires=["forgectrl.panel-serves"],
      description="The capture queue flags a frame errored when it is short, torn, or arrived "
                  "after the CSI-2 receiver lost sync; forgectrl drops those rather than "
                  "demosaicing them, and restarts the stream if they persist. A healthy machine "
                  "captures a burst with none of them. A nonzero corrupt count here is a real "
                  "signal - a marginal camera ribbon, a mistimed D-PHY - even though the frames "
                  "themselves never reach a client.")
def frame_health(ctx):
    fc = ctx.forgectrl
    ev = ctx.evidence

    def health():
        st, body = fc.get("/cam/status")
        ctx.check(st == 200 and isinstance(body, dict), "GET /cam/status -> %s", st)
        h = body.get("health")
        ctx.check(isinstance(h, dict), "/cam/status carries no health block: %s", body)
        return h

    before = health()
    ev["health_before"] = before
    ctx.log("before: %s", before)

    # A burst, so the count covers a run of frames rather than a single grab.
    for _ in range(3):
        st, data = fc.get("/cam/snapshot", params={"cam": "lid", "res": "half"}, raw=True)
        ctx.check(st == 200 and data and data[:2] == b"\xff\xd8", "snapshot -> %s", st)

    after = health()
    ev["health_after"] = after
    ctx.log("after: %s", after)

    captured = (after.get("captured") or 0) - (before.get("captured") or 0)
    corrupt = (after.get("corrupt") or 0) - (before.get("corrupt") or 0)
    restarts = (after.get("restarts") or 0) - (before.get("restarts") or 0)
    ev["captured"] = captured
    ev["corrupt"] = corrupt
    ev["restarts"] = restarts
    ctx.log("captured %d frames, %d corrupt, %d stream restarts", captured, corrupt, restarts)

    ctx.check(captured > 0, "no frames were dequeued during three snapshots")
    ctx.check(corrupt == 0, "%d of %d captured frames came back errored", corrupt, captured)
    ctx.check(restarts == 0, "the capture stream was restarted %d time(s)", restarts)


@test("camera.lid-privacy", title="Cameras capture only with the lid closed", subsystem="camera",
      kind="operator", est_min=3,
      covers=_PRIVACY_COVERS, requires=["forgectrl.panel-serves"], actions=["lid"],
      steps=["Start with the lid closed; you will be told to open it, then close it again.",
             "Nothing moves and the laser is not involved."],
      description="The privacy gate: with the lid open neither camera captures. A running stream "
                  "stops within a frame or so of the lid opening, /cam/status reports capture as "
                  "not allowed, and both the snapshot and the stream are refused with 409 and a "
                  "reason naming the lid. Closing the lid restores all of it. This is what stops "
                  "the machine - and in cloud mode the Glowforge service - from imaging the room "
                  "through an open lid.")
def lid_privacy(ctx):
    import urllib.error
    import urllib.request

    fc = ctx.forgectrl
    ev = ctx.evidence

    def status():
        st, body = fc.get("/cam/status")
        ctx.check(st == 200 and isinstance(body, dict), "GET /cam/status -> %s", st)
        return body

    def snapshot():
        """(status, body) for a half-res lid snapshot."""
        st, data = fc.get("/cam/snapshot", params={"cam": "lid", "res": "half"}, raw=True)
        return st, data or b""

    # --- lid closed: the baseline the rest is measured against ----------
    ctx.act("lid", "close")
    body = status()
    ctx.check(body.get("capture_allowed") is True,
              "with the lid closed /cam/status should allow capture, got %r",
              body.get("capture_allowed"))
    st, data = snapshot()
    ctx.check(st == 200 and data[:2] == b"\xff\xd8",
              "snapshot with the lid closed -> %s", st)
    ev["closed_snapshot_bytes"] = len(data)
    ctx.log("lid closed: snapshot %d bytes", len(data))

    # --- a live stream must die when the lid opens ----------------------
    req = urllib.request.Request(fc.base + "/cam/stream?cam=lid",
                                 headers={"Host": fc.host_header()})
    stream = urllib.request.urlopen(req, timeout=15)
    try:
        first = stream.read(4096)
        ctx.check(b"\xff\xd8" in first, "the stream did not start before the lid test")
        ctx.act("lid", "open", text="The stream is running.")
        # The engine tears the pipeline down on the next frame and the
        # stream ends. Draining now returns whatever was buffered before
        # the lid opened and then EOF; what must not happen is frames
        # continuing to arrive indefinitely.
        drained, ended = 0, False
        try:
            while True:
                chunk = stream.read(65536)
                if not chunk:
                    ended = True
                    break
                drained += len(chunk)
                ctx.check(drained < 8 * 1024 * 1024,
                          "the stream was still delivering after %d bytes with the lid open",
                          drained)
        except Exception as e:  # noqa: BLE001 - a reset connection ends it too
            ended = True
            ctx.log("stream ended with %s", type(e).__name__)
        ev["stream_bytes_after_lid_open"] = drained
        ctx.log("stream ended after %d further buffered bytes", drained)
        ctx.check(ended, "the stream never ended after the lid opened")
    finally:
        stream.close()

    # --- lid open: everything is refused, and says why ------------------
    body = status()
    ev["status_lid_open"] = body
    ctx.check(body.get("capture_allowed") is False,
              "with the lid open /cam/status should refuse capture, got %r",
              body.get("capture_allowed"))
    ctx.check(body.get("stopped_by_lid") is True,
              "the engine should record that the lid stopped it, got %r",
              body.get("stopped_by_lid"))
    ctx.check(body.get("running") is False,
              "the capture engine should not still be running with the lid open")

    st, data = snapshot()
    ev["snapshot_lid_open"] = [st, data[:120].decode("utf-8", "replace")]
    ctx.log("lid open: snapshot -> %s %s", st, data[:120])
    ctx.check(st == 409, "snapshot with the lid open should be refused with 409, got %s", st)
    ctx.check(b"lid" in data.lower(), "the refusal should name the lid: %r", data[:120])
    ctx.check(data[:2] != b"\xff\xd8", "a JPEG was returned with the lid open")

    st, data = fc.get("/cam/snapshot", params={"cam": "head", "res": "half"}, raw=True)
    ctx.log("lid open: head snapshot -> %s", st)
    ctx.check(st == 409, "the head camera should be refused too, got %s", st)

    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            ctx.fail("the stream opened with the lid open (%s)", r.status)
    except urllib.error.HTTPError as e:
        ctx.log("lid open: stream -> %s", e.code)
        ctx.check(e.code == 409, "the stream should be refused with 409, got %s", e.code)

    # --- closing the lid restores it -----------------------------------
    ctx.act("lid", "close")
    body = status()
    ctx.check(body.get("capture_allowed") is True,
              "closing the lid should allow capture again, got %r",
              body.get("capture_allowed"))
    st, data = snapshot()
    ctx.check(st == 200 and data[:2] == b"\xff\xd8",
              "snapshot after closing the lid -> %s", st)
    ctx.log("lid closed again: snapshot %d bytes", len(data))


@test("camera.key-read", title="The camera key reads without a login", subsystem="camera",
      kind="auto", est_min=1,
      covers=[("forgectrl", "src/camkey.*"), ("forgectrl", "src/auth.c"),
              ("forgectrl", "src/main.c"), ("forgectrl", "src/ui/panel.js"),
              ("forgectrl", "src/ui/index.html"), ("forgectrl", "src/ui/help.js")],
      requires=["forgectrl.auth"],
      description="With the read routes closed to the network (panel_open_reads=0), a camera "
                  "status read from the board's LAN address is refused without the key and "
                  "served with it, on HTTP and on HTTPS; a wrong key is refused; the key never "
                  "authorizes a write; a rotation from the panel route stops the old key and "
                  "the new one reads. The setting is put back after.")
def key_read(ctx):
    from .commission import request
    from .forgectrl import lan_ip
    fc = ctx.forgectrl
    ev = ctx.evidence
    ip = lan_ip()
    ctx.check(ip, "cannot determine the board's LAN address")
    http_base = "http://%s" % ip
    tls_base = "https://%s" % ip
    token = {"X-ForgeFIRM-Token": fc.token}

    st, body, _ = request(fc.base, "GET", "/system/camera-key", headers=token)
    ctx.check(st == 200, "GET /system/camera-key from loopback -> %s", st)
    key = json.loads(body)["key"]
    ctx.check(len(key) == 32, "the key is not 32 characters: %r", key)
    ev["key_len"] = len(key)

    before = fc.settings().get("panel_open_reads", "")
    st, _ = fc.post("/settings", params={"panel_open_reads": "0"})
    ctx.check(st == 200, "closing the reads -> %s", st)
    try:
        for base in (http_base, tls_base):
            st, _, _ = request(base, "GET", "/cam/status")
            ctx.log("GET %s/cam/status (no key) -> %s", base, st)
            ctx.check(st == 403, "a closed read without the key -> %s, expected 403", st)
            st, _, _ = request(base, "GET", "/cam/status?key=" + key)
            ctx.log("GET %s/cam/status?key=... -> %s", base, st)
            ctx.check(st == 200, "a read with the key -> %s, expected 200", st)
            st, _, _ = request(base, "GET", "/cam/status", headers={"X-ForgeFIRM-Camera-Key": key})
            ctx.check(st == 200, "a read with the key header -> %s, expected 200", st)
            wrong = ("0" if key[0] != "0" else "1") + key[1:]
            st, _, _ = request(base, "GET", "/cam/status?key=" + wrong)
            ctx.check(st == 403, "a wrong key -> %s, expected 403", st)
        # the key is a read credential only
        st, _, _ = request(tls_base, "POST", "/settings", data={"panel_open_reads": "0"},
                           headers={"X-ForgeFIRM-Camera-Key": key})
        ctx.log("POST /settings with the camera key -> %s", st)
        ctx.check(st == 403, "the camera key authorized a write (%s)", st)
        # rotation
        st, body, _ = request(fc.base, "POST", "/system/camera-key?rotate=1", headers=token)
        ctx.check(st == 200, "rotate -> %s", st)
        new = json.loads(body)["key"]
        ctx.check(new != key, "rotation kept the same key")
        st, _, _ = request(http_base, "GET", "/cam/status?key=" + key)
        ctx.check(st == 403, "the old key still reads after rotation (%s)", st)
        st, _, _ = request(http_base, "GET", "/cam/status?key=" + new)
        ctx.check(st == 200, "the new key does not read (%s)", st)
        ev["rotated"] = True
    finally:
        fc.post("/settings", params={"panel_open_reads": before})
