---
name: diplomat-remote-machines
description: Offload builds, test runs, renders and any other artifact-producing work to the machines on the local network that the diplomat-remote-machines MCP server lists. Use before running a build, test suite, bundler, compiler, renderer, container build or benchmark, or when this machine is slow, hot or out of memory.
---

# Offloading work to remote machines

**This machine is shared by many agents at once.** Anything that produces an artifact - a
build, a binary or app bundle, a test or lint run, a package, a container image, a render
or video, generated docs, a dataset, a benchmark - should run on a remote machine when one
is usable.

## 1. Pick a machine

Call `list-machines`. It lists this machine first, for comparison, then the remote
machines best first, each with:

- what it is: OS, CPU and cores, GPU, RAM, the toolchains on its login PATH, its home dir;
- how loaded it is: load average now, over 5 minutes and since the last scan, spare
  cores, available RAM, swap, free disk;
- `WARNING:` lines for what makes it a poor choice right now (overloaded, memory
  pressure, swap nearly full on Linux, low disk).

Take the first machine without warnings that has the toolchain your job needs and the
disk for its inputs and outputs. Load is re-read every minute; pass `refresh: true` only
when you need a fresh read of everything (it takes a few seconds).

## 2. Run the work there

```bash
T=radoniusz                      # the target list-machines printed
D=offload/my-task                # one directory per task, under the remote home
ssh "$T" mkdir -p "$D"           # GNU rsync creates only the last directory level
rsync -az --delete --exclude node_modules --exclude .git ./ "$T:$D/"
ssh "$T" '$SHELL -l -s' <<EOF
cd $D && npm ci && npm run build
EOF
rsync -az "$T:$D/dist/" ./dist/  # bring back only what you need
ssh "$T" "rm -rf $D"
```

- Run commands through a login shell (`'$SHELL -l -s'`, fed on stdin as above). A plain
  `ssh <target> <command>` is not one: its PATH lacks Homebrew and the other toolchains
  `list-machines` reports. Quote the heredoc marker (`<<'EOF'`) when the commands should
  expand `$VARS` on the remote rather than here. The script is the shell's stdin, so a
  command in it that reads stdin (`ssh`, `docker run -i`, `read`) needs `</dev/null`, or it
  swallows the lines after it and the run still exits 0.
- A machine on a port other than 22 is printed with the `ssh -p` and `rsync -e` forms to
  use for it.
- Pushed work can be cloned or fetched there instead of rsynced.
- Bound long commands yourself, and leave nothing running when you are done.

## 3. When to stay on this machine

- `list-machines` lists no machine, or only ones with warnings that matter for this job.
- The work needs something only this machine has: its simulators and emulators (those go
  through `request-device`), its GUI, its local services, credentials that must not leave it.
- The remote lacks the toolchain and installing it is not reasonable.

## Rules

- Never copy secrets (`.env` files, keys, tokens, credentials) to a remote.
- Never install system-wide software on a remote without being asked.
- A machine listed as "host key never accepted" or "SSH refused our keys" is not yours to
  use; do not try to get around that.
