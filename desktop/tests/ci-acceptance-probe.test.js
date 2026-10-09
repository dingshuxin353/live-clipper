const assert = require('node:assert/strict');
const test = require('node:test');

test('CI acceptance rejects a failing desktop test', () => {
  assert.equal(true, false);
});
