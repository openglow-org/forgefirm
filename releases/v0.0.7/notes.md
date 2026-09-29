ForgeFIRM v0.0.7 is a beta release.

It adds extension packages: software that is not part of ForgeFIRM, which you install from the control panel and which runs in a sandbox with only what you grant it. Extensions are off until you turn them on. You can upload a package, or get one from OpenGlow's catalog, which lists Visual alignment. A package's own page is on the new Extensions tab.

Also new:

- Manual homing. Release the motors, push the head against the gantry stops by hand, and set home there or at an offset you choose. It needs no account, no service, and no camera.
- The crumb tray. Take the tray out for taller work and switch the tray mode on the Lens card: Z is then the height above the floor of the machine.
- API tokens let your own programs read the status, follow the machine's events, see a camera, jog the head with the laser off, or run jobs. A token reaches only what you tick when you make it, and a job it runs still waits for the button on the machine.
- The Setup page can measure the bed size on a machine with the gantry stops.
- Live camera video is smoother and has less delay, and the control panel is faster over HTTPS.

Expect bugs. When you find one, open an issue in this repository or report it on the community forum at https://community.openglow.org so it can be fixed.

Every release below 0.1.0 is a beta release. Installation, usage, and safety are documented at https://docs.forgefirm.org/.
