# Electric Notifier — Iligan Light outage & billing popups

Pops up a desktop notification whenever your electrical provider
(https://www.facebook.com/iliganlight) posts about a **power outage** or a
**billing increase** — checked **once at every login (PC power-on)**, with a
keyword filter so only those two topics appear, plus a history log of past
alerts.

## How it works

1. An autostart entry runs `electric-notifier.py boot` at each login
   (that happens on every power-on).
2. It fetches the newest posts from the provider page via a small **local RSS
   bridge** (RSSHub, running in Docker on this PC — see "Feed setup" below).
3. Posts are matched against keyword **rules** in `config.json`
   (categories: `outage`, `billing`).
4. **New** matched posts raise popups — outages are `critical` urgency so they
   don't auto-dismiss; billing updates are normal urgency. **Clicking the
   popup (or its "Open post" button) opens the actual Facebook post in your
   browser.** The click stays armed for `click_wait` seconds (default 120)
   while the notifier waits; afterwards use the `open` command instead.
5. Shown post IDs are stored in `~/.local/share/electric-notifier/state.json`
   so nothing ever repeats. Every popup is appended to
   `~/.local/share/electric-notifier/history.log`.

## Files

```
~/electric-notifier/
├── electric-notifier.py   the whole engine (stdlib-only Python)
├── config.json            feeds, urgency, keyword rules
├── install.sh             installs/removes the autostart entry
└── README.md              this file

~/.local/share/electric-notifier/
├── state.json             seen-post cache (the "no duplicates" memory)
├── history.log            every popup ever shown
├── notifier.log           diagnostics
└── .lock                  prevents double-run popups
```

## Feed setup (one-time, ~5 minutes)

Facebook blocks anonymous scraping (tested: public RSSHub instances return
503, mbasic returns HTTP 400). The reliable, personal route is a **local
RSSHub** instance using Facebook's own internal API with your session cookie.
Nothing is sent anywhere — it stays on your PC.

**1. Copy two cookies from your logged-in browser**

Log in at facebook.com → press F12 → **Application** (Firefox: Storage) →
**Cookies** → `https://www.facebook.com` → copy the values of:

- `c_user`
- `xs`

**2. Run the bridge (Docker is already installed on your machine)**

```bash
docker run -d --name rsshub --restart=always \
  -p 127.0.0.1:1200:1200 \
  -e FACEBOOK_COOKIE='c_user=PASTE_C_USER; xs=PASTE_XS' \
  diygod/rsshub
```

(First run downloads the image — give it a minute. `--restart=always` makes it
come back after every reboot, so it's ready by the time you log in.)

**3. Test the feed**

```bash
~/electric-notifier/electric-notifier.py test-feed
```

You should see recent post titles, some tagged `[outage]` / `[billing]`.
Posts only get tagged when the title/content mentions those topics — most
casual posts won't, and that's exactly the filter you asked for.

**Updating the bridge occasionally:** Facebook changes internals, so refresh
RSHub every few months:

```bash
docker pull diygod/rsshub && docker rm -f rsshub
# …then re-run the docker run line above with the same cookies
```

### Alternative feeds (no Docker)

The script understands **any** RSS/Atom URL. If you ever prefer a hosted
bridge (e.g. fetchrss.com or an Apify-generated feed), just add its URL to
`config.json → feeds` (first working URL wins; the local RSSHub entry is
first by default).

## Daily commands

```bash
~/electric-notifier/electric-notifier.py test-feed     # is the feed OK?
~/electric-notifier/electric-notifier.py test-notify   # sample popup (clickable!)
~/electric-notifier/electric-notifier.py test-rules --text "scheduled power interruption tonight"
~/electric-notifier/electric-notifier.py open          # open the latest alert's post in the browser
~/electric-notifier/electric-notifier.py boot          # run a manual check
$EDITOR ~/electric-notifier/config.json                # tweak keywords
tail -20 ~/.local/share/electric-notifier/notifier.log # diagnostics
cat ~/.local/share/electric-notifier/history.log       # past alerts
```

## Keyword rules

Rules live in `config.json` under `rules`. Each category is a list of
regexes (case-insensitive) matched against the post title + text. To make a
rule looser or stricter, just edit the entries — for example, to catch *any*
billing post (not only increases), add `"\\bbilling\\b"` to `billing`.

Test any text against the rules:

```bash
~/electric-notifier/electric-notifier.py test-rules --text "ERC approved rate increase effective next billing period"
```

## Background watcher (installed on this machine)

Besides the boot check, a systemd **user timer** re-checks the feed every
15 minutes while the PC is on — 3 minutes after login (lets Docker warm up),
then every 15 minutes. Same rules: keyword-filtered, no repeats, popups stay
clickable. The unit files ship with this repository under `systemd/`.

```bash
# install on a fresh machine
mkdir -p ~/.config/systemd/user
cp ~/electric-notifier/systemd/electric-notifier.{service,timer} ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now electric-notifier.timer

# management on this machine
systemctl --user list-timers electric-notifier.timer        # when is the next check
journalctl --user -u electric-notifier.service -f           # live log
systemctl --user disable --now electric-notifier.timer      # turn the watcher off
systemctl --user enable --now electric-notifier.timer       # turn it back on
```

Change the interval (for example to 5 minutes): edit
`~/.config/systemd/user/electric-notifier.timer`, set `OnUnitActiveSec=5min`,
then run `systemctl --user daemon-reload`.


## Troubleshooting

- **No popup at boot** — the feed was probably unreachable at that moment
  (e.g. internet not up yet, RSSHub still starting). The script logs it and
  retries at the NEXT boot; missed posts are not lost (they stay "new" until
  shown). Check `notifier.log`.
- **Popups blocked by Focus / Do Not Disturb** — GNOME: Settings →
  Notifications → allow notifications, uncheck Do Not Disturb.
- **`test-feed` says rate limit / errors** — refresh your Facebook cookie
  (`c_user` + `xs` expire if you log out; re-copy and recreate the container).
- **Post IDs reset** — delete `~/.local/share/electric-notifier/state.json`
  and run `boot` once; it re-bootstraps silently (popups only for posts from
  the last `bootstrap_days`).
- **Clicked too late / away from desk** — the click works while the notifier
  waits (default 120 s per popup, `notify.click_wait` in config). After that,
  open the post with `electric-notifier.py open` or copy the URL from
  `history.log`.

## License

MIT — see [LICENSE](LICENSE).

## System updates & recovery

Everything runs in **user space**, so it shrugs off routine system churn:

| Event | Result |
|---|---|
| Kernel updates | No effect — no kernel modules involved. After any reboot, Docker (`enabled`), the RSSHub container (`restart=always`), the autostart entry and the 15-min timer all come back automatically. |
| Regular package/OS updates | No effect — plain files in `$HOME` + stdlib Python. |
| Password change, browser cookie rotation | Fine — only matters if you *log out* of Facebook (re-copy cookies, re-run the docker command). |
| OS reinstall / wiped home directory | Everything local is gone — two-step full recovery below. |

**Full recovery after wiping the disk** (works on any machine, new PC included):

```bash
git clone https://github.com/ryann-g/electric-notifier
cd electric-notifier
chmod +x restore.sh && ./restore.sh
```

`restore.sh` installs Docker requirements, recreates the bridge (asks for
your cookies at runtime — nothing is stored in the repo), re-enables the
background watcher and the autostart entry, then runs the test cycle.

## Uninstall

```bash
~/electric-notifier/install.sh --uninstall        # remove autostart
docker rm -f rsshub                               # remove the bridge
rm -rf ~/electric-notifier ~/.local/share/electric-notifier  # full purge
```
