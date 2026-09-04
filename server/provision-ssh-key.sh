#!/bin/sh
set -eu

# Usage: cat ~/.ssh/rsshub_cookie_sync.pub | ./provision-ssh-key.sh
# The key is read from stdin so it never appears in a shell argument or log.
PATH=/usr/sbin:/usr/bin:/sbin:/bin
export PATH
LC_ALL=C
export LC_ALL

die() {
    echo "provision-ssh-key.sh: $*" >&2
    exit 1
}

for command_name in id getent awk cut dd wc sed stat ssh-keygen install mktemp chown chmod mv usermod; do
    command -v "$command_name" >/dev/null 2>&1 || die "required command is missing: $command_name"
done
[ -x /usr/bin/python3 ] || die "required command is missing: /usr/bin/python3"

if [ "$(id -u)" -ne 0 ]; then
    die "must run as root"
fi

if ! id rsshub-sync >/dev/null 2>&1; then
    die "rsshub-sync account is missing; run install.sh first"
fi

account=$(getent passwd rsshub-sync)
account_shell=$(printf '%s\n' "$account" | awk -F: 'NR == 1 { print $7 }')
if [ "$account_shell" != /bin/sh ] || [ "$(id -u rsshub-sync)" -eq 0 ] || [ "$(id -g rsshub-sync)" -eq 0 ]; then
    die "rsshub-sync account failed security checks"
fi
primary_group=$(id -gn rsshub-sync)
if [ "$(id -Gn rsshub-sync)" != "$primary_group" ]; then
    die "rsshub-sync account has supplementary groups"
fi

# Read one byte beyond the limit so oversized input cannot be silently
# truncated into a valid-looking key.  The input is public material, but the
# temporary file remains root-only and is always removed by the trap.
umask 077
input_file=$(mktemp /tmp/rsshub-cookie-sync-public-key.XXXXXX)
temporary=
trap 'rm -f "$input_file" ${temporary:+"$temporary"}' EXIT HUP INT TERM
dd bs=8193 count=1 of="$input_file" 2>/dev/null
input_size=$(wc -c < "$input_file") || die "cannot read public key"
[ "$input_size" -ge 1 ] && [ "$input_size" -le 8192 ] \
    || die "public key must be between 1 and 8192 bytes"
if ! /usr/bin/python3 - "$input_file" <<'PY'
import pathlib
import sys

payload = pathlib.Path(sys.argv[1]).read_bytes()
if b"\x00" in payload or b"\r" in payload:
    raise SystemExit(1)
body = payload[:-1] if payload.endswith(b"\n") else payload
if not body or b"\n" in body:
    raise SystemExit(1)
PY
then
    die "provide exactly one public key line without control characters"
fi
key=$(sed -n '1p' "$input_file") || die "cannot read public key"
case "$key" in
    ssh-ed25519\ *) : ;;
    *) die "expected one ssh-ed25519 public key on stdin" ;;
esac

home=$(getent passwd rsshub-sync | cut -d: -f6)
if [ -z "$home" ]; then
    die "cannot determine rsshub-sync home"
fi
if [ "$home" != /var/lib/rsshub-sync ]; then
    die "rsshub-sync home is not the expected path"
fi
[ ! -L "$home" ] && [ -d "$home" ] || die "rsshub-sync home is not a safe directory"
if ! printf '%s\n' "$key" | ssh-keygen -lf - >/dev/null 2>&1; then
    die "invalid OpenSSH public key"
fi
if [ -L "$home/.ssh" ]; then
    die "ssh directory must not be a symlink"
fi
if [ -e "$home/.ssh" ] && [ ! -d "$home/.ssh" ]; then
    die "ssh path is not a directory"
fi
group=$(id -gn rsshub-sync)
[ "$(stat -c '%U' -- "$home")" = rsshub-sync ] \
    || die "rsshub-sync home has an unexpected owner"
[ "$(stat -c '%G' -- "$home")" = "$group" ] \
    || die "rsshub-sync home has an unexpected group"
home_mode=$(stat -c '%A' -- "$home") || die "cannot inspect rsshub-sync home"
case "$home_mode" in
    ?????w????|????????w?) die "rsshub-sync home must not be writable by group or others" ;;
esac
if [ -d "$home/.ssh" ]; then
    [ "$(stat -c '%U' -- "$home/.ssh")" = rsshub-sync ] \
        || die "ssh directory has an unexpected owner"
    [ "$(stat -c '%G' -- "$home/.ssh")" = "$group" ] \
        || die "ssh directory has an unexpected group"
    [ "$(stat -c '%a' -- "$home/.ssh")" = 700 ] \
        || die "ssh directory permissions must be 0700"
fi
install -d -o rsshub-sync -g "$group" -m 0700 "$home/.ssh"
authorized_keys=$home/.ssh/authorized_keys
if [ -L "$authorized_keys" ]; then
    die "authorized_keys must not be a symlink"
fi
if [ -e "$authorized_keys" ]; then
    [ -f "$authorized_keys" ] || die "authorized_keys is not a regular file"
    [ "$(stat -c '%U' -- "$authorized_keys")" = rsshub-sync ] \
        || die "authorized_keys is not owned by rsshub-sync"
    auth_mode=$(stat -c '%A' -- "$authorized_keys") || die "cannot inspect authorized_keys"
    case "$auth_mode" in
        ?????w????|????????w?) die "authorized_keys must not be writable by group or others" ;;
    esac
fi
authorized_key="restrict,command=\"sudo -n /usr/local/sbin/rsshub-cookie-sync-apply\" $key"
temporary=$(mktemp "$home/.ssh/.authorized_keys.XXXXXX")
if [ -f "$authorized_keys" ]; then
    # Replace only this project's forced-command entry.  A pre-existing
    # rsshub-sync account may have unrelated administrator-managed keys; those
    # must survive both provisioning and uninstall.
    awk '!/^restrict,command="sudo -n \/usr\/local\/sbin\/rsshub-cookie-sync-apply" ssh-ed25519 /' \
        "$authorized_keys" > "$temporary"
fi
printf '%s\n' "$authorized_key" >> "$temporary"
chown rsshub-sync:"$group" "$temporary"
chmod 0600 "$temporary"
mv -f "$temporary" "$authorized_keys"
temporary=
trap - EXIT HUP INT TERM
rm -f "$input_file"
usermod --lock rsshub-sync
echo "SSH public key installed for rsshub-sync."
