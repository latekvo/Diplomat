---
description: Offload artifact-producing work (builds, tests, renders, packages) to the remote machines the diplomat-remote-machines MCP server lists.
alwaysApply: true
---

# Offload artifact-producing work to remote machines

This machine is shared by many agents at once, and other machines on the network are
reachable over SSH. The `diplomat-remote-machines` MCP server keeps a live table of them:
what each can do (OS, CPU, GPU, RAM, toolchains) and how loaded it is right now.

**For any task that produces an artifact** - a build, a compiled binary or app bundle, a
test or lint run, a package, a container image, a render or video, generated docs, a
dataset, a benchmark - **prefer running it on a remote machine**:

1. Call `list-machines` first. Pick the first machine without warnings that has the
   toolchain and enough spare cores, RAM and disk for the job.
2. Copy the inputs there: `ssh <target> mkdir -p offload/<task>`, then
   `rsync -az --exclude node_modules --exclude .git <dir>/ <target>:offload/<task>/`,
   or clone/fetch work you have pushed.
3. Run it through a login shell. A plain `ssh <target> <command>` is not one, so its PATH
   lacks Homebrew and the other toolchains `list-machines` reports:
   ```bash
   ssh <target> '$SHELL -l -s' <<'EOF'
   cd offload/<task> && <command>
   EOF
   ```
   The script is the shell's stdin, so give any command that reads stdin (`ssh`,
   `docker run -i`, `read`) `</dev/null`, or it swallows the lines after it.
4. Copy back only the artifacts and results you need, then `ssh <target> 'rm -rf offload/<task>'`.

Run it on this machine instead when `list-machines` shows no usable machine, when the work
needs something only this machine has (its simulators and devices, its GUI, its local
services, credentials that must not leave it), or when the remote lacks the toolchain.

Never copy secrets (`.env` files, keys, tokens) to a remote, never install system-wide
software there without being asked, and leave nothing running there when you finish.
