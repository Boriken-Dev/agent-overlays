# Bohío as an agent's remote hands: node, peer, `bohio mcp`

Facts found by setting up a Bohío node on a Linux build host and driving it from a Windows workstation through `bohio mcp`.
The protocol and every setting are in Bohío's own `docs/` (`agents.md`, `security.md`) and `examples/bohio.cfg`; this file
holds only what had to be rediscovered.

## Node on a host with no login session (a transient systemd unit)

- `bohio serve` binds its local control endpoint at `pipe://bohio-<id>`, which on Unix needs `XDG_RUNTIME_DIR`. A unit run
  as an account with no login has none, so `bohio pair` and `peer` against the running node fail. Set
  `[control] endpoint = "pipe:///ABS/PATH/data/control.sock"` (a path inside the node's data directory).
- The commands (`peer approve`, `pair`, `peer list`) read the same `--config` as the node and write into it
  (`[peer."sha256:..."]`); run them as the account that runs the node. The config and its directory must be owned by that account
  and not group- or world-writable, or `serve` refuses to start.
- A transient unit (`systemd-run --uid=ACCOUNT --unit=NAME -p MemoryMax=... -p CPUQuota=... bohio serve --config ...`) puts
  every process an `exec` starts into the unit's cgroup, so the unit's `MemoryMax` and `CPUQuota` cap the builds a peer runs
  too (`cat /proc/self/cgroup` through `exec` shows it). An unprivileged account cannot start its own `systemd-run --scope`
  there, so give the unit the build's cap. It is gone at stop or reboot: restart it with the same command; the
  config and the data directory (identity, approved peers) persist.
- A Rust release build of `bohio --features cli` with the SDK lock's toolchain takes about five minutes at 4 jobs.

## Peers and grants

- `fs_roots` take literal directories. A glob such as `/work/ai6-*` is kept as text and matches nothing (every path is
  "leaves the directories this peer may use"); repeat `--fs-root` for each directory.
- `bohio peer approve ID --allow exec,fs.read,fs.write --fs-root DIR ...` replaces the peer's whole table each time it is
  given any of `--allow/--fs-root/--program`.
- A client that does not yet trust the host is refused with "the peer proved its key and is not trusted"; give
  `--server HOST_ID` (the id `bohio id` prints on the host, compared over another trusted channel). The host then lists the
  client's key as `waiting` in `bohio peer list` and `peer approve` accepts it. `bohio join` with a PIN (`bohio pair` on the host)
  failed here over a 280 ms link with "the network path was lost" on every try; approving the waiting key by id worked.
- `exec` is not confined by `fs_roots`: a peer with `exec` runs anything the node's account can.

## Client use

- Every command takes `--config` after the subcommand (`bohio id --config F`, `bohio remote --config F ENDPOINT --server ID info`),
  not before it.
- Git Bash on Windows rewrites an argument that looks like a path (`/home/x`) into `C:/Program Files/Git/home/x`: set
  `MSYS_NO_PATHCONV=1` for `bohio remote ... put/hash/read`.
- `bohio mcp` `exec` takes `argv` or `script`; `script` needs a `shell` kind the host does not know on Linux (`bad_request`),
  so use `argv: ["bash", "-c", "..."]`. Without the `session` permission the `session_*` tools are unusable, so a long build is one `exec`
  with `deadline_ms: 0`.
- `put` and `get` need `--local-root DIR` on the `bohio mcp` command line; `put` sends only what changed.
- Registering for Claude Code: `claude mcp add --scope local NAME -- BOHIO.exe mcp --config CFG --local-root DIR --host
  "NAME=tcp://HOST:PORT --server HOST_ID"`. The tools are `mcp__NAME__exec`, `read`, `write`, `put`, `hash`, ... after a restart or
  `/mcp`. To test the same server without a restart, speak newline-delimited JSON-RPC to it on standard input (initialize,
  `notifications/initialized`, `tools/call`), from a script file.
- Measured over a 280 ms WAN: each call after the first costs one round trip (0.31 s); a new `ssh host cmd` costs 3.2 s; a
  94 MB `put` took 47 s against 5 minutes for `tar | ssh tar`.
