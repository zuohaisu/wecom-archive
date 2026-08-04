#!/usr/bin/env node
/* RND-239 focused browser smoke: Demo theme state must be DOM-only. */
import assert from "node:assert/strict";
import { existsSync, mkdtempSync, readFileSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { resolve } from "node:path";
import { spawn } from "node:child_process";
import net from "node:net";

const repoRoot = resolve(import.meta.dirname, "..");
const siteRoot = resolve(repoRoot, "static_site/company_homepage");
const shellPath = resolve(siteRoot, "demo/assets/demo-shell.js");
const chromePath = process.env.CHROME_BIN || "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome";
const pages = [
  "index.html",
  "conversations.html",
  "search.html",
  "contacts.html",
  "media.html",
  "analytics.html",
  "audit-log.html"
];
const resources = [
  "/",
  "/demo/assets/demo.css",
  "/demo/assets/demo-data.js",
  "/demo/assets/demo-shell.js",
  "/brand/logo-icon.svg",
  "/assets/console_review.png"
];

function sleep(milliseconds) {
  return new Promise((resolvePromise) => setTimeout(resolvePromise, milliseconds));
}

async function waitFor(check, description, timeout = 8000) {
  const deadline = Date.now() + timeout;
  let lastError;
  while (Date.now() < deadline) {
    try {
      if (await check()) return;
    } catch (error) {
      lastError = error;
    }
    await sleep(80);
  }
  throw new Error(`${description}${lastError ? `: ${lastError.message}` : ""}`);
}

function findOpenPort() {
  return new Promise((resolvePromise, reject) => {
    const server = net.createServer();
    server.once("error", reject);
    server.listen(0, "127.0.0.1", () => {
      const { port } = server.address();
      server.close((error) => error ? reject(error) : resolvePromise(port));
    });
  });
}

async function connectCdp(debugUrl, requests, browserErrors) {
  const targets = await (await fetch(`${debugUrl}/json/list`)).json();
  const target = targets.find((item) => item.type === "page");
  assert.ok(target, "Chrome must expose a page target");

  const socket = new WebSocket(target.webSocketDebuggerUrl);
  const pending = new Map();
  let commandId = 0;

  socket.onmessage = (event) => {
    const message = JSON.parse(event.data);
    if (message.method === "Network.requestWillBeSent") {
      requests.push({ method: message.params.request.method, url: message.params.request.url });
    }
    if (message.method === "Runtime.exceptionThrown") {
      browserErrors.push(message.params.exceptionDetails.text || "Uncaught page exception");
    }
    if (message.method === "Log.entryAdded" && message.params.entry.level === "error") {
      browserErrors.push(message.params.entry.text || "Browser log error");
    }
    if (!message.id || !pending.has(message.id)) return;
    const { resolvePromise, reject } = pending.get(message.id);
    pending.delete(message.id);
    if (message.error) reject(new Error(`${message.error.message} (${message.error.code})`));
    else resolvePromise(message.result || {});
  };

  await new Promise((resolvePromise, reject) => {
    socket.onopen = resolvePromise;
    socket.onerror = () => reject(new Error("Could not connect to Chrome DevTools"));
  });

  return {
    async call(method, params = {}) {
      return new Promise((resolvePromise, reject) => {
        const id = ++commandId;
        pending.set(id, { resolvePromise, reject });
        socket.send(JSON.stringify({ id, method, params }));
      });
    },
    close() {
      socket.close();
    }
  };
}

function themeStateExpression() {
  return `(() => {
    const button = document.querySelector("[data-demo-theme-toggle]");
    return JSON.stringify({
      theme: document.documentElement.getAttribute("data-demo-theme"),
      text: button && button.textContent,
      pressed: button && button.getAttribute("aria-pressed"),
      label: button && button.getAttribute("aria-label")
    });
  })()`;
}

async function main() {
  const shellSource = readFileSync(shellPath, "utf8");
  for (const forbidden of ["localStorage", "sessionStorage", "document.cookie", "indexedDB"]) {
    assert.ok(!shellSource.includes(forbidden), `Demo Shell must not reference ${forbidden}`);
  }
  assert.match(shellSource, /data-demo-theme-toggle/, "Demo Shell must retain its theme toggle");
  assert.match(shellSource, /applyTheme\(next\)/, "Theme toggle must update the current page DOM");

  if (!existsSync(chromePath)) {
    throw new Error(`Chrome is required for this focused smoke test; not found at ${chromePath}`);
  }

  const port = await findOpenPort();
  const debugPort = await findOpenPort();
  const baseUrl = `http://127.0.0.1:${port}`;
  const debugUrl = `http://127.0.0.1:${debugPort}`;
  const profile = mkdtempSync(resolve(tmpdir(), "rnd239-theme-smoke-"));
  let server;
  let chrome;
  let cdp;

  try {
    server = spawn("python3", ["-m", "http.server", String(port), "--directory", siteRoot], { stdio: "ignore" });
    await waitFor(async () => (await fetch(`${baseUrl}/`)).status === 200, "Static HTTP server did not start");

    for (const path of resources) {
      const response = await fetch(`${baseUrl}${path}`);
      assert.equal(response.status, 200, `${path} must return HTTP 200`);
    }
    const homepage = await (await fetch(`${baseUrl}/`)).text();
    assert.match(homepage, /href="demo\//, "Homepage must link to the Demo");
    assert.match(homepage, /id="pricing"/, "Homepage pricing anchor must remain available");
    for (const page of pages) {
      const response = await fetch(`${baseUrl}/demo/${page}`);
      assert.equal(response.status, 200, `/demo/${page} must return HTTP 200`);
      const markup = await response.text();
      assert.match(markup, /data-demo-nav/, `${page} must retain the shared Demo navigation host`);
      assert.match(markup, /data-demo-topbar/, `${page} must retain the shared Demo topbar host`);
      assert.match(markup, /assets\/demo-shell\.js/, `${page} must load the shared Demo Shell`);
    }

    chrome = spawn(chromePath, [
      "--headless=new",
      "--disable-gpu",
      "--no-first-run",
      "--disable-background-networking",
      "--disable-component-update",
      "--disable-sync",
      "--remote-debugging-address=127.0.0.1",
      `--remote-debugging-port=${debugPort}`,
      `--user-data-dir=${profile}`,
      "about:blank"
    ], { stdio: "ignore" });
    await waitFor(async () => (await fetch(`${debugUrl}/json/list`)).ok, "Chrome DevTools did not start");

    const requests = [];
    const browserErrors = [];
    cdp = await connectCdp(debugUrl, requests, browserErrors);
    await cdp.call("Page.enable");
    await cdp.call("Runtime.enable");
    await cdp.call("Network.enable");
    await cdp.call("Log.enable");
    const evaluate = async (expression) => {
      const result = await cdp.call("Runtime.evaluate", { expression, returnByValue: true });
      if (result.exceptionDetails) throw new Error(result.exceptionDetails.text || "Page evaluation failed");
      return result.result && result.result.value;
    };
    const waitForShell = () => waitFor(async () => Boolean(await evaluate("document.querySelector('[data-demo-theme-toggle]')")), "Demo Shell did not render");
    const persistenceCalls = async () => JSON.parse(await evaluate("JSON.stringify(window.__rnd239PersistenceCalls)"));
    const currentThemeState = async () => JSON.parse(await evaluate(themeStateExpression()));

    // Seed the legacy key before instrumentation: a compliant Shell must ignore it.
    await cdp.call("Page.navigate", { url: `${baseUrl}/` });
    await waitFor(async () => Boolean(await evaluate("document.body")), "Homepage did not load");
    await evaluate("window.localStorage.setItem('crowntime-static-demo-theme', 'dark')");
    await cdp.call("Page.addScriptToEvaluateOnNewDocument", { source: `(() => {
      const calls = [];
      Object.defineProperty(window, "__rnd239PersistenceCalls", { value: calls });
      const wrap = (target, method, kind) => {
        if (!target || typeof target[method] !== "function") return;
        const original = target[method];
        Object.defineProperty(target, method, {
          configurable: true,
          value: function () {
            calls.push(kind + "." + method);
            return original.apply(this, arguments);
          }
        });
      };
      ["getItem", "setItem", "removeItem", "clear", "key"].forEach((method) => wrap(window.Storage && Storage.prototype, method, "storage"));
      const cookie = Object.getOwnPropertyDescriptor(Document.prototype, "cookie");
      if (cookie && cookie.set) {
        Object.defineProperty(Document.prototype, "cookie", {
          configurable: true,
          get: cookie.get,
          set: function (value) {
            calls.push("cookie.set");
            return cookie.set.call(this, value);
          }
        });
      }
      ["open", "deleteDatabase", "databases"].forEach((method) => wrap(window.indexedDB, method, "indexedDB"));
    })()` });

    await cdp.call("Page.navigate", { url: `${baseUrl}/demo/conversations.html` });
    await waitForShell();
    assert.deepEqual(await currentThemeState(), {
      theme: "light", text: "深色", pressed: "false", label: "切换为深色主题"
    }, "Demo must begin in its default light theme despite a legacy stored value");
    assert.deepEqual(await persistenceCalls(), [], "Demo must not read persistent browser storage during initialization");

    await evaluate("document.querySelector('[data-demo-theme-toggle]').click()");
    assert.deepEqual(await currentThemeState(), {
      theme: "dark", text: "浅色", pressed: "true", label: "切换为浅色主题"
    }, "Theme button must update the current DOM and accessibility state");
    assert.deepEqual(await persistenceCalls(), [], "Theme changes must not write persistent browser storage");

    await cdp.call("Page.reload", { ignoreCache: true });
    await waitForShell();
    assert.deepEqual(await currentThemeState(), {
      theme: "light", text: "深色", pressed: "false", label: "切换为深色主题"
    }, "Reload must restore the default light theme without reading saved state");
    assert.deepEqual(await persistenceCalls(), [], "Reload must not read persistent browser storage");

    for (const page of pages) {
      await cdp.call("Page.navigate", { url: `${baseUrl}/demo/${page}` });
      await waitForShell();
      const navHrefs = JSON.parse(await evaluate("JSON.stringify(Array.from(document.querySelectorAll('.demo-nav-item')).map((link) => link.getAttribute('href')))"));
      assert.deepEqual(navHrefs, pages, `${page} must render all shared Demo navigation links`);
      assert.equal(await evaluate("document.documentElement.getAttribute('data-demo-theme')"), "light", `${page} must default to light theme`);
      assert.deepEqual(await persistenceCalls(), [], `${page} must not access persistent browser storage`);
    }

    const nonGetRequests = requests.filter((request) => request.method !== "GET");
    const apiRequests = requests.filter((request) => /\/api\//.test(request.url));
    assert.deepEqual(nonGetRequests, [], "Demo must not issue non-GET requests");
    assert.deepEqual(apiRequests, [], "Demo must not request an API");
    assert.deepEqual(browserErrors, [], "Demo must not emit browser console or runtime errors");
    console.log("RND-239 theme persistence smoke: PASS");
  } finally {
    cdp?.close();
    chrome?.kill("SIGTERM");
    server?.kill("SIGTERM");
    await sleep(300);
    rmSync(profile, { recursive: true, force: true, maxRetries: 3 });
  }
}

main().catch((error) => {
  console.error(`RND-239 theme persistence smoke: FAIL\n${error.stack || error.message}`);
  process.exitCode = 1;
});
