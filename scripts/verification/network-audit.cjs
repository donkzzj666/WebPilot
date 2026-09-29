/* Verification-only audit of the Node process used by Playwright's driver.
 * This records attempts; it is not a firewall or the M1-12 network policy.
 */
"use strict";

const fs = require("node:fs");
const net = require("node:net");
const dns = require("node:dns");
const output = process.env.WEBAGENT_NETWORK_AUDIT;

if (output) {
  const record = (event, details = {}) => {
    fs.appendFileSync(output, JSON.stringify({ at: new Date().toISOString(), pid: process.pid, event, ...details }) + "\n");
  };
  record("audit_loaded", { node: process.version });

  const originalConnect = net.Socket.prototype.connect;
  net.Socket.prototype.connect = function (...args) {
    // net.connect normalizes arguments to an array before calling this method.
    const values = Array.isArray(args[0]) ? args[0] : args;
    const first = values[0];
    let target;
    if (first && typeof first === "object") {
      target = first.path ? { transport: "unix", path: first.path } : {
        transport: "tcp", host: first.host || "localhost", port: first.port,
      };
    } else if (typeof first === "string" && !/^\d+$/.test(first)) {
      target = { transport: "unix", path: first };
    } else {
      target = { transport: "tcp", host: typeof values[1] === "string" ? values[1] : "localhost", port: first };
    }
    record("net.connect", target);
    return originalConnect.apply(this, args);
  };

  // Both callback and promise APIs are covered, including explicitly created resolvers.
  const wrapDns = (object, method) => {
    const original = object[method];
    if (typeof original !== "function") return;
    object[method] = function (...args) {
      record("dns." + method, { host: String(args[0]) });
      return original.apply(this, args);
    };
  };
  for (const target of [dns, dns.promises, dns.Resolver.prototype, dns.promises.Resolver.prototype]) {
    for (const method of ["lookup", "lookupService", "resolve", "resolve4", "resolve6", "resolveAny", "resolveCaa", "resolveCname", "resolveMx", "resolveNaptr", "resolveNs", "resolvePtr", "resolveSoa", "resolveSrv", "resolveTxt", "reverse"]) {
      wrapDns(target, method);
    }
  }

  const dgram = require("node:dgram");
  for (const method of ["connect", "send"]) {
    const original = dgram.Socket.prototype[method];
    dgram.Socket.prototype[method] = function (...args) {
      // The complete primitive arguments make both overloaded send forms reviewable.
      record("dgram." + method, { arguments: args.map(value => typeof value === "string" || typeof value === "number" ? value : typeof value) });
      return original.apply(this, args);
    };
  }
  process.on("exit", code => record("audit_exit", { code }));
}
