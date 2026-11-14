#!/bin/sh
# Optional Optimus macOS support package: ask, then download and verify.
#
# Double-click in Finder, or run:  sh Get-Optimus-macOS-Support.command [option]
#   (no option)        ask, then download and install if you agree
#   --status           show what is installed
#   --uninstall        remove the support package
#   --yes              install without the question (for scripted setups)
#   --from-directory D install from a local copy (still fully verified)
#
# Nothing is downloaded until you agree. Every file is fetched from one commit
# of Ag3nt47/Optimus-macOS-Support that is pinned below, and must match the
# reviewed SHA-256 before it is installed. Only /bin/sh, curl and shasum are
# used, so this works with the prebuilt app and on older macOS. It never asks
# for administrator rights and changes nothing outside the folder below.
set -eu
PATH=/usr/bin:/bin:/usr/sbin:/sbin
export PATH
umask 077

# ── Reviewed pin (update together with the support repository) ─────────────
REPOSITORY=Ag3nt47/Optimus-macOS-Support
COMMIT=98f7ec5bf114c8691a764e6fa80d17e6ff011fd3
VERSION=1.0.0
MANIFEST_SHA256=17463a800d2b537de59ff22c2514a2be8bb1d1fd6714ba8e1fa36910b6888c50
MANIFEST_SIZE=1452
MAX_FILE_BYTES=1048576
MAX_FILES=128
# ───────────────────────────────────────────────────────────────────────────

SUPPORT="$HOME/Library/Application Support/Optimus/macos-support"
INTERACTIVE=0
[ -t 0 ] && [ -t 1 ] && INTERACTIVE=1

finish() {
    status=$1
    # Keep a Finder-opened Terminal window readable before it closes.
    if [ "$INTERACTIVE" -eq 1 ] && [ -z "${OPTIMUS_SUPPORT_NO_PAUSE:-}" ]; then
        printf '\nPress Return to close. '
        read -r _ || true
    fi
    exit "$status"
}
fail() { printf 'Optimus macOS support: %s\n' "$1" >&2; finish "${2:-1}"; }

[ "$(uname -s)" = Darwin ] || fail 'this package is for macOS.' 2
[ "$(id -u)" -ne 0 ] || fail 'run this as your normal account, not as root.' 2

MODE=ask
SOURCE_DIR=
while [ "$#" -gt 0 ]; do
    case "$1" in
        --status) MODE=status; shift ;;
        --uninstall) MODE=uninstall; shift ;;
        --yes) MODE=yes; shift ;;
        --from-directory)
            [ "$#" -ge 2 ] || fail '--from-directory needs a folder.' 2
            SOURCE_DIR=$2; shift 2 ;;
        --help|-h) sed -n '2,9p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
        *) fail "unknown option: $1" 2 ;;
    esac
done

installed_sha() { sed -n 's/^manifest_sha256=//p' "$SUPPORT/installed.txt" 2>/dev/null | head -n 1; }

if [ "$MODE" = status ]; then
    if [ -f "$SUPPORT/installed.txt" ]; then
        printf 'Installed: %s\n' "$(sed -n 's/^version=//p' "$SUPPORT/installed.txt")"
        if [ "$(installed_sha)" = "$MANIFEST_SHA256" ]; then
            printf 'Matches this Optimus revision: yes\n'
        else
            printf 'Matches this Optimus revision: no (run this command again to update)\n'
        fi
        printf 'Scripts: %s/current/scripts\n' "$SUPPORT"
    else
        printf 'The Optimus macOS support package is not installed.\n'
    fi
    exit 0
fi

if [ "$MODE" = uninstall ]; then
    if [ -L "$SUPPORT" ]; then fail 'refusing a symbolic-link support folder.'; fi
    if [ -d "$SUPPORT" ]; then
        rm -rf -- "$SUPPORT/packs" "$SUPPORT/current" "$SUPPORT/installed.txt"
        rmdir "$SUPPORT" 2>/dev/null || true
        printf 'The Optimus macOS support package was removed.\n'
    else
        printf 'The Optimus macOS support package was not installed.\n'
    fi
    exit 0
fi

if [ "$(installed_sha)" = "$MANIFEST_SHA256" ] && [ -d "$SUPPORT/current/scripts" ]; then
    printf 'The Optimus macOS support package %s is already installed.\nScripts: %s/current/scripts\n' "$VERSION" "$SUPPORT"
    finish 0
fi

QUESTION="Download the optional Optimus macOS support package (version $VERSION) from GitHub?

It adds Mac versions of the Windows-only Optimus scripts (safe stop, performance report, update continuation, drive-space check) and Mac reviews for built-in protection, startup items, storage health and network reset, plus notes for older macOS.

Every file is checked against a pinned SHA-256 before it is installed. Nothing asks for administrator rights."

agreed=0
if [ "$MODE" = yes ]; then
    agreed=1
elif [ -x /usr/bin/osascript ] && [ -z "${SSH_CONNECTION:-}" ]; then
    answer=$(/usr/bin/osascript - "$QUESTION" <<'APPLESCRIPT' 2>/dev/null || true
on run argv
    display dialog (item 1 of argv) with title "Optimus macOS support" buttons {"Not now", "Download"} default button "Download" cancel button "Not now"
    return button returned of result
end run
APPLESCRIPT
)
    [ "$answer" = Download ] && agreed=1
elif [ "$INTERACTIVE" -eq 1 ]; then
    printf '%s\n\nDownload now? [y/N] ' "$QUESTION"
    read -r reply || reply=
    case "$reply" in y|Y|yes|YES) agreed=1 ;; esac
else
    fail 'no answer is possible without a terminal; re-run with --yes to agree.' 2
fi
if [ "$agreed" -ne 1 ]; then
    printf 'Nothing was downloaded. Run this command again whenever you want the support package.\n'
    finish 0
fi

# ── Download and verify into a private temporary folder ───────────────────
WORK=$(mktemp -d "${TMPDIR:-/tmp}/optimus-support.XXXXXXXX") || fail 'could not create a temporary folder.'
cleanup() { rm -rf -- "$WORK"; }
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' HUP TERM

fetch() {  # fetch RELATIVE_PATH DEST MAX_BYTES
    if [ -n "$SOURCE_DIR" ]; then
        src="$SOURCE_DIR/$1"
        [ -f "$src" ] && [ ! -L "$src" ] || return 1
        cp -- "$src" "$2"
    else
        curl --silent --show-error --fail --proto '=https' --max-redirs 0 \
            --max-time 60 --max-filesize "$(($3 + 1))" \
            --output "$2" -- "https://raw.githubusercontent.com/$REPOSITORY/$COMMIT/$1"
    fi
}
verify() {  # verify FILE SHA256 SIZE
    [ "$(stat -f %z "$1")" = "$3" ] && [ "$(shasum -a 256 "$1" | cut -d' ' -f1)" = "$2" ]
}

printf 'Downloading the Optimus macOS support package %s...\n' "$VERSION"
fetch pack-manifest.txt "$WORK/manifest" "$MANIFEST_SIZE" || fail 'could not download the package manifest. Nothing was installed.'
verify "$WORK/manifest" "$MANIFEST_SHA256" "$MANIFEST_SIZE" || \
    fail 'the package manifest does not match the reviewed pin. Nothing was installed.'
[ "$(head -n 1 "$WORK/manifest")" = "# optimus-macos-support $VERSION" ] || \
    fail 'the package manifest is not the reviewed version. Nothing was installed.'

STAGE="$WORK/stage"
mkdir "$STAGE"
count=0
tail -n +2 "$WORK/manifest" > "$WORK/entries"
while read -r digest size mode path; do
    count=$((count + 1))
    [ "$count" -le "$MAX_FILES" ] || fail 'the package lists too many files.'
    printf '%s %s %s %s\n' "$digest" "$size" "$mode" "$path" | LC_ALL=C grep -Eq \
        '^[0-9a-f]{64} [0-9]{1,7} (644|755) ((scripts|docs)/[A-Za-z0-9][A-Za-z0-9._-]{0,120}|README\.md|LICENSE)$' || \
        fail "the package manifest has an unsafe entry. Nothing was installed."
    [ "$size" -le "$MAX_FILE_BYTES" ] || fail "$path is larger than allowed. Nothing was installed."
    mkdir -p "$STAGE/$(dirname "$path")"
    fetch "$path" "$STAGE/$path" "$size" || fail "could not download $path. Nothing was installed."
    verify "$STAGE/$path" "$digest" "$size" || \
        fail "$path does not match the reviewed package. Nothing was installed."
    chmod "$mode" "$STAGE/$path"
done < "$WORK/entries"
[ "$count" -gt 0 ] || fail 'the package manifest lists no files.'

# ── Install: every byte verified; switch the current version atomically ───
[ -L "$SUPPORT" ] && fail 'refusing a symbolic-link support folder.'
mkdir -p "$SUPPORT/packs"
chmod 700 "$SUPPORT" "$SUPPORT/packs"
TARGET="$SUPPORT/packs/$VERSION-$(printf '%s' "$MANIFEST_SHA256" | cut -c1-12)"
rm -rf -- "$TARGET"
mv "$STAGE" "$TARGET"
ln -s "packs/$(basename "$TARGET")" "$SUPPORT/.current-new"
mv -f -h "$SUPPORT/.current-new" "$SUPPORT/current" 2>/dev/null || {
    rm -f "$SUPPORT/current"; mv "$SUPPORT/.current-new" "$SUPPORT/current"; }
for old in "$SUPPORT/packs"/*; do
    [ "$old" = "$TARGET" ] || rm -rf -- "$old"
done
printf 'repository=%s\ncommit=%s\nversion=%s\nmanifest_sha256=%s\ninstalled_at=%s\n' \
    "$REPOSITORY" "$COMMIT" "$VERSION" "$MANIFEST_SHA256" "$(date +%s)" > "$SUPPORT/installed.txt"

printf '\nInstalled and verified %s file(s).\nScripts: %s/current/scripts\n' "$count" "$SUPPORT"
printf 'Start with: sh "%s/current/scripts/optimus-compat.sh"\n' "$SUPPORT"
if [ "$INTERACTIVE" -eq 1 ] && [ -z "${OPTIMUS_SUPPORT_NO_PAUSE:-}" ]; then
    /usr/bin/open "$SUPPORT/current" 2>/dev/null || true
fi
finish 0
