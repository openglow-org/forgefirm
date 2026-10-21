ForgeFIRM v0.0.10 is a beta release.

The Coolant flow step of setup could not finish on some machines, a Glowforge Pro among them ([issue #5](https://github.com/openglow-org/forgefirm/issues/5)). The step warms the coolant flow sensor with the pump on and with it off, and it needs the two sets of readings at least 3 °C apart. It read single readings, which scatter more than the averages the machine checks during a job. When the readings were too close, it raised the heater to 55 and then 70 percent, and on a coolant loop that already read warm, that reached the 48 °C safety limit before the step could finish. Now:

- The step, and the Flow verify and Flow calibrate diagnostics, read averages, the way the check during a job does.
- When the readings are still too close, the heater steps up from 40 to 45 and then 50 percent, and only when the readings already taken show the stronger test staying well under the safety limit. Otherwise the step stops and asks you to check the coolant level and look for air in the lines.
- The log shows the coolant temperature each reading started from.

Also fixed:

- Your account name is no longer written to the logs. A sanitized log export now takes it out, including from lines an earlier release wrote and from the setup record the export carries.
- X and Y microsteps: on a machine where the setting was never changed, the control panel showed 8 while the machine ran at 32, and choosing 8 took effect only the next time the controller started. The panel now shows 32, and a change applies at once.

After you update:

- If your machine finished Coolant flow on an earlier release, it keeps the threshold it found then. Running the step again finds a new one from the averaged readings.

Expect bugs. When you find one, open an issue in this repository or report it on the community forum at https://community.openglow.org so it can be fixed.

Every release below 0.1.0 is a beta release. Installation, usage, and safety are documented at https://docs.forgefirm.org/.
