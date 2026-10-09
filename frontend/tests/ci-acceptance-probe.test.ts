import { expect, test } from 'vitest';

test('CI acceptance rejects a failing frontend test', () => {
  expect(true).toBe(false);
});
