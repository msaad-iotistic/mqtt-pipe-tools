#!/bin/bash
set -e

if [ "$#" -ne 2 ]; then
  echo "Usage: $0 <cmd1> <cmd2>"
  exit 1
fi

CMD1="$1"; CMD2="$2"
TMP=$(mktemp -d); mkfifo "$TMP/f1" "$TMP/f2"

# Open both ends so they never block
exec 3<>"$TMP/f1"   # FD3 is read+write on f1
exec 4<>"$TMP/f2"   # FD4 is read+write on f2

# Launch both, redirecting to the right FDs
bash -c "$CMD1" <&3 >&4 &
PID1=$!

bash -c "$CMD2" <&4 >&3 &
PID2=$!

trap "kill $PID1 $PID2; rm -rf '$TMP'" EXIT
wait $PID1 $PID2
