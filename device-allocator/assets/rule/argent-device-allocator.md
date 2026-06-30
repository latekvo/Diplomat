---
description: Argent device allocator — always-on rule. Forces every agent to reserve a simulator/emulator before using it, so concurrent agents never collide on a shared device.
alwaysApply: true
---

# Device allocation is mandatory

This machine's iOS simulators and Android emulators are a **shared, arbitrated
pool**. Two agents on one device corrupt each other's session (interleaved taps,
random navigation, wasted debugging). To prevent that:

**Before you boot, run, install onto, screenshot, or otherwise interact with ANY
iOS simulator or Android emulator, you MUST first call `request-device`** (from the
`argent-device-allocator` MCP server) and then use **only** the device id it returns.

- Never select, boot, or touch a simulator/emulator you were not allocated.
- `request-device` → get an exclusive device (UDID for iOS, adb serial for Android).
- `free-device` → release it (and shut it down) the moment you are done.
- `change-device` → swap for a different platform/version in one step.
- `report-device-broken` → if it won't boot or misbehaves; you get a replacement and
  a repair is dispatched automatically. Do not keep fighting a broken device.

This is not optional and applies to every device interaction, every time. If you are
about to type `xcrun simctl boot`, `adb -s …`, `open -a Simulator`, or launch an app
on a device and you have **not** been allocated that exact device id, stop and call
`request-device` first.
