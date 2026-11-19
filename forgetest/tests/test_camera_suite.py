# Copyright 2026 514 LLC d/b/a OpenGlow
# Written by Scott Wiederhold
# https://community.openglow.org
# SPDX-License-Identifier:    MIT

"""camera.lid-privacy's judgment of a running stream after the lid opens,
over a scripted stream: the frames that come after the lid is seen open are
measured against the size of the frames before it, and a stream that keeps
delivering fails, whatever the transport does. camera.h264-stream fails on
an encoder that answers 503: the image ships one."""
import threading
import time
import unittest

import helpers
from forgetest.runner import Context, Failed, Run
from forgetest.suite import camera

FRAME = b"--frame\r\nContent-Type: image/jpeg\r\n\r\n" + camera.JPEG_SOI + b"\x00" * 2000 + b"\xff\xd9\r\n"


class ScriptedStream:
    """An MJPEG stream: a frame every `period` seconds until stop_at (time.time)
    or close(), then EOF."""

    def __init__(self, period=0.01, stop_at=None, quiet_at=None):
        self.period = period
        self.stop_at = stop_at
        self.quiet_at = quiet_at        # from then on nothing arrives, and the stream does not end
        self.closed = threading.Event()

    def read1(self, n):
        if self.closed.wait(self.period) or (self.stop_at is not None and time.time() >= self.stop_at):
            return b""
        if self.quiet_at is not None and time.time() >= self.quiet_at:
            self.closed.wait()
            return b""
        return FRAME

    def close(self):
        self.closed.set()


class StreamWatchTests(unittest.TestCase):
    def watch(self, stream):
        w = camera.StreamWatch(stream)
        self.addCleanup(w.stop)
        self.assertTrue(w.wait_frames(5, 2))
        return w

    def test_a_stream_that_keeps_delivering_after_the_lid_fails(self):
        v = self.watch(ScriptedStream()).after_edge(time.time(), end_s=0.3)
        self.assertGreater(v["frames"], camera.STREAM_FRAMES_AFTER_EDGE)       # counted after the edge
        self.assertIsNotNone(camera.lid_stream_failure(v))

    def test_a_stream_that_falls_silent_without_ending_fails(self):
        stream = ScriptedStream()
        w = self.watch(stream)
        edge = time.time()
        stream.quiet_at = edge
        v = w.after_edge(edge, end_s=0.3)
        self.assertFalse(v["ended"])
        self.assertIsNotNone(camera.lid_stream_failure(v))

    def test_an_ended_stream_is_held_to_a_frame_or_so_after_the_edge(self):
        stream = ScriptedStream()
        w = self.watch(stream)
        edge = time.time()
        stream.stop_at = edge + 0.015              # a frame or so after the edge, then the end
        v = w.after_edge(edge, end_s=1.0)
        self.assertIsNone(camera.lid_stream_failure(v), v)
        # ended, but with more than the bound after the edge
        self.assertIsNotNone(camera.lid_stream_failure(
            dict(v, bytes=(camera.STREAM_FRAMES_AFTER_EDGE + 1) * v["frame_bytes"])))


class H264Tests(unittest.TestCase):
    def test_an_encoder_that_answers_503_fails(self):
        fake = helpers.FakeForgectrl().start()
        self.addCleanup(fake.stop)
        fake.on_get = lambda path, q: (503, {"error": "no H.264 encoder"}) if path == "/cam/h264" else None
        t = helpers.make_test("camera.h264-stream", [])
        run = Run("test", t.id, t.title)
        with self.assertRaises(Failed):
            camera.h264_stream(Context(run, None, t))
        self.assertEqual(run.evidence["status"], 503)


if __name__ == "__main__":
    unittest.main()
