const assert = require("node:assert/strict");
const test = require("node:test");

const { BackendClient, redactText } = require("../backend-client");

test("backend client carries the random token without exposing it in the URL", async () => {
  const seen = [];
  const client = new BackendClient({
    port: 43210,
    token: "desktop-secret-token",
    transport: async (options, body) => { seen.push([options, body]); return { statusCode: 200, body: '{"ok":true,"pending_review_count":3}' }; },
  });
  const result = await client.getStudio();
  assert.equal(result.pending_review_count, 3);
  assert.equal(seen[0][0].path, "/api/studio");
  assert.equal(seen[0][0].headers.Authorization, "Bearer desktop-secret-token");
  assert.equal(seen[0][0].path.includes("desktop-secret-token"), false);
});

test("backend errors and logs never echo the authentication token", async () => {
  const token = "do-not-log-this-token";
  const client = new BackendClient({
    port: 43210,
    token,
    transport: async () => ({ statusCode: 500, body: JSON.stringify({ error: token }) }),
  });
  await assert.rejects(client.getStudio(), (error) => {
    assert.equal(error.message.includes(token), false);
    assert.match(error.message, /HTTP 500/);
    return true;
  });
  assert.equal(redactText(`before ${token} after`, [token]), "before [REDACTED] after");
});

test("readiness requires both the stable shell endpoint and project database", async () => {
  const paths = [];
  const client = new BackendClient({
    port: 43210,
    token: "token",
    transport: async (options) => { paths.push(options.path); return { statusCode: 200, body: '{"ok":true,"pending_review_count":0}' }; },
  });
  await client.checkReady();
  assert.deepEqual(paths, ["/api/onboarding", "/api/studio"]);
});

test("readiness retries transient failures and stops if the backend exits", async () => {
  const client = new BackendClient({ port: 43210, token: "token", transport: async () => ({ statusCode: 200, body: "{}" }) });
  let attempts = 0;
  let sleeps = 0;
  client.checkReady = async () => {
    attempts += 1;
    if (attempts < 3) throw new Error("not ready");
  };
  await client.waitUntilReady({ sleep: async () => { sleeps += 1; } });
  assert.equal(attempts, 3);
  assert.equal(sleeps, 2);

  await assert.rejects(
    client.waitUntilReady({ isAlive: () => false }),
    /后台服务在启动期间退出/,
  );
});

test("desktop output and file selection calls encode IDs and keep selected paths in the request body", async () => {
  const seen = [];
  const client = new BackendClient({
    port: 43210,
    token: "token",
    transport: async (options, body) => {
      seen.push([options, body]);
      if (options.method === "POST") {
        return { statusCode: 201, body: '{"selection_token":"grant-1","expires_in_seconds":300}' };
      }
      return { statusCode: 200, body: '{"path":"/private/output/clip.mp4"}' };
    },
  });

  await client.resolveOutputPath("output/with space");
  await client.registerFileSelection("issue/with space", "source", "/private/source.mp4");

  assert.equal(seen[0][0].path, "/api/desktop/outputs/output%2Fwith%20space/path");
  assert.equal(seen[1][0].path, "/api/desktop/file-selections");
  assert.deepEqual(JSON.parse(seen[1][1]), {
    issue_id: "issue/with space",
    kind: "source",
    selected_path: "/private/source.mp4",
  });
  assert.equal(seen[1][0].path.includes("private/source"), false);
});
