# status_board — single-pane TUI for the workday

A long-lived [Textual](https://textual.textualize.io/) TUI that shows, in one
pane: remote job/cron boards fetched over SSH (through jump hosts where
needed), whether each of your sites is up (probed from wherever they are
reachable), and PRs awaiting your review across multiple GitHub accounts
(and Bitbucket). Panel definitions travel with the repos that own them — no
central registry to edit.

```bash
cd ~/GitHub/status_board
uv sync
uv run python src/status_board.py           # the live board
uv run python src/status_board.py --once    # one static render, no TUI (sanity check)
uv run python src/status_board.py --add     # wizard: add a new panel to a config
```

The TUI uses the readablecode "terminal navy" theme (shared with herdstone
via `readable-utils`); `--once` fetches every panel concurrently and prints
in config order.

## Keys

| Key | Action |
|-----|--------|
| `q` | quit |
| `r` | refresh **all** panels now |
| `u` | refresh **one** panel now — the panel under the mouse, else the focused one |
| `tab` / `shift+tab` | move focus between panels (clicking a panel also focuses it) |
| `esc` / `q` (in a log-follow pane) | back to the board |

PR titles and site names are clickable. Inside the TUI the click is handled by the app
itself (Textual owns the mouse, so terminal-native hyperlinks don't fire) and
opens the panel's `browser:` if set — e.g. `browser: edge` sends a work
account's PRs to Edge while everything else uses the OS default. Recognized
names: `edge`, `chrome`, `firefox`, `safari`; anything else is passed through
as the app/binary name. In `--once` output the titles are plain OSC 8
hyperlinks handled by the terminal (iTerm2, WezTerm, Kitty, recent Windows
Terminal), which always use the OS default browser.

## Where panels come from

This repo is designed to be cloned **next to** one or more private
`*_credentials` repos, all under the same parent directory:

```
~/GitHub/
├── status_board/                 <- this repo (public: code + secrets-free examples)
│   └── statusboard.yaml          <- optional, tracked: SECRETS-FREE panels only
├── personal_credentials/         <- private repo
│   ├── personal_statusboard.yaml <- panel definitions for this context
│   ├── personal_hosts.json       <- host inventory (SSH targets & jump hosts)
│   └── personal.env              <- gitignored tokens the panels reference
└── acme_credentials/             <- one private repo per work context
    ├── acme_statusboard.yaml
    ├── acme_hosts.json
    └── acme.env
```

On startup the board loads, in order:

1. `statusboard.yaml` in this repo's root, if present — tracked in the public
   repo, so **secrets-free panels only**;
2. `<context>_statusboard.yaml` in every sibling `*_credentials` repo
   (e.g. `acme_credentials/acme_statusboard.yaml`).

A machine only shows the panels of the credentials repos it has cloned —
clone a context's credentials repo and its panels appear. Panel names must
be unique across all loaded configs.

Panels are **grouped visually by the repo they came from**: each config's
panels sit together inside a double-bordered box titled with the context
(`acme`, `personal`, …), so one glance separates one context's world from
another's. In `--once` output the groups become heavy section rules.

Copy-paste-ready starting points for all three files live in
[`examples/`](examples/):

- [`statusboard.example.yaml`](examples/statusboard.example.yaml) — every
  panel type with every option, commented;
- [`hosts.example.json`](examples/hosts.example.json) — the host inventory
  schema;
- [`example.env`](examples/example.env) — where tokens go (never in the yaml).

## The add-panel wizard

```bash
uv run python src/status_board.py --add
```

walks through adding a panel interactively: pick which config file it lives
in (any sibling credentials repo — the file is created if it doesn't exist
yet — or this repo's tracked `statusboard.yaml`, flagged as public), pick the
panel type, and answer its fields. Host and jump tokens are checked against
the inventories as you type, `log_link` patterns are compile-checked, and the
final YAML is previewed before anything is written. The wizard **appends** to
existing configs, so hand-written formatting and comments are preserved. The
board picks the new panel up on next launch.

## Panel types

Every panel takes `name`, `type`, optional `interval` (seconds between
refreshes) and `note`.

### `ssh_command` — run a command on a remote machine and show its output

```yaml
- name: acme_vm_cron_jobs
  type: ssh_command
  host: sshacmevm          # inventory name or alias
  jump: sshacme            # optional jump hop (inventory name or alias)
  command: "bash ~/scripts/job_status.sh"
  interval: 300
  timeout: 90
```

`host` and `jump` resolve against the host inventories
(`<context>_hosts.json`): the config's own credentials repo is searched
first, then every other sibling inventory. The jump hop is injected on the
command line as `ssh -J user@host:port`, so the chain lives entirely in
config + inventory — **deliberately not in any machine's `~/.ssh/config`** —
and the board behaves identically on any machine with the credentials repos
cloned. When the board runs on the jump machine itself, the hop is skipped
automatically; when the *target* is the machine the board runs on, the
command runs locally with no ssh at all. An inventory host's
`identity_file` is passed as `-i`. This chain logic lives in the shared
`readable-utils` package (git-pinned in `[tool.uv.sources]`), the same code
the herdstone repo uses, so the two can't drift.

Requirements: non-interactive key auth to every hop (connections use
`BatchMode=yes`, so a password prompt = instant failure), and
`AllowTcpForwarding` enabled on the jump host's sshd (the default; needed
because `-J` tunnels through it — this works on Windows OpenSSH Server too).
ANSI colors in the command's output are rendered as-is.

#### `host_stats` — htop-style disk / cpu / memory meters for the host

Add `host_stats: true` to any `ssh_command` panel to get a row of htop-style
gradient meters for the host:

```
disk / ▕██████████████████░░░░▏  80% 48G of 60G    cpu ▕█░░░…▏   4% load 0.19 0.15 0.22 · 4 cores    mem ▕███░░░…▏  13% 2.0G of 15.6G
```

Each bar fills with a smooth green→yellow→red ramp (the same scale htop
paints its meters with), and the readout is colored by how full the resource
is. The remote host emits one machine-readable line; **all rendering happens
locally**, so the meters look identical everywhere they appear: pinned under
a command panel's output (below the scroll region, so a tall cron board
never pushes them out of view), as the entire body of a stats-only panel,
and in `--once` output.

Everything comes from numbers the kernel already maintains — nothing is
installed or tracked on the host: `df -Pk /` for disk, `/proc/loadavg` for
CPU (the same 1/5/15-minute averages `top`'s header shows, so a panel on a
5-minute `interval` reads the 5-minute column as its per-refresh average —
the cpu bar is the 5-minute load over the core count), and `free -m` for
memory. Linux keeps no memory average, so the `mem` figure is a
point-in-time reading at fetch. Linux hosts only.

A panel can also be **stats-only** — set `host_stats: true` and omit
`command` entirely for a host that has nothing else to report:

```yaml
- name: acme_vm_stats
  type: ssh_command
  host: sshacmevm
  host_stats: true
  interval: 300
```

#### `log_link` — click a row to follow its log

An `ssh_command` panel whose rows correspond to log files (like a cron
board) can make each row clickable:

```yaml
  log_link:
    pattern: '^[●○] \S+ +(\S+)'          # first capture group = the job token
    command: "tail -n 200 -F ~/logs/{job}.log"
```

`pattern` is matched against every line of the panel's output
(multiline); the **first capture group** in each match is underlined and
made clickable in the TUI. Clicking pushes a full-screen follow pane that
runs `command` (with `{job}` replaced by the captured, shell-quoted token)
over the **same host/jump chain the panel already uses**, streaming output
live — `tail -F` keeps following across log rotation. Press `esc` or `q`
to return to the board (the remote tail is killed on exit). `--once` output
is unaffected: a terminal can't host the follow pane, so the rows stay
plain text there.

### `http_checks` — is each site up?

```yaml
- name: acme_sites
  type: http_checks
  sites:
    - name: intranet
      url: https://intranet.acme.internal/
      insecure: true            # self-signed cert
    - url: http://10.0.0.20:8000/api/health
    - name: sso portal
      url: https://portal.acme.internal/
      expect: [200, 302, 401]   # codes that count as up
  host: sshacmevm               # optional: probe from this host
  jump: sshacme                 #   through this hop
  interval: 120
```

One `curl` GET per site, body discarded, redirects **not** followed (a 3xx
to a login page means the site is up — SSO fronts count as alive), a hard
per-site deadline (`max_time`, default 10s). A site is up when it answers
with any 2xx/3xx, or with one of its `expect` codes when that is set (e.g.
`expect: 401` for an API that demands auth on its root). Rows render as

```
✓ intranet       200 · 14ms
✗ api health     DOWN · (7) Failed to connect to 10.0.0.20 port 8000 ...
```

each site clickable (opens it in the panel's `browser:` if set), the panel
subtitle reads `all 3 up` or `2 up · 1 DOWN`, and any down site flags the
panel red the way a fetch error would — while still showing every row, so
the one that broke is obvious.

**Where the probe runs is the point.** With no `host`, the sites are probed
from the machine running the board — right for public sites. Internal sites
are usually reachable only from inside a network the board machine isn't
on, so `host` (plus an optional `jump`) moves the probe onto that machine
over the **same ssh chain an `ssh_command` panel uses**: the board sends a
single sh one-liner that curls every site and reads the results back, so
the sites are checked exactly the way their users reach them (DNS, cert
and all). A remote vantage host needs `curl` and a POSIX shell
(Linux/macOS). When the board happens to run *on* the vantage host, the ssh
hop is skipped and curl runs locally — no shell involved, so a board on
Windows works too. `timeout` (default 60s) bounds the ssh round trip when
probing remotely. `insecure: true` skips certificate verification for a
site with a self-signed or internal-CA cert.

### `github_prs` — PRs awaiting your review, one panel per account

```yaml
- name: github_personal_prs
  type: github_prs
  token_env: GH_PAT_PERSONAL
  env_file: personal.env    # optional, relative to the config's repo
  interval: 180
```

Shows every open PR the account is waiting on or being waited for — three
account-wide searches (`review-requested:`, `reviewed-by:`, `author:`), so
new repos are covered automatically. Each PR's reviews are then fetched to
badge the rows:

- `✏` you have an **unsubmitted draft review** — you wrote comments but never
  clicked "Submit review", so the author cannot see them; shown first and
  loudest because everyone is silently waiting on everyone
- `●` review requested — genuinely needs your review; when the author
  re-requests you after a changes-requested review, the PR returns here
  marked "re-requested after your changes"
- `✋` you requested changes — waiting on the author, not on you (a
  team-level request keeps a PR searchable without putting it back in your
  personal queue)
- `💬` you commented without approving/blocking and no request is pending
- `⬆` your own open PR, with the aggregate verdict of everyone else's
  reviews (`✓ approved` / `✗ changes requested` / `⧗ awaiting review`)
- `◌` draft PR — anyone's, yours included: greyed and sorted last, parked
  until it's marked ready, nothing to approve

PRs you approved with nothing further pending are dropped. The account is
whoever the token belongs to, so two accounts = two panels with different
`token_env` names. Two panels can also share ONE account with different
fine-grained tokens: the search only returns repos a token was granted, so a
client repo hosted under a personal account gets its own panel via a
single-repo PAT (which then lives in that client's credentials repo). Add
optional `search:` qualifiers (e.g. `-repo:owner/name`) to keep an
all-repos panel from overlapping a single-repo one. Tokens resolve from the
real environment first, then the `env_file` — tokens never go in the
statusboard config itself. Putting the token in the credentials repo's env
file is the intended setup: any machine with the repo cloned gets a working
board, no shell exports or gh CLI needed.

The PAT only needs **read** access:

- fine-grained token (recommended): `Pull requests: Read` — `Metadata: Read`
  is included automatically — granted on the repos you review in (or all
  repos of the account);
- classic token: `repo` scope if any repo is private (classic has no
  read-only option); no scopes for public-only.

Org tokens must be SSO-authorized or searches silently return nothing — the
board probes for this and shows which orgs are blocked instead of quietly
rendering an empty queue.

### `bitbucket_prs` — open PRs listing you as reviewer

```yaml
- name: acme_bitbucket_prs
  type: bitbucket_prs
  workspace: some-workspace
  repos: [repo-a, repo-b]   # omit to scan every repo in the workspace (slower)
  username_env: BB_USERNAME
  app_password_env: BB_APP_PASSWORD
  env_file: acme.env
```

Auth is a Bitbucket app password (Account settings → App passwords, scopes:
Account read + Pull requests read). Bitbucket has no cross-workspace
"review requested" search, so the board queries per repo — list the repos you
care about to keep it fast.

## Viewing from a personal device

The board runs wherever the credentials/keys live and you view it from
anywhere on the LAN — e.g. run it in tmux on one machine and attach from a
phone or tablet over SSH (Blink/Termius render Textual fine). Textual can
also serve any app as a web page
(`uv run textual serve "python src/status_board.py"`, needs `textual-dev`)
if a browser tab ever beats a terminal.

## Development

```bash
uv sync
uv run pytest            # unit tests - no network, no credentials needed
uv run flake8 .
uv run isort .
uv run mypy .
```
