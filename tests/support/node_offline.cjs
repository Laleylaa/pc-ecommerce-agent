// Guard the reviewed TypeScript/SSR subprocess. No browser or live HTTP is used.
const denied = () => { throw new Error('Offline checks forbid external IO'); };
for (const name of ['node:http', 'node:https']) {
  const module = require(name);
  module.request = module.get = denied;
}
const net = require('node:net');
net.connect = net.createConnection = net.Socket.prototype.connect = denied;
const tls = require('node:tls');
tls.connect = denied;
const dns = require('node:dns');
dns.lookup = dns.resolve = denied;
globalThis.fetch = denied;
const cp = require('node:child_process');
for (const key of ['exec', 'execSync', 'spawn', 'spawnSync', 'fork', 'execFile', 'execFileSync']) cp[key] = denied;
