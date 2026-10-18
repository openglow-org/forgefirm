ForgeFIRM v0.0.9 is a beta release.

The setup is simpler, and it is safe to run again. On some machines, running First fire a second time, or after a power cycle, drove the head into the side of the machine. The setup sheet and its steps (Place the sheet, First fire, Dose curve, Corner rolloff, Flow under load) are gone. Focus and the optional Laser floor now each burn on their own small scrap in the back-left corner, starting from home against the gantry stops. A new Lens travel check measures the lens with the laser off. Only the first steps, through Intake fans and Coolant flow, are required. The rest are recommended, and Finish later takes you to the control panel.

Also fixed:

- Stopping mid-move (a reset from your Grbl sender, Abort in the control panel, or a cooling fault) now slows the head to a stop and keeps its position, so the machine stays homed.
- A rare stream fault that could replay leftover motion, with the laser off, at the start of the next job now stops with an alarm.
- The setup's burns no longer need the head's beam sensor, which reads nothing on some heads.
- The setup no longer shows the head as "not detected".
- The TEC check is gone. It could never pass, and it turned the TEC off on Pro machines. Your answer in the Your machine step now sets whether a TEC is fitted.
- A setup question or wait that times out now ends its step with the reason and a way to run it again.
- The coolant flow calibration no longer leaves its trial settings behind after an abort.
- The logs now record which firmware runs at every start and every update, so a log export shows how a machine was updated.
- Extensions: a rollback or an interrupted install no longer leaves an extension broken.

After you update:

- If your machine has not done the Intake fans or Coolant flow step, the setup asks for it, and the machine waits for it before it runs a job.
- Pro owners who ran the setup on an earlier release: make sure Cooling protection > Coolant loop > TEC (Pro chiller) is on. The old TEC check turned it off.
- If you ran the setup sheet on an earlier release and your jobs now land offset from where you placed them, send G92.1 once from your Grbl sender.

Expect bugs. When you find one, open an issue in this repository or report it on the community forum at https://community.openglow.org so it can be fixed.

Every release below 0.1.0 is a beta release. Installation, usage, and safety are documented at https://docs.forgefirm.org/.
