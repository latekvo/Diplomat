---
name: argent-device-allocator
description: Reserve and manage exclusive Android emulators / iOS simulators through the local device-allocator MCP server. Use whenever you are about to boot, launch, install onto, screenshot, or interact with any simulator or emulator, or when doing on-device / E2E / UI testing, so you never collide with another agent's device.
---

# Device allocator

Multiple agents share this machine's simulators and emulators. If two agents use
the same device at once, their taps and navigation interleave and both sessions
are corrupted. The device allocator hands each agent a device for its **exclusive**
use and reclaims it automatically when the agent finishes or dies.

## The rule

**Before booting, running, installing onto, or interacting with ANY device, call
`request-device` first.** Use only the device id it returns. Never pick, boot, or
touch a simulator/emulator you were not allocated.

## The tools (argent-device-allocator MCP server)

- **`request-device`** — ask for a device. Optional `platform` (`android` | `ios` |
  `any`), optional `version` (`"18"`/`"18.5"` for iOS; `"14"` or API level `"34"`
  for Android), optional `agentName` (a short label shown in the Argent Utils panel).
  Returns a device id: a **UDID** for iOS (use with `xcrun simctl … <udid>`), or an
  **adb serial** like `emulator-5554` for Android (use with `adb -s <serial> …`).
- **`free-device`** — you're done; releases the device and shuts it down. Call this
  as soon as you finish. Good hygiene keeps the pool available.
- **`change-device`** — release your current device and get a different one in one
  step (e.g. you now need the other platform or another OS version).
- **`report-device-broken`** — your device won't boot or is misbehaving. It is
  pulled from the pool, a repair is dispatched automatically, and you are handed a
  different device immediately. Don't waste time fighting a broken device.

## Typical flow

1. `request-device { platform: "ios", version: "18", agentName: "bluesky e2e" }`
   → `{ deviceId: "99AD1D87-…", platform: "ios", … }`
2. Drive the app on **that** device only: `xcrun simctl … 99AD1D87-…`
   (or `adb -s emulator-5554 …` for Android).
3. If it won't boot: `report-device-broken { reason: "boot timed out" }` → use the
   replacement you're given.
4. When finished: `free-device`.

You never need to enumerate devices yourself or guess which one is free — that's the
allocator's job. Just request, use the id you're given, and free.
