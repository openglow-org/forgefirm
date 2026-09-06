"""Hardware and service access for the suite: forgectrl's HTTP API, the
kernel module's sysfs, the init scripts, and the Grbl TCP port.

Everything is reachable through environment overrides so the suite can be
exercised against a mock on a host:
  FORGECTRL_URL          default http://127.0.0.1
  FORGECTRL_TOKEN_FILE   default /data/forgefirm/panel.token
  GF_SYSFS_ROOT          default /sys/glowforge/  (must end with '/')
  GRBL_HOST / GRBL_PORT  default 127.0.0.1 / 23
  FORGETEST_INITD        default /etc/init.d
"""
import json
import os
import socket
import subprocess
import threading
import time
import urllib.error
import urllib.parse
import urllib.request


class HwError(Exception):
    pass


# ------------------------------------------------------------ forgectrl

# The supervisor's levers answer only when the switch is done: a mode
# switch waits for the old controller to exit, the new one to start (a
# pending liveness probe first) and its first job-state report (up to
# 15 s; the emulator's comes with its start, a real client's with its
# machine); a stop waits for the child to go. They get their own timeout,
# above the daemon's own deadlines, so a slow but honest switch is never
# read as a dead daemon.
SLOW_PATHS = ("/mode", "/controller/start", "/controller/stop")
SLOW_TIMEOUT_S = 120.0


class Forgectrl:
    """Thin client for the machine-services daemon."""

    def __init__(self, base=None, token=None, timeout=10.0, slow_timeout=SLOW_TIMEOUT_S):
        self.base = (base or os.environ.get("FORGECTRL_URL") or "http://127.0.0.1").rstrip("/")
        self.timeout = timeout
        self.slow_timeout = slow_timeout
        self._token = token

    def timeout_for(self, method, path):
        return self.slow_timeout if method == "POST" and path in SLOW_PATHS else self.timeout

    @property
    def token(self):
        if self._token is None:
            path = os.environ.get("FORGECTRL_TOKEN_FILE") or "/data/forgefirm/panel.token"
            try:
                with open(path, "r", encoding="utf-8") as f:
                    self._token = f.read().strip()
            except OSError:
                self._token = ""
        return self._token

    def host_header(self):
        return urllib.parse.urlsplit(self.base).netloc

    def request(self, method, path, params=None, data=None, headers=None, auth=True, raw=False):
        """Returns (status, body). body is parsed JSON when the response
        is JSON, else text (or bytes when raw=True). Never raises on an
        HTTP error status - the suite asserts on codes."""
        url = self.base + path
        if params:
            url += ("&" if "?" in url else "?") + urllib.parse.urlencode(params)
        body = None
        hdrs = {"Host": self.host_header()}
        if headers:
            hdrs.update(headers)
        if data is not None:
            if isinstance(data, (dict, list)):
                body = urllib.parse.urlencode(data).encode()
                hdrs.setdefault("Content-Type", "application/x-www-form-urlencoded")
            elif isinstance(data, str):
                body = data.encode()
            else:
                body = data
        if auth and self.token:
            hdrs.setdefault("X-ForgeFIRM-Token", self.token)
        req = urllib.request.Request(url, data=body, method=method, headers=hdrs)
        try:
            with urllib.request.urlopen(req, timeout=self.timeout_for(method, path)) as resp:
                status = resp.status
                content = resp.read()
                ctype = resp.headers.get("Content-Type", "")
        except urllib.error.HTTPError as e:
            status = e.code
            content = e.read()
            ctype = e.headers.get("Content-Type", "") if e.headers else ""
        except (urllib.error.URLError, socket.timeout, OSError) as e:
            raise HwError("forgectrl %s %s: %s" % (method, path, e))
        if raw:
            return status, content
        text = content.decode("utf-8", "replace")
        if "json" in ctype:
            try:
                return status, json.loads(text)
            except ValueError:
                pass
        return status, text

    def get(self, path, **kw):
        return self.request("GET", path, **kw)

    def post(self, path, **kw):
        return self.request("POST", path, **kw)

    def status(self):
        st, body = self.get("/status")
        if st != 200 or not isinstance(body, dict):
            raise HwError("forgectrl /status -> %s" % st)
        return body

    def settings(self):
        st, body = self.get("/settings")
        if st != 200 or not isinstance(body, dict):
            raise HwError("forgectrl /settings -> %s" % st)
        return body

    def wait_idle(self, timeout=60.0, poll=0.5, abort=None):
        deadline = time.time() + timeout
        while time.time() < deadline:
            if abort and abort():
                raise HwError("aborted while waiting for idle")
            try:
                if self.status().get("state") == "idle":
                    return True
            except HwError:
                pass
            time.sleep(poll)
        return False


# ---------------------------------------------------------------- sysfs

def sysfs_root():
    r = os.environ.get("GF_SYSFS_ROOT") or "/sys/glowforge/"
    return r if r.endswith("/") else r + "/"


def sysfs_read(attr, default=None):
    try:
        with open(sysfs_root() + attr, "r") as f:
            return f.read().strip()
    except OSError:
        return default


def sysfs_int(attr, default=None):
    v = sysfs_read(attr)
    if v is None or v == "":
        return default
    try:
        return int(v.split()[0], 0)
    except ValueError:
        return default


def sysfs_write(attr, value):
    with open(sysfs_root() + attr, "w") as f:
        f.write(str(value))


# ------------------------------------------------------ head accelerometer

# The head accelerometer (LIS2HH12 on i2c-3 at 0x1e): the supervisor's
# motion-liveness witness, and the catalog's. Resolved by bus address,
# never by iio index. The raw sysfs read is slow (~150 ms), which still
# lands several samples in a one-second move; the verdict is peak-to-peak
# on X or Y, against the thresholds forgectrl's probe established (a live
# head reads 1800-2900 on its probe move, a wedged one <= 250).
HEAD_ACCEL_I2C = "3-001e"
ACCEL_P2P_MOVING = 800


def head_accel_dir():
    """The head accel's iio directory, or None. GF_IIO_ROOT overrides the
    /sys/bus/iio/devices root (host tests)."""
    root = os.environ.get("GF_IIO_ROOT") or "/sys/bus/iio/devices"
    try:
        names = sorted(os.listdir(root))
    except OSError:
        return None
    for n in names:
        d = os.path.join(root, n)
        try:
            target = os.readlink(d)
        except OSError:
            target = ""
        if HEAD_ACCEL_I2C in target or HEAD_ACCEL_I2C in n:
            return d
        # a plain directory (host fixture): its name carries the address
        try:
            with open(os.path.join(d, "name")) as f:
                if HEAD_ACCEL_I2C in f.read():
                    return d
        except OSError:
            pass
    return None


def head_accel_read(d):
    """(x, y) raw counts, or None."""
    out = []
    for axis in ("x", "y"):
        try:
            with open(os.path.join(d, "in_accel_%s_raw" % axis)) as f:
                out.append(int(f.read().strip()))
        except (OSError, ValueError):
            return None
    return tuple(out)


class AccelSampler:
    """Samples the head accelerometer in a thread for as long as it is
    running; `p2p(t0, t1)` is the peak-to-peak on X and Y over the samples
    in that window and how many there were. Used as a context manager
    around a motion the test wants the head to have made."""

    def __init__(self, period=0.0):
        self.dir = head_accel_dir()
        self.period = period
        self.samples = []            # (t, x, y)
        self._stop = threading.Event()
        self._th = None
        self.errors = 0

    @property
    def available(self):
        return self.dir is not None

    def __enter__(self):
        if self.dir is not None:
            self._th = threading.Thread(target=self._loop, daemon=True, name="forgetest-accel")
            self._th.start()
        return self

    def __exit__(self, *a):
        self._stop.set()
        if self._th is not None:
            self._th.join(2.0)
        return False

    def _loop(self):
        while not self._stop.is_set():
            v = head_accel_read(self.dir)
            if v is None:
                self.errors += 1
            else:
                self.samples.append((time.time(), v[0], v[1]))
            if self.period:
                self._stop.wait(self.period)

    def p2p(self, t0, t1=None):
        """(p2p_x, p2p_y, n) over [t0, t1]; (0, 0, 0) with no samples."""
        t1 = time.time() if t1 is None else t1
        xs = [x for t, x, _y in self.samples if t0 <= t <= t1]
        ys = [y for t, _x, y in self.samples if t0 <= t <= t1]
        if not xs:
            return 0, 0, 0
        return max(xs) - min(xs), max(ys) - min(ys), len(xs)


# ----------------------------------------------------------- button LEDs

BUTTON_LEDS = ("button_led_1", "button_led_2", "button_led_3")


def leds_root():
    r = os.environ.get("GF_LEDS_ROOT") or "/sys/class/leds/"
    return r if r.endswith("/") else r + "/"


def button_leds():
    """The three button LEDs' commanded levels (the smooth trigger's
    `target`, which the controller writes; `brightness` follows it with
    a fade and is read where no target exists), or None where unreadable."""
    out = []
    for name in BUTTON_LEDS:
        val = None
        for attr in ("target", "brightness"):
            try:
                with open(leds_root() + name + "/" + attr) as f:
                    val = int(f.read().strip())
                break
            except (OSError, ValueError):
                continue
        out.append(val)
    return out


def button_lit():
    """True when any button LED is on, False when all three read 0, None
    when none is readable."""
    vals = [v for v in button_leds() if v is not None]
    if not vals:
        return None
    return any(v > 0 for v in vals)


# --------------------------------------------------------------- init.d

def initd(service, action, timeout=60):
    """Run /etc/init.d/<service> <action>; returns (rc, output)."""
    base = os.environ.get("FORGETEST_INITD") or "/etc/init.d"
    script = os.path.join(base, service)
    try:
        p = subprocess.run([script, action], stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                           timeout=timeout)
    except (OSError, subprocess.TimeoutExpired) as e:
        return 127, str(e)
    return p.returncode, p.stdout.decode("utf-8", "replace")


def pidof(comm):
    """PIDs whose /proc/<pid>/comm equals comm (15-char kernel limit applies)."""
    out = []
    try:
        for pid in os.listdir("/proc"):
            if not pid.isdigit():
                continue
            try:
                with open("/proc/%s/comm" % pid) as f:
                    if f.read().strip() == comm:
                        out.append(int(pid))
            except OSError:
                pass
    except OSError:
        pass
    return out


def pgrep_f(needle):
    """PIDs whose /proc/<pid>/cmdline contains needle (a script run by an
    interpreter has the interpreter's comm, so pidof cannot see it)."""
    out = []
    try:
        for pid in os.listdir("/proc"):
            if not pid.isdigit():
                continue
            try:
                with open("/proc/%s/cmdline" % pid, "rb") as f:
                    if needle.encode() in f.read():
                        out.append(int(pid))
            except OSError:
                pass
    except OSError:
        pass
    return out


def run(cmd, timeout=60):
    """Run a command list; returns (rc, combined output)."""
    try:
        p = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=timeout)
    except (OSError, subprocess.TimeoutExpired) as e:
        return 127, str(e)
    return p.returncode, p.stdout.decode("utf-8", "replace")


# ------------------------------------------------------------------ grbl

def grbl_port_open(timeout=5):
    """True when the Grbl port accepts a connection (closed again at once)."""
    try:
        s = socket.create_connection((os.environ.get("GRBL_HOST") or "127.0.0.1",
                                      int(os.environ.get("GRBL_PORT") or 23)), timeout=timeout)
        s.close()
        return True
    except OSError:
        return False


class Grbl:
    """Minimal Grbl-over-TCP client. The suite is the only client while a
    motion test runs; nothing here is used to poll status when a sender
    may be attached (position for display comes from forgectrl)."""

    def __init__(self, host=None, port=None, timeout=5.0):
        self.host = host or os.environ.get("GRBL_HOST") or "127.0.0.1"
        self.port = int(port or os.environ.get("GRBL_PORT") or 23)
        self.timeout = timeout
        self.sock = None
        self.buf = b""

    def __enter__(self):
        self.connect()
        return self

    def __exit__(self, *exc):
        self.close()

    def connect(self):
        self.sock = socket.create_connection((self.host, self.port), timeout=self.timeout)
        # Drain the greeting.
        time.sleep(0.3)
        self.drain()

    def close(self):
        if self.sock:
            try:
                self.sock.close()
            except OSError:
                pass
            self.sock = None

    def _recv(self):
        """Pull whatever is waiting into the buffer; never blocks long."""
        self.sock.settimeout(0.05)
        try:
            while True:
                d = self.sock.recv(4096)
                if not d:
                    break
                self.buf += d
        except (socket.timeout, OSError):
            pass

    def _take_report(self):
        """Remove every complete <...> report from the buffer and return the
        last one, keeping the text around them. Status reports are the only
        thing consumed here: the driver's [MSG:] lines stay for drain()."""
        last = None
        while True:
            i = self.buf.find(b"<")
            j = self.buf.find(b">", i + 1) if i >= 0 else -1
            if i < 0 or j <= i:
                return last
            last = self.buf[i + 1:j].decode("utf-8", "replace")
            self.buf = self.buf[:i] + self.buf[j + 1:]

    def drain(self):
        self._recv()
        out, self.buf = self.buf, b""
        return out.decode("utf-8", "replace")

    def send_raw(self, data):
        self.sock.settimeout(self.timeout)
        self.sock.sendall(data)

    def command(self, line, timeout=None):
        """Send one line, return the response lines up to ok/error."""
        self.send_raw((line.strip() + "\n").encode())
        return self.wait_response(timeout or self.timeout)

    def wait_response(self, timeout):
        deadline = time.time() + timeout
        lines = []
        self.sock.settimeout(0.2)
        while time.time() < deadline:
            try:
                d = self.sock.recv(4096)
                if not d:
                    break
                self.buf += d
            except socket.timeout:
                pass
            while b"\n" in self.buf:
                raw, self.buf = self.buf.split(b"\n", 1)
                s = raw.decode("utf-8", "replace").strip()
                if not s:
                    continue
                lines.append(s)
                if s == "ok" or s.startswith("error:"):
                    return lines
        return lines

    def realtime(self, byte):
        self.send_raw(bytes([byte]))

    def status_report(self):
        """One '?' report, parsed: {'state': 'Idle', 'MPos': (x,y,z), ...}.
        The '?' is re-sent every 0.5 s until a report arrives: a soft
        reset (^X) flushes the controller's read buffer and eats a '?'
        that lands in it. Reports already buffered are stale and dropped -
        but only the reports: anything else the controller said is left in
        the buffer, so a test that polls for a state does not lose the
        [MSG:] line that explains it."""
        self._recv()
        self._take_report()                 # stale: predates this '?'
        self.send_raw(b"?")
        deadline = time.time() + self.timeout
        resend = time.time() + 0.5
        self.sock.settimeout(0.2)
        while time.time() < deadline:
            try:
                d = self.sock.recv(4096)
                if d:
                    self.buf += d
            except socket.timeout:
                pass
            if time.time() >= resend:
                self.send_raw(b"?")
                resend = time.time() + 0.5
            rep = self._take_report()
            if rep is not None:
                return parse_report(rep)
        raise HwError("no status report from grbl")


def parse_report(rep):
    parts = rep.split("|")
    out = {"state": parts[0]}
    for p in parts[1:]:
        if ":" in p:
            k, v = p.split(":", 1)
            if k in ("MPos", "WPos", "WCO"):
                try:
                    out[k] = tuple(float(x) for x in v.split(","))
                except ValueError:
                    out[k] = v
            else:
                out[k] = v
    return out
