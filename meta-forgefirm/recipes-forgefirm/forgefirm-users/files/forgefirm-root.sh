# ForgeFIRM: an interactive root shell (the serial console, or SSH on
# the dev image) starts with a warning. Non-interactive shells (scp,
# rsync, ssh with a command) print nothing.
case "$-" in
  *i*)
    if [ "$(id -u 2>/dev/null)" = "0" ]; then
      echo "You are root on a laser cutter."
      echo "A wrong command here can damage the machine or hurt someone. Take care."
    fi
    ;;
esac
