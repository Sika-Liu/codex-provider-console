#!/bin/sh
set -eu

# Docker runs the panel as the deployment user's numeric UID/GID. Add a
# temporary NSS record when that UID does not exist in the base image so tools
# such as ssh-keygen can resolve the current user without elevating privileges.
uid=$(id -u)
gid=$(id -g)
home=${HOME:-/user-home}
if ! grep -Eq "^[^:]*:[^:]*:${uid}:" /etc/passwd; then
    nss_wrapper=$(find /usr/lib -type f -name libnss_wrapper.so -print -quit 2>/dev/null || true)
    if [ -n "$nss_wrapper" ]; then
        runtime_dir="/tmp/codex-panel-nss-${uid}"
        mkdir -p "$runtime_dir"
        passwd_file="$runtime_dir/passwd"
        group_file="$runtime_dir/group"
        cp /etc/passwd "$passwd_file"
        cp /etc/group "$group_file"
        printf 'codex-panel:x:%s:%s:Codex Panel:%s:/usr/sbin/nologin\n' "$uid" "$gid" "$home" >> "$passwd_file"
        if ! grep -Eq "^[^:]*:[^:]*:${gid}:" /etc/group; then
            printf 'codex-panel:x:%s:\n' "$gid" >> "$group_file"
        fi
        export LD_PRELOAD="$nss_wrapper${LD_PRELOAD:+:$LD_PRELOAD}"
        export NSS_WRAPPER_PASSWD="$passwd_file"
        export NSS_WRAPPER_GROUP="$group_file"
    fi
fi

exec "$@"
