#!/bin/sh
set -eu

# Usage: cat ~/.ssh/rsshub_cookie_sync.pub | ./provision-ssh-key.sh
# The key is read from stdin so it never appears in a shell argument or log.
PATH=/usr/sbin:/usr/bin:/sbin:/bin
export PATH

if [ "$(id -u)" -ne 0 ]; then
    echo "provision-ssh-key.sh must run as root" >&2
    exit 1
fi

if ! id rsshub-sync >/dev/null 2>&1; then
    echo "rsshub-sync account is missing; run install.sh first" >&2
    exit 1
fi

account=$(getent passwd rsshub-sync)
account_shell=$(printf '%s\n' "$account" | awk -F: 'NR == 1 { print $7 }')
if [ "$account_shell" != /bin/sh ] || [ "$(id -u rsshub-sync)" -eq 0 ] || [ "$(id -g rsshub-sync)" -eq 0 ]; then
    echo "rsshub-sync account failed security checks" >&2
    exit 1
fi
primary_group=$(id -gn rsshub-sync)
if [ "$(id -Gn rsshub-sync)" != "$primary_group" ]; then
    echo "rsshub-sync account has supplementary groups" >&2
    exit 1
fi

key=$(dd bs=1 count=8192 2>/dev/null)
if [ "$(printf '%s\n' "$key" | awk 'END { print NR }')" -ne 1 ]; then
    echo "provide exactly one public key line" >&2
    exit 1
fi
case "$key" in
    ssh-ed25519\ *) : ;;
    *) echo "expected one ssh-ed25519 public key on stdin" >&2; exit 1 ;;
esac
if printf '%s' "$key" | grep -q "$(printf '\r')"; then
    echo "public key contains a carriage return" >&2
    exit 1
fi

home=$(getent passwd rsshub-sync | cut -d: -f6)
if [ -z "$home" ]; then
    echo "cannot determine rsshub-sync home" >&2
    exit 1
fi
if [ "$home" != /var/lib/rsshub-sync ]; then
    echo "rsshub-sync home is not the expected path" >&2
    exit 1
fi
if ! printf '%s\n' "$key" | ssh-keygen -lf - >/dev/null 2>&1; then
    echo "invalid OpenSSH public key" >&2
    exit 1
fi
if [ -L "$home/.ssh" ]; then
    echo "ssh directory must not be a symlink" >&2
    exit 1
fi
if [ -e "$home/.ssh" ] && [ ! -d "$home/.ssh" ]; then
    echo "ssh path is not a directory" >&2
    exit 1
fi
group=$(id -gn rsshub-sync)
install -d -o rsshub-sync -g "$group" -m 0700 "$home/.ssh"
authorized_key="restrict,command=\"sudo -n /usr/local/sbin/rsshub-cookie-sync-apply\" $key"
umask 077
temporary=$(mktemp "$home/.ssh/.authorized_keys.XXXXXX")
trap 'rm -f "$temporary"' EXIT HUP INT TERM
printf '%s\n' "$authorized_key" > "$temporary"
chown rsshub-sync:"$group" "$temporary"
chmod 0600 "$temporary"
mv -f "$temporary" "$home/.ssh/authorized_keys"
trap - EXIT HUP INT TERM
usermod --lock rsshub-sync
echo "SSH public key installed for rsshub-sync."
