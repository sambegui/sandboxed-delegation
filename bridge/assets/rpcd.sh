#!/bin/sh
# In-sandbox rpc daemon: omp --mode rpc whose stdin is a FIFO held open read-write, so it never sees EOF.
# usage: rpcd.sh <dir> <omp args...>   (start detached: setsid nohup sh rpcd.sh ... &)
D=$1
shift
mkdir -p "$D"
rm -f "$D/in"
mkfifo "$D/in"
exec 3<>"$D/in"
umask 002
echo $$ >"$D/pid"
exec env HOME=/sandbox/.home PI_CODING_AGENT_DIR=/sandbox/.omp-agent PI_NO_TITLE=1 \
  omp --mode rpc --no-ui --yolo --no-title "$@" <&3 >>"$D/out.jsonl" 2>>"$D/err.log"
