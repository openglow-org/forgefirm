ForgeFIRM v0.0.11 is a beta release.

ForgeFIRM no longer tries to detect coolant flow. The only way the machine can see flow is the pair of temperature sensors on the coolant line, with a small heater between them. Their readings shift when the fans speed up and when the laser fires, and they differ widely from one machine to the next: a flow check that works on one machine cannot finish on another ([issue #5](https://github.com/openglow-org/forgefirm/issues/5)). With the limited number of beta testers that are providing log data, there isn't enough data to work with to try and develop a reliable solution. For now, the effort to monitor coolant flow is being shelved. The Coolant flow step of setup, the Flow verify and Flow calibrate diagnostics, and the flow fault during a job are gone. The machine still watches the coolant's temperature and won't fire when the coolant is too hot or too cold. Before you cut, make sure the pump runs and the coolant circulates.

This release does add a Coolant flow watch (Machine > Cooling protection > Coolant loop, off by default), to learn what these sensors see on other machines. It only writes what it sees to the log and never stops a job. If you want to contribute to the project, please turn this on and share logs.

The light on the machine button now tells you what the machine is doing:

- Breathing white: a job is waiting for you to press the button.
- Solid white: the machine is moving.
- Off: the machine is at rest.
- Flashing red: something needs your attention, and the laser won't fire. A banner on the control panel says why. After a fire, a crash, a fan failure, or coolant that got critically hot, the laser stays blocked until you press Acknowledge on the control panel or restart the machine, even after the problem is gone.

GRBL mode:

- Every job needs its own button press, even one that starts right after another.
- If your sender stops sending for more than a minute in the middle of a job (for example, the computer goes to sleep, or the job file has a long pause written into it), the laser locks. When the job carries on, it stops with alarm 3. Start it again. If your job files pause on purpose for longer than a minute, set Disarm after idle on the GRBL tab to more seconds.
- If a job can't start the laser (the button wasn't pressed in time, a cooling fault, the lid opened), it ends with alarm 3. Unlock the machine and start the job again.
- If you resume a paused job while the cooling is holding it, the job stays paused instead of ending. Resume it again once the cooling is ready.

Cloud mode:

- The machine now asks for the button press only once the laser is ready to fire, so the print cuts from its first move.
- If the cooling isn't ready when a print starts, the machine waits for it before asking for the button. If the cooling pauses a print, press the button when it breathes white to resume.
- The Glowforge app shows why a print is paused when the cooling holds it.

Also new and fixed:

- Reset all settings, on the System tab, puts ForgeFIRM back the way it was right after you installed it. Your account, settings and extensions are erased, and the setup runs again. Hold the machine button while you confirm.
- Control panel actions that ask you to hold the machine button are refused while a job is waiting for its button press or running, so holding the button for the control panel never counts as a job's press.
- The coolant heater turns itself off within 5 seconds if the software controlling it stops, and whenever the machine shuts down or reboots.
- If warming up the coolant takes more than 30 minutes, the machine stops heating, the job keeps waiting, and the control panel says why.
- The head light sometimes stayed on after a picture from the head camera. The machine now checks every setting it sends to the print head, and sends it again if it didn't take.
- A wrong password on the sign-in page now says so.
- The forgotten-password steps now match what the button shows: purple, then off, then amber.
- A log export now includes a sensors log: every sensor reading once a second, a summary of every job, and the two minutes before each fault. Attach it when you report a problem.

After you update:

- The old flow check's settings are gone. You don't need to do anything.

Expect bugs. When you find one, open an issue in this repository or report it on the community forum at https://community.openglow.org so it can be fixed.

Every release below 0.1.0 is a beta release. Installation, usage, and safety are documented at https://docs.forgefirm.org/.
