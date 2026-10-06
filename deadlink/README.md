# Deadlink

A browser game that teaches networking basics. A zombie outbreak took the
network down one node at a time; you rebuild the grid from a bunker and bring
the survivors back online.

Made for a high school cyber club. One HTML file, no install, works offline,
and sound is optional (every cue also shows on screen).

**Play:** download [`Deadlink.html`](Deadlink.html) (or grab it from the
`deadlink-v2.0.0` release) and open it in any modern browser.

## What it teaches

| Node | Concept | Students do |
|------|---------|-------------|
| 1 | Binary | Flip switches to build numbers, then read binary back |
| 2 | IP addresses | Sort packets: private LAN, public internet, broken (> 255), loopback |
| 3 | DHCP | Run DORA, hand out free addresses, avoid IP conflicts |
| 4 | DNS | Look up names, use the cache and TTL, NXDOMAIN, reject spoofed replies |
| 5 | Ports | Send connections to the right service, spot refused ports, apply a firewall policy |
| 6 | Subnets | Local or gateway, then subnet math (usable hosts, network and broadcast, smallest fit) |
| 7 | OSI model | Sort things into the 7 layers, then diagnose which layer an outage is in |

## How it plays

- Answer problems at a node. 8 right **secures** it.
- Close that node's **Field Terminal** case: a simulated Linux terminal where you
  find and fix a fault with real commands (`ip a`, `ip route`, `ping`,
  `nslookup`, `nc -zv`, `curl`, `dhclient`, `traceroute`). Each case is
  inspect, fix, verify, submit.
- Spend the salvage you earned to bring the next node online. Nothing earns
  salvage while the game is closed. There is no automation on purpose.
- Wrong answers make noise; too much noise draws a horde that halves salvage
  until you push it back. Time-outs and hints never add noise.
- Every node has a hint (H). Calm mode turns the timers off.
- The **Report** button exports a CSV of what the student practiced.

`Teacher Notes.html` covers classroom use and has the answer key for all 7
Field Terminal cases.

## Teacher tools

Open the page with `?teacher=1` on the end of the address, then go to Settings.

## Saves

Progress saves in the browser's localStorage. Settings has a save code to move
progress between machines, and a button that wipes everything stored.

## Building from source

`source/` holds `game.js`, `style.css` and `build.py`. The build inlines the CSS
and JS into one file:

```bash
cd deadlink/source
python build.py   # writes out/Deadlink.html
```

Everything in the Field Terminal is simulated. Nothing touches the real
computer or network.
