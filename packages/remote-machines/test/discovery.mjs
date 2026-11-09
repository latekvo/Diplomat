// Discovery without the network: which names count as this network, what the ssh config,
// known_hosts and mDNS sources yield, how candidates from several sources merge into one,
// and that only a host whose key known_hosts already holds is trusted.
// Run: node test/discovery.mjs

import assert from 'node:assert/strict';
import fs from 'node:fs';
import path from 'node:path';
import { sandbox } from './lib/sandbox.mjs';

const sb = sandbox({
  hosts: {
    alpha: { hostname: 'alpha.local', user: 'builder' },
    cloud: { hostname: 'build.example.com' },
    pinned: { hostname: 'pinned.example.com' },
    other: { hostname: '10.0.0.9', port: 2222 },
    gone: { hostname: 'gone.local' },
    eqbox: { hostname: 'eqbox.local' },
    jumped: { hostname: 'jumped.local', extra: ['proxyjump bastion'] },
    aliased: { hostname: '10.0.0.20', port: 2200, extra: ['hostkeyalias keyalias.example.com'] },
    MixedBox: { hostname: 'fresh.lan' },
  },
  sshConfig: [
    'Host alpha',
    '  HostName alpha.local',
    'Host *.internal !nope',
    'Host cloud other gone jumped aliased',
    'Host=eqbox',
    'Include conf.d/*.conf',
    '',
  ].join('\n'),
  known: ['alpha.local', '[10.0.0.9]:2222', 'pinned.example.com', 'fresh.lan', 'jumped.local', 'keyalias.example.com'],
  settings: { remoteMachineHosts: ['pinned'], remoteMachineExclude: ['GONE'] },
});
fs.mkdirSync(path.join(sb.dir, 'conf.d'));
fs.writeFileSync(path.join(sb.dir, 'conf.d', 'a.conf'), 'Host included\n');
fs.writeFileSync(path.join(sb.dir, 'conf.d', 'b.txt'), 'Host notincluded\n');
// A hashed entry, a marker line and a public address alongside the plain LAN ones.
fs.appendFileSync(sb.env.FAKE_KNOWN_HOSTS, '|1|abc=|def= ssh-ed25519 AAAA\n@revoked bad.lan ssh-ed25519 AAAA\n8.8.8.8 ssh-ed25519 AAAA\n');
Object.assign(process.env, sb.env);
const d = await import('../src/discovery.js');

let checks = 0;
const check = (name, fn) => { fn(); checks++; console.log(`  PASS ${name}`); };

try {
  check('local-network names and addresses', () => {
    for (const h of ['radoniusz', 'Mini.LOCAL.', 'box.lan', 'pi.home.arpa', 'nas.ts.net', '10.1.2.3', '172.20.0.1',
      '192.168.1.5', '169.254.9.9', '100.100.1.1', 'fe80::1', 'fd12::3']) assert.equal(d.isLocalNetwork(h), true, h);
    for (const h of ['localhost', 'github.com', '8.8.8.8', '172.32.0.1', '100.128.0.1', '2001:db8::1', '']) {
      assert.equal(d.isLocalNetwork(h), false, h);
    }
  });

  check('ssh config aliases follow Include globs and skip patterns', () => {
    assert.deepEqual(d.sshConfigAliases(), ['alpha', 'cloud', 'other', 'gone', 'jumped', 'aliased', 'eqbox', 'included']);
  });

  check('known_hosts yields plain local-network entries only', () => {
    assert.deepEqual(d.knownHostsLan([sb.env.FAKE_KNOWN_HOSTS]), [
      { hostname: 'alpha.local', port: 22 },
      { hostname: '10.0.0.9', port: 2222 },
      { hostname: 'fresh.lan', port: 22 },
      { hostname: 'jumped.local', port: 22 },
    ]);
  });

  check('mDNS output from dns-sd and avahi-browse', () => {
    const dnsSd = [
      '; In the list of services below, the SRV records will typically reference dot-local Multicast DNS names.',
      'MacBook\\032Pro._ssh._tcp                     SRV     0 0 22 radoniusz.local. ; Replace with unicast FQDN',
      'box._ssh._tcp                                 SRV     0 0 2200 box.local. ; Replace with unicast FQDN',
      'box._ssh._tcp                                 TXT     ""',
    ].join('\n');
    assert.deepEqual(d.parseDnsSd(dnsSd), [{ hostname: 'radoniusz.local', port: 22 }, { hostname: 'box.local', port: 2200 }]);
    const avahi = [
      '+;eth0;IPv4;nas;SSH Remote Terminal;local',
      '=;eth0;IPv4;nas;SSH Remote Terminal;local;nas.local;192.168.1.20;22;',
    ].join('\n');
    assert.deepEqual(d.parseAvahi(avahi), [{ hostname: 'nas.local', port: 22 }]);
  });

  const list = await d.candidates({ hosts: ['pinned', '-oProxyCommand=touch pwned', 'two words'], exclude: ['GONE'] });
  const byTarget = Object.fromEntries(list.map((c) => [c.target, c]));
  check('candidates merge sources, keep the config alias, drop off-network and excluded hosts', () => {
    assert.deepEqual(list.map((c) => c.target).sort(), ['aliased', 'alpha', 'eqbox', 'fresh.lan', 'included', 'jumped', 'other', 'pinned']);
    // alpha comes from the ssh config and known_hosts; the alias, with its user, wins.
    assert.deepEqual(byTarget.alpha.sources, ['ssh-config', 'known-hosts']);
    assert.deepEqual(byTarget.alpha.names, ['alpha', 'alpha.local']);
    assert.equal(byTarget.alpha.user, 'builder');
    // An off-network host is kept only when Diplomat's settings name it.
    assert.equal(byTarget.cloud, undefined);
    assert.deepEqual(byTarget.pinned.sources, ['config']);
    // `other` and its known_hosts entry name the same host on a non-standard port.
    assert.deepEqual(byTarget.other.sources, ['ssh-config', 'known-hosts']);
    assert.equal(byTarget.other.port, 2222);
  });

  check('only a host whose key known_hosts holds is trusted', () => {
    assert.equal(byTarget.alpha.trusted, true);
    assert.equal(byTarget.other.trusted, true); // matched as [10.0.0.9]:2222
    assert.equal(byTarget.pinned.trusted, true);
    assert.equal(byTarget['fresh.lan'].trusted, true);
    assert.equal(byTarget.included.trusted, false);
    assert.equal(byTarget.aliased.trusted, true);
  });

  check('a host behind a ProxyJump is never trusted, whatever known_hosts holds', () => {
    assert.equal(byTarget.jumped.proxied, true);
    assert.equal(byTarget.jumped.trusted, false);
    assert.equal(byTarget.alpha.proxied, false);
  });

  const withoutAlpha = await d.candidates({ exclude: ['alpha'] });
  const withoutMixed = await d.candidates({ exclude: ['MixedBox'] });
  check('excluding an alias hides its host under every other name too', () => {
    assert.ok(!withoutAlpha.some((c) => c.hostname === 'alpha.local'), JSON.stringify(withoutAlpha.map((c) => c.target)));
    assert.ok(withoutAlpha.some((c) => c.target === 'fresh.lan'));
    assert.ok(!withoutMixed.some((c) => c.hostname === 'fresh.lan'), JSON.stringify(withoutMixed.map((c) => c.target)));
  });

  check('a name that is not a plain host token is dropped', () => {
    assert.ok(!list.some((c) => c.names.some((n) => n.startsWith('-') || n.includes(' '))), JSON.stringify(list.map((c) => c.names)));
  });

  check('discovery never connects to anything', () => assert.deepEqual(sb.probed(), []));
  console.log(`DISCOVERY OK — ${checks} checks passed`);
} finally {
  sb.cleanup();
}
