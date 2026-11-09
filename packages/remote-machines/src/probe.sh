# Prints one machine's load (and, with `full`, its capabilities) as key=value lines
# between markers, so whatever a login shell prints around it is ignored. POSIX sh,
# fed over `ssh <host> sh -s -- <mode>` and run locally the same way: nothing is
# installed on the machine it describes, and it changes nothing there.
mode=${1:-load}
os=$(uname -s)
echo __RM_BEGIN__
echo "os=$os"
echo "hostname=$(hostname 2>/dev/null)"
echo "home=$HOME"
echo "conn=$SSH_CONNECTION"
df -Pk "$HOME" 2>/dev/null | awk 'NR==2 {print "disk_kb=" $2 " " $4}'
# Authenticated SSH sessions: sshd names each one's process user@<tty> or user@notty.
# Over ssh, this probe's own session is one of them.
ps_out=$(ps -A -o args= 2>/dev/null) && ssh_n=$(printf '%s\n' "$ps_out" | grep -cE '^sshd(-session)?: [^ ]+@')
[ -n "$ssh_n" ] && [ -n "$SSH_CONNECTION" ] && [ "$ssh_n" -gt 0 ] && ssh_n=$((ssh_n - 1))
echo "ssh_sessions=$ssh_n"
if [ "$os" = Darwin ]; then
  echo "loadavg=$(sysctl -n vm.loadavg | tr -d '{}')"
  echo "ncpu=$(sysctl -n hw.ncpu)"
  echo "mem_bytes=$(sysctl -n hw.memsize)"
  echo "mem_free_pct=$(sysctl -n kern.memorystatus_level 2>/dev/null)"
  echo "mem_pressure=$(sysctl -n kern.memorystatus_vm_pressure_level 2>/dev/null)"
  echo "swap=$(sysctl -n vm.swapusage)"
  echo "boottime=$(sysctl -n kern.boottime | sed 's/^{ sec = \([0-9]*\).*/\1/')"
  echo "machine_id=$(ioreg -rd1 -c IOPlatformExpertDevice | awk -F'"' '/IOPlatformUUID/ {print $4}')"
else
  echo "loadavg=$(cut -d' ' -f1-3 /proc/loadavg)"
  echo "ncpu=$(nproc 2>/dev/null || getconf _NPROCESSORS_ONLN)"
  awk '/^(MemTotal|MemAvailable|SwapTotal|SwapFree):/ {sub(":", "", $1); print "meminfo_" $1 "=" $2}' /proc/meminfo
  echo "boottime=$(awk '/^btime/ {print $2}' /proc/stat)"
  echo "machine_id=$(cat /etc/machine-id /var/lib/dbus/machine-id 2>/dev/null | head -1)"
  # nvidia-smi prints its failures (a driver/library mismatch, no devices) on stdout.
  nvidia=$(nvidia-smi --query-gpu=name,memory.total,memory.used,utilization.gpu --format=csv,noheader,nounits 2>/dev/null) ||
    nvidia=
  [ -n "$nvidia" ] && printf '%s\n' "$nvidia" | sed 's/^/nvidia=/'
fi
if [ "$mode" = full ]; then
  echo "arch=$(uname -m)"
  echo "kernel=$(uname -r)"
  if [ "$os" = Darwin ]; then
    echo "os_version=macOS $(sw_vers -productVersion)"
    echo "cpu=$(sysctl -n machdep.cpu.brand_string)"
    echo "perf_cores=$(sysctl -n hw.perflevel0.physicalcpu 2>/dev/null)"
    system_profiler SPDisplaysDataType 2>/dev/null | awk -F': ' '
      /Chipset Model:/ {if (g) print "gpu=" g; g=$2}
      /Total Number of Cores:/ {g=g " (" $2 "-core)"}
      /VRAM/ {g=g ", " $2 " VRAM"}
      END {if (g) print "gpu=" g}'
    echo "xcode=$(xcodebuild -version 2>/dev/null | head -1)"
  else
    echo "os_version=$( . /etc/os-release 2>/dev/null && echo "$PRETTY_NAME")"
    # ARM kernels have no "model name" in /proc/cpuinfo; lscpu names the core instead.
    cpu=$(awk -F': ' '/^model name/ {print $2; exit}' /proc/cpuinfo)
    [ -n "$cpu" ] || cpu=$(lscpu 2>/dev/null | awk -F': *' '/^Model name/ && $2 != "-" {print $2; exit}')
    echo "cpu=$cpu"
    [ -n "$nvidia" ] ||
      lspci 2>/dev/null | awk -F': ' '/VGA|3D controller/ {print "gpu=" $2}'
  fi
  # Toolchains on the login shell's PATH, the one agents are told to run work under
  # (`ssh host '$SHELL -l -s'`); a plain `ssh host cmd` PATH misses Homebrew.
  lpath=$("${SHELL:-/bin/sh}" -lc 'printf "\n%s" "$PATH"' </dev/null 2>/dev/null | tail -1)
  [ -n "$lpath" ] && PATH=$lpath
  for t in git gh node npm pnpm yarn bun python3 uv docker podman swift cargo go java ffmpeg tart claude; do
    p=$(command -v "$t" 2>/dev/null) && echo "tool=$t $p"
  done
  command -v node >/dev/null 2>&1 && echo "node_version=$(node --version 2>/dev/null)"
fi
echo __RM_END__
