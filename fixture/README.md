# forgefixture: the bench actuator

The acceptance campaign asks a person for about eighty small things,
and most of them are the same three: open the lid, pull the interlock,
press the button. This is the box that does those on request, so the
tool can run an operator test with nobody in the room. An ESP32-S3
DevKitC-1 on the bench network drives three relays wired into the
machine's own connectors; the acceptance tool (`forgetest`, on the
machine) asks it over HTTP and then watches the machine for the result,
the way it watches an operator's hand today.

Nothing here touches the laser's safety chain. Two of the contacts are
normally closed and sit in series with loops the chain already reads;
the third is normally open across the button input and is only ever
pulsed. With the fixture unpowered, unplugged, rebooting or crashed, the
machine is a stock machine.

## Hardware

| Part | What |
|---|---|
| ESP32-S3-DevKitC-1 | any flash size; powered from its USB port by a **USB wall adapter** (see grounds) |
| 3 × 1-channel 3.3 V optocoupler relay modules, high-level trigger | VCC, GND, IN; coil ~100 mA each |
| a 2-pin header and jumper | the button channel's enable |
| the interposer harness at the machine | bench-local; described in the project's hardware facts bank, not here |

Pins on the DevKit (all plain GPIOs, no strapping, USB, flash or PSRAM
role):

| Signal | GPIO | Relay contact | Where in the machine |
|---|---|---|---|
| lid | 4 | **NC**, in series with the lid-switch loop | energized = the loop opens = lid open |
| interlock | 5 | **NC**, in series with the interlock loop | energized = the loop opens = interlock pulled |
| button | 6 | **NO**, across the button input | energized = pressed; pulsed only, 20 to 500 ms |
| button enable | 7 | input, internal pull-up | a jumper to GND enables the button channel; no jumper, no presses |

Relay coils from the machine's 3.3 V (the modules' VCC and GND), the
three IN pins from the GPIOs above, the DevKit's GND to the modules'
GND. The modules' opto inputs share GND with their coils, so the ESP32
and the machine share a ground: power the DevKit from a wall adapter,
not from a PC, unless you want that PC's ground on the machine.

## Build and flash

The only input is `fixture.env`: the wifi network, the API key, the
hostname. Everything else is pinned (ESP-IDF v5.5, the mDNS component in
`dependencies.lock`).

    ./fixture.sh env              # fixture.env from the example, with a fresh key; fill in the wifi
    ./fixture.sh build            # idf.py if installed, else the espressif/idf container (docker or podman)
    ./fixture.sh flash COM5       # or /dev/ttyUSB0; esptool from pip talks to the board directly
    ./fixture.sh monitor COM5     # the log

Either USB port of the DevKit flashes; the one marked UART shows the log
too. `fixture.env` is git-ignored and never leaves the bench. A build in
the container works anywhere docker or podman runs (Git Bash on Windows
included); `pip install esptool pyserial` is the whole host-side
requirement for flashing and watching.

## What it does on the network

It joins the wifi as `forgefixture` (the `HOSTNAME` in `fixture.env`),
sends that name in its DHCP request, and announces `forgefixture.local`
over mDNS with a `_forgefixture._tcp` service. It reconnects forever and
never sleeps the radio. The API is HTTP on port 80, JSON, LAN only, no
OTA (the flash happens over USB and nowhere else).

Every request carries the key in `X-Fixture-Key`; without it, or with
a wrong one, every path answers 401 and the attempt is logged with the
caller's address.

| Request | Does |
|---|---|
| `GET /` or `/state` | identity, firmware version, uptime, the three channels' states, whether the button is enabled, the wifi link |
| `POST /lid {"state":"open"}` | energizes the lid channel (the loop opens); `"close"` releases it |
| `POST /interlock {"state":"open"}` | the same for the interlock loop |
| `POST /button {"ms":200}` | one press, `ms` clamped into 20 to 500 (200 when absent); 409 while the enable jumper is out or a press is still in progress |
| `POST /release` | every channel released |

An action answers with the state as `GET /` shows it. The fixture does
not read the machine's switches; the tool verifies every action through
the machine's own readings, which is the point.

    curl -s -H "X-Fixture-Key: $KEY" http://forgefixture.local/
    curl -s -H "X-Fixture-Key: $KEY" -d '{"state":"open"}' http://forgefixture.local/lid

## The tool's side

`forgetest` looks for `/data/forgetest/fixture.json` on the machine:

    {"hostname": "forgefixture", "key": "<the API_KEY>", "ip": null,
     "channels": ["lid", "interlock", "button"], "arm_press": false}

`ip` overrides the mDNS lookup (the tool resolves `<hostname>.local`
itself; the image has no mDNS resolver). `channels` names what is
wired. `arm_press` stays false unless the fixture may press the button
to arm the laser for a live test; by default that press is a person's.
The file is mode 0600 and bench-local. With the fixture up, an
`operator` test whose actions it covers runs in the unattended queue;
`live` tests keep their kind and their acknowledgment, and every action
records who performed it. Presses are spaced by the tool: the next one
waits for the last pulse to end plus a 300 ms release, so the machine
sees two presses and not one long one.

## What keeps it safe

- Every line is driven low first thing at boot, before the radio or the
  server exist, and after any reset.
- The button is a pulse whose end is armed before the line rises; a hung
  task trips the task watchdog, which panics and reboots to all-low.
- The button channel needs the jumper; a LAN key leak cannot press the
  button on a fixture whose jumper is out.
- NC contacts in the loops: a fixture fault can only add an open, never
  mask a real lid open or a pulled interlock.
- No OTA, no configuration over the network, nothing persisted beyond
  the wifi driver's own calibration.

## Layout

    main/         the firmware: main.c, wifi.c, api.c, relays.c, policy.c (the pure decisions)
    test/         the host tests of policy.c and relays.c (./fixture.sh test)
    fixture.env.example, sdkconfig.defaults, dependencies.lock
