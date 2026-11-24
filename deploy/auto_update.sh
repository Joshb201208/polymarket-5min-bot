#!/bin/bash
set -eu
script_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
# Python loads the entire updater before it changes the checkout it came from.
exec /usr/bin/python3 "$script_dir/update_release.py" "$script_dir/.."
