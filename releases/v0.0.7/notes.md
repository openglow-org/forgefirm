ForgeFIRM v0.0.7 is a beta release.

ForgeFIRM is now extensible. New capabilities and features can be created as extensions without requiring major changes to the core firmware. This is intended to promote 3rd-party feature development while allowing the core firmware to remain light and stable. Each extension runs in a sandbox with only the permissions the user grants it. Packages can be uploaded or installed from a new catalog.

Also new:

- Manual homing: Allows you to home the machine by moving the gantry and head to their home positions against gantry stops. Cheap, repeatable and effective.
- Crumb Tray Removal: Toggling whether or not the crumb tray is installed will adjust the Z height for accurate, calculation-free focus.
- A new API interface lets your own programs read the status, follow the machine's events, see a camera, jog the head with the laser off, or run jobs (operator button press still required).
- Improved the video pipeline and enabled hardware-accelerated TLS encryption to reduce CPU overhead.
- New Extension: Adjust the laser start point using the head camera for precision alignment.

Expect bugs. When you find one, open an issue in this repository or report it on the community forum at https://community.openglow.org so it can be fixed.

Every release below 0.1.0 is a beta release. Installation, usage, and safety are documented at https://docs.forgefirm.org/.
