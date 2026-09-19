ForgeFIRM v0.0.6 is a beta release.

It fixes a boot that never finished on some networks: a router that refused the machine's DHCPv6 request held the boot before the console login, SSH, and the control panel started, while the machine still answered ping. ForgeFIRM no longer runs a DHCPv6 client. IPv6 addresses come from the router advertisement (SLAAC), and a network that offers only stateful DHCPv6 leaves the machine on IPv4. The installer now resumes and retries a firmware download that fails, and it keeps a log of every run that the control panel's log export carries, so an install that went wrong can be diagnosed afterward. The X and Y axes now default to x32 microstepping.

Expect bugs. When you find one, open an issue in this repository or report it on the community forum at https://community.openglow.org so it can be fixed.

Every release below 0.1.0 is a beta release. Installation, usage, and safety are documented at https://docs.forgefirm.org/.
