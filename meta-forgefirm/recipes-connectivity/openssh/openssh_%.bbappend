# ForgeFIRM SSH policy, set in the installed files so the release image
# carries it as built:
#   PermitRootLogin no          root logs in at the serial console only
#   PermitEmptyPasswords no     an account without a password cannot log in
#   PasswordAuthentication yes  operator accounts log in with a password
# The dev image's debug-tweaks turns PermitRootLogin and
# PermitEmptyPasswords back to yes at rootfs time (ssh_allow_root_login
# and ssh_allow_empty_password in rootfs-postcommands.bbclass match the
# active lines too), so the bench keeps root over SSH.
#
# The init script starts sshd only when the control panel has turned it
# on (/run/forgefirm/ssh-enabled, tmpfs, gone at reboot) or on the dev
# image (/etc/forgefirm-dev). The guard sits in check_for_no_start, which
# start, reload and restart call; stop is never gated.
#
# The host keys live under /data/forgefirm/ssh: sshd_check_keys makes
# them at the first start (it reads the HostKey paths from the config),
# and they stay across updates, so the machine's fingerprint does not
# change with a release. The rootfs is read-only, and the
# read-only-rootfs image feature, finding no key in the image, selects
# sshd_config_readonly at rootfs time; both configs carry the same
# HostKey lines, so that selection changes nothing.

do_install:append() {
    for config in sshd_config sshd_config_readonly; do
        f=${D}${sysconfdir}/ssh/$config
        [ -e "$f" ] || continue
        sed -i \
            -e 's/^[#[:space:]]*PermitRootLogin .*/PermitRootLogin no/' \
            -e 's/^[#[:space:]]*PermitEmptyPasswords .*/PermitEmptyPasswords no/' \
            -e 's/^[#[:space:]]*PasswordAuthentication .*/PasswordAuthentication yes/' \
            -e '/^[#[:space:]]*HostKey /d' \
            "$f"
        for t in rsa ecdsa ed25519; do
            echo "HostKey /data/forgefirm/ssh/ssh_host_${t}_key" >> "$f"
        done
        grep -q '^PermitRootLogin no$' "$f" \
            && grep -q '^PermitEmptyPasswords no$' "$f" \
            && grep -q '^PasswordAuthentication yes$' "$f" \
            && [ "$(grep -c '^HostKey /data/forgefirm/ssh/' "$f")" = 3 ] \
            || bbfatal "$config: the ForgeFIRM policy lines did not land"
    done

    init=${D}${sysconfdir}/init.d/sshd
    sed -i '/^check_for_no_start() {$/a\
    [ -e /run/forgefirm/ssh-enabled ] || [ -e /etc/forgefirm-dev ] || {\
        echo "sshd: not enabled (turn it on from the ForgeFIRM control panel)"\
        exit 0\
    }' "$init"
    grep -q 'forgefirm/ssh-enabled' "$init" \
        || bbfatal "init.d/sshd: the check_for_no_start anchor was not found"
}
