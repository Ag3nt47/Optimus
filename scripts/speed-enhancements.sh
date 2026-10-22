#!/usr/bin/env bash
# Run from a portable package, or supply --executable /absolute/path/to/Optimus.
# Defaults to a read-only status scan. Apply respects saved settings/permissions.
set -euo pipefail

usage() {
    printf '%s\n' 'Usage: speed-enhancements.sh [--executable PATH] [--mode status|apply|history | --restore RUN_ID] [--output DIRECTORY]'
}
fail() { printf '%s\n' "Optimus speed enhancements: $*" >&2; exit 2; }

mode='status'
selected=''
restore=''
executable=''
output=''
has_output=0
has_executable=0
while (( $# )); do
    case "$1" in
        --mode)
            (( $# >= 2 )) || fail '--mode requires a value.'
            [[ -z "$selected" ]] || fail 'Choose one mode or restore operation.'
            case "$2" in status|apply|history) mode="$2";; *) fail 'Mode must be status, apply or history.';; esac
            selected='mode'; shift 2;;
        --restore)
            (( $# >= 2 )) || fail '--restore requires one exact run ID.'
            [[ -z "$selected" ]] || fail 'Choose one mode or restore operation.'
            [[ "$2" =~ ^[0-9a-f]{32}$ ]] || fail 'Restore requires 32 lowercase hexadecimal characters.'
            restore="$2"; selected='restore'; shift 2;;
        --executable)
            (( $# >= 2 )) || fail '--executable requires a path.'
            (( has_executable == 0 )) || fail 'Supply --executable once.'
            [[ -n "$2" ]] || fail '--executable requires a path.'
            executable="$2"; has_executable=1; shift 2;;
        --output)
            (( $# >= 2 )) || fail '--output requires a directory.'
            (( has_output == 0 )) || fail 'Supply --output once.'
            [[ -n "$2" ]] || fail '--output requires a directory.'
            output="$2"; has_output=1; shift 2;;
        --help|-h) usage; exit 0;;
        *) fail "Unknown argument: $1";;
    esac
done
if (( has_executable == 0 )); then
    [[ ! -L "${BASH_SOURCE[0]}" ]] || fail 'Use an ordinary package script or supply --executable.'
    script_directory="$(cd -P -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
    [[ "$(basename -- "$(dirname -- "$script_directory")")" == 'source' ]] || fail 'Source checkouts must supply the installed Optimus executable using --executable.'
    package_root="$(cd -P -- "$script_directory/../.." && pwd)"
    if [[ "$(basename -- "$package_root")" == 'Resources' && "$(basename -- "$(dirname -- "$package_root")")" == 'Contents' ]]; then
        executable="$package_root/../MacOS/Optimus"
    else
        executable="$package_root/Optimus"
    fi
fi
[[ -f "$executable" && -x "$executable" && ! -L "$executable" ]] || fail 'Supply an ordinary installed Optimus executable using --executable.'
executable_directory="$(cd -P -- "$(dirname -- "$executable")" && pwd)"
executable="$executable_directory/$(basename -- "$executable")"
arguments=(--speed-enhancements "$mode")
if [[ "$selected" == 'restore' ]]; then arguments=(--speed-restore "$restore"); fi
if (( has_output )); then arguments+=(--output "$output"); fi
exec "$executable" "${arguments[@]}"
