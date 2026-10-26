// Preloaded by the portal server (`node --require ./client-ip.cjs server.js`, see the Dockerfile).
//
// Next.js keeps an `X-Forwarded-For` header the client sent and only fills it in when it is
// missing, so a browser talking to the portal directly could name any address it liked, and
// andai-core (which believes the header from the portal) would key its login limiter and audit
// trail on it. This hook rewrites the header on every incoming request before Next sees it, to the
// one client address the portal itself believes:
//
// * the TCP peer, when the peer is not a trusted proxy;
// * otherwise the header read right to left, skipping trusted proxies; the first other address.
//
// Trusted proxies in front of the portal (a TLS ingress, a load balancer) are listed in
// ANDAI_PORTAL_TRUSTED_PROXIES as comma-separated addresses or CIDRs; by default there are none.
"use strict";

const http = require("node:http");
const net = require("node:net");

function plainAddress(value) {
  let address = String(value || "").trim();
  if (address.startsWith("[") && address.includes("]")) address = address.slice(1, address.indexOf("]"));
  if (address.toLowerCase().startsWith("::ffff:") && net.isIPv4(address.slice(7))) address = address.slice(7);
  return net.isIP(address) ? address : null;
}

function trustedList(spec) {
  const list = new net.BlockList();
  for (const entry of String(spec || "").split(",").map((e) => e.trim()).filter(Boolean)) {
    const [base, bits] = entry.split("/");
    const address = plainAddress(base);
    if (!address) throw new Error(`ANDAI_PORTAL_TRUSTED_PROXIES: not an address or CIDR: ${entry}`);
    const family = net.isIPv6(address) ? "ipv6" : "ipv4";
    if (bits === undefined) list.addAddress(address, family);
    else list.addSubnet(address, Number(bits), family);
  }
  return list;
}

function isTrusted(list, address) {
  return list.check(address, net.isIPv6(address) ? "ipv6" : "ipv4");
}

function clientAddress(list, peer, header) {
  const peerAddress = plainAddress(peer);
  if (!peerAddress) return null;
  if (!isTrusted(list, peerAddress) || !header) return peerAddress;
  const hops = String(header).split(",").reverse();
  for (const hop of hops) {
    const address = plainAddress(hop);
    if (!address) return peerAddress;
    if (!isTrusted(list, address)) return address;
  }
  return peerAddress;
}

const trusted = trustedList(process.env.ANDAI_PORTAL_TRUSTED_PROXIES);
const emit = http.Server.prototype.emit;
http.Server.prototype.emit = function emitWithClientAddress(event, request, ...rest) {
  if (event === "request" && request && request.headers) {
    const client = clientAddress(trusted, request.socket && request.socket.remoteAddress, request.headers["x-forwarded-for"]);
    if (client) request.headers["x-forwarded-for"] = client;
    else delete request.headers["x-forwarded-for"];
  }
  return emit.call(this, event, request, ...rest);
};

module.exports = { clientAddress, trustedList };
