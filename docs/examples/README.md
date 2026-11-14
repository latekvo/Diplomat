# Examples

## Remote device pool providers

A provider module lends the [device allocator](../../packages/device-allocator/README.md#a-remote-device-pool)
real devices from somewhere else. Its contract is in the header of
[`src/remote.js`](../../packages/device-allocator/src/remote.js).

- [`agfleet/provider.mjs`](agfleet/provider.mjs) - [Argent Fleet](https://agfleet.swmtest.xyz/llms.txt):
  Android phones leased over its REST API and tunnelled to a local adb port, with its browser
  sign-in shared with the `agfleet` CLI. [`agfleet/test/`](agfleet/test) drives it through the
  real allocator daemon against a fake farm: `node docs/examples/agfleet/test/provider.test.mjs`.

To use one, point **Devices → Remote pool → Provider module** at it.
