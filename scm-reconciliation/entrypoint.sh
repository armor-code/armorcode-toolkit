#!/bin/sh
# Pass all arguments to the Python script
# Logs go to stdout + tempdir/armorcode/log via logging module
exec python -W ignore main.py "$@"
