ForgeFIRM v0.0.8 is a beta release.

Machines with one intake fan: some Glowforge machines are built with a single intake fan instead of two. On those machines, the Airflow step of setup stopped with an error because the second fan never turned. Setup now detects a single intake fan, tells you so, and finishes. The second intake is then not checked while the machine runs. Machines with two intake fans work as before.

Expect bugs. When you find one, open an issue in this repository or report it on the community forum at https://community.openglow.org so it can be fixed.

Every release below 0.1.0 is a beta release. Installation, usage, and safety are documented at https://docs.forgefirm.org/.
