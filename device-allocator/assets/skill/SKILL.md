---
name: diplomat-device-allocator
description: Reserve and manage exclusive Android emulators / iOS simulators / Apple TV / Android TV / Vega devices through the local device-allocator MCP server. Use whenever you are about to boot, launch, install onto, screenshot, create, or interact with any simulator or emulator, or when doing on-device / E2E / UI testing, so you never collide with another agent's device.
---

# Device allocator

**If you can see this skill or the `diplomat-device-allocator` MCP server, there are — or
may at any moment be — MULTIPLE agents on this machine. You are NEVER the only one.**
Two agents on one device corrupt each other's session, so each agent gets a device for
its **exclusive** use and the allocator reclaims it when the agent finishes or dies.

## The rule

**Before booting, running, installing onto, screenshotting, creating, or interacting
with ANY device, call `request-device` first.** Use only the device id it returns.
Never pick, boot, create, or squat a device you were not allocated.

## The tools

- **`request-device`** — ask for a device. Give `platform` (`ios`, `android`,
  `apple-tv`, `android-tv`, `vega`) and optionally `format` (`phone`/`tablet`, iOS/Android
  only). Set `version` **only when a specific OS version is genuinely required** —
  otherwise omit it. Returns a UDID (Apple) or adb serial (Android). Two special
  responses:
  - **exhausted** — the concurrency quota is reached and other agents hold every slot.
    Call `await-device`, then request again. Do **not** create your own device to bypass it.
  - **needs-create** — no matching device exists (there is no fixed pool). Create one to
    spec yourself, then call `request-device` again with `deviceId` set to the new
    device's id to claim it.
- **`await-device`** — blocks until a device slot frees when the pool was exhausted, then
  tells you to call `request-device` again.
- **`free-device`** — you're done; releases the device (and shuts it down if the
  allocator booted it). Call it as soon as
  you finish.
- **`change-device`** — release your current device and get a different one (platform /
  format / version) in one step.
- **`report-device-broken`** — your device won't boot or misbehaves. It's pulled from the
  pool, a repair is dispatched, and you're handed a different device immediately.

## Typical flow

1. `request-device { platform: "ios", format: "phone", agentName: "bluesky e2e" }`
   → `{ outcome: "allocated", deviceId: "99AD1D87-…", … }`
2. Drive the app on **that** device only (`xcrun simctl … 99AD1D87-…`, or
   `adb -s emulator-5554 …` for Android).
3. If exhausted → `await-device` → then `request-device` again.
4. If needs-create → create the device (`xcrun simctl create` / `avdmanager create avd` /
   an argent setup skill) → `request-device { …, deviceId: "<new id>" }`.
5. When finished: `free-device`.

You never assume you're alone and never touch a device you weren't handed. Request, use
the id you're given, and free.
