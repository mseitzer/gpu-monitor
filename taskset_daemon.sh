DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" >/dev/null 2>&1 && pwd )"
script=python $DIR/gpu_monitor.py -v -t

cmd="watch -n 300 "
$cmd $script

