import { fileURLToPath } from 'node:url';

// The device allocator whose daemon these tests drive: this checkout's, or
// DIPLOMAT_ALLOCATOR_DIR's for a copy kept outside it.
export const ALLOCATOR = process.env.DIPLOMAT_ALLOCATOR_DIR
  || fileURLToPath(new URL('../../../../../packages/device-allocator', import.meta.url));
