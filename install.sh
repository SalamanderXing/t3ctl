#!/usr/bin/env bash
# install.sh — install the t3ctl tooling on a devbox. Idempotent; run as root
# from a checkout (or an archive) of this repo at a tagged version.
#
#   bin/*        -> /usr/local/bin/{t3ctl,t3-token-renew,t3-notify}
#                   (+ a symlink named $T3CTL_NOTIFY_BIN if it differs)
#   units/*      -> /etc/systemd/system/  (token renew timer, callback approver)
#   skills/      -> /usr/local/share/t3ctl/skills   (add to hermes config:
#                   skills.external_dirs: [/usr/local/share/t3ctl/skills])
#   t3ctl.conf.example -> /etc/t3ctl.conf   (only if missing; edit it)
#   VERSION      -> /var/lib/t3ctl/version, plus a sha256 manifest of every
#                   installed path for the host's drift check.
#
# It does not mint a token (systemctl start t3-token-renew does, once
# /etc/t3ctl.conf is right) and does not create the hermes webhook route or
# its secret — see README "Wiring the callback".
set -euo pipefail

SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
STATE=/var/lib/t3ctl
SHARE=/usr/local/share/t3ctl
[ "$(id -u)" -eq 0 ] || { echo "install.sh: run as root" >&2; exit 1; }

changed=(); manifest=()
put() { # put SRC DEST MODE
  manifest+=("$2")
  if [ -e "$2" ] && cmp -s "$1" "$2"; then return 0; fi
  install -D -m "$3" "$1" "$2"; echo "wrote    $2"; changed+=("$2")
}

for f in "$SRC"/bin/*; do put "$f" "/usr/local/bin/$(basename "$f")" 0755; done
for f in "$SRC"/units/*; do put "$f" "/etc/systemd/system/$(basename "$f")" 0644; done
while IFS= read -r f; do
  put "$f" "$SHARE/skills/${f#"$SRC"/skills/}" 0644
done < <(find "$SRC/skills" -type f)

if [ ! -e /etc/t3ctl.conf ]; then
  install -m 0644 "$SRC/t3ctl.conf.example" /etc/t3ctl.conf
  echo "wrote    /etc/t3ctl.conf (from the example — edit it, then: systemctl start t3-token-renew)"
fi
# shellcheck disable=SC1091
. /etc/t3ctl.conf
notify_bin=${T3CTL_NOTIFY_BIN:-t3-notify}
if [ "$notify_bin" != t3-notify ]; then
  if [ "$(readlink -f "/usr/local/bin/$notify_bin" 2>/dev/null)" != /usr/local/bin/t3-notify ]; then
    ln -sfn /usr/local/bin/t3-notify "/usr/local/bin/$notify_bin"; echo "linked   /usr/local/bin/$notify_bin -> t3-notify"
  fi
  manifest+=("/usr/local/bin/t3-notify")
fi
mkdir -p "$(dirname "${T3CTL_TOKEN_FILE:-/etc/t3ctl/token}")"

systemctl daemon-reload
systemctl enable -q t3-token-renew.timer t3-callback-approver.service
systemctl is-active -q t3-token-renew.timer || systemctl start t3-token-renew.timer
if [ -r "${T3CTL_TOKEN_FILE:-/etc/t3ctl/token}" ]; then
  systemctl is-active -q t3-callback-approver.service || systemctl start t3-callback-approver.service
  if printf '%s\n' "${changed[@]}" | grep -qE '/t3ctl$|t3-callback-approver'; then
    systemctl restart t3-callback-approver.service; echo "restarted t3-callback-approver"
  fi
else
  echo "note     no token at ${T3CTL_TOKEN_FILE:-/etc/t3ctl/token} yet — approver left stopped; run: systemctl start t3-token-renew"
fi

mkdir -p "$STATE"
cp "$SRC/VERSION" "$STATE/version"
: > "$STATE/manifest.sha256"
for p in "${manifest[@]}"; do [ -f "$p" ] && sha256sum "$p" >> "$STATE/manifest.sha256"; done
echo "t3ctl $(cat "$STATE/version") installed (${#changed[@]} path(s) changed, manifest $(wc -l < "$STATE/manifest.sha256") files)"
