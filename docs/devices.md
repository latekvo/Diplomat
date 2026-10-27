# Devices

The screen for the [device allocator](../packages/device-allocator/README.md), the MCP server that
gives each agent a simulator or emulator of its own. Open it with the phone button in the panel
header (its badge counts the devices in use) or the **Devices** header in the left column.

## Devices

Every device the allocator knows, **In use** first, then **Free**. Each row shows the platform,
name, version and form factor, where the device comes from (**local**, or **remote** and the
pool's name), its state, the agent holding it, when it was taken and how long its screen has been
still, and its handle (UDID, adb serial, or the remote device's local port).

- **Free** hands a local device back to the pool; **Kill** shuts it down as well.
- **Repaired** appears while a repair agent is working on a device, and puts it back in service.
- **Return** gives a remote device back to its pool.
- Clicking the holder raises its terminal (macOS).

With the daemon not running the list is its last snapshot, and says so. **Recent** under it is
the allocator's part of the activity feed: frees, kills, repairs, remote leases a pool took
back, and installs, uninstalls, starts and restarts of the allocator, failed ones included.

## Allocator

The daemon (running or not, its pid and socket), the same install controls as **Settings → Device
allocator (MCP)**, **Start daemon** (an agent's first request would also start it), and the two
idle limits, shared with Settings.

**Restart daemon**, while one runs, stops it and starts this checkout's. A daemon never exits by
itself, and **Reinstall** / **Update** leave a running one alone, so after updating the checkout
this is how new daemon code takes over. Agents keep their leases (the new daemon reloads them),
but a simulator or emulator the allocator booted is shut down as the old daemon exits.

## Remote pool

Real devices somewhere else, lent to this machine's agents before its own simulators and
emulators are booted. Diplomat knows nothing about any particular pool: a **provider module** you
point it at does the talking (see the [allocator's README](../packages/device-allocator/README.md)).
A daemon from before remote pools reports none: the card then says so and offers **Restart
daemon**, after updating the checkout if it has no remote pool either.

- **Provider module** - the module's path (`~` allowed). Blank means no remote pool. It is
  `deviceRemoteProvider` in `~/.diplomat/config.json`, which the daemon re-reads on every request
  and the screen while it is open; `DA_REMOTE_PROVIDER` overrides it. A change that cannot be
  written there says so on the screen.
- The status block shows the provider, its state (*on*, *signed out*, *signing in*, *error*, ...),
  and whatever else it reports: a sign-in link while signing in, and, after a sign-in nobody
  answered, the time until which it will not ask again.
- **Sign in** opens the pool's own sign-in and waits for it, up to ten minutes, in the background.
- **List pool devices** shows what the pool lends right now. It never prompts a sign-in.
- **Leased here** lists the remote devices this machine's agents hold.
