# HTB solver command reference

Checked 2026-09-06 against the upstream sources below. Check installed help before
use. Replace placeholders with observed values for the requested HTB Labs machine.
Run local commands through Kerkov `exec`; use the managed shell for target commands.

## HTB API helper

Reuse the interpreter command rendered in the loaded skill's `Inputs and preparation`
section to run `'<skill_dir>/scripts/htb_service.py' --help` and `submit --help`.
Do not substitute system `python3`; an isolated installation may not provide it.

| Helper command | Method and route |
| --- | --- |
| `status <machine_id>` / `find <name>` | `GET /api/v4/machine/profile/<id-or-name>` |
| `submit <machine_id> <flag> [--difficulty <rating>]` | `POST /api/v5/machine/own` |
| `active` / `wait-ip --timeout 180` | `GET /api/v4/machine/active` |
| `terminate <machine_id>` (user-selected cleanup only) | `POST /api/v4/vm/terminate` |

Submission sends `id` and `flag`, plus `difficulty` when supplied. Do not globally
replace v4 with v5: endpoint versions differ. The v5 submission route is supported
by the current [htb-terminal machine service](https://github.com/Okymi-X/htb-terminal/blob/f29c1260743c94720747e6d1f839d4ee54f928c3/htb_terminal/services/machines.py)
and [HTB App MCP implementation](https://github.com/AeonDave/htb-app-mcp).
These are community-maintained clients, not an official HTB API specification or
proof of live authenticated behavior in this repository.

The [htb-terminal CLI](https://github.com/Okymi-X/htb-terminal/blob/main/htb_terminal/parser.py)
requires ratings in steps of ten from 10 to 100; other clients make the rating
optional. This helper preserves optional ratings and surfaces validation failures.
Follow the skill's recovery procedure when HTB requires a rating. Prefer a
client's current source over its README when they disagree.

HTB's [machine guide](https://help.hackthebox.com/en/articles/5185338-how-to-play-machines-on-hack-the-box)
describes instance controls and flag submission. Current access includes dedicated
instances for free users and a rotating free selection of retired machines; do not
infer an access failure solely from `retired: true`.

## Port discovery and service versions

Create `scans` under the engagement workspace first. Start with TCP; choose UDP
or broader enumeration when results justify it.

```sh
nmap -n -Pn -sT --open -p- -T3 -oA scans/tcp <target_ip>
nmap -n -Pn -sT -sV -p <observed_open_ports> -oA scans/services <target_ip>
```

`-Pn` skips host discovery; it does not fix routing. `-sT` works without raw socket
privileges; use `-sS` when privileged if appropriate. Run long scans as managed
sessions. Choose NSE scripts after checking their purpose; default scripts are
not necessarily passive. Source: [Nmap options](https://nmap.org/book/man-briefoptions.html).

## Web enumeration and virtual hosts

```sh
curl -sS -D - --max-time 15 'http://<target_ip>/'
curl -sS -D - --max-time 15 --resolve '<observed_host>:80:<target_ip>' 'http://<observed_host>/'
curl -sS -D - --max-time 15 --resolve '<observed_host>:443:<target_ip>' 'https://<observed_host>/'
```

Inspect redirects before following them and constrain each hostname to the target.
`--resolve` sets the connection address while preserving Host and TLS SNI. For a
confirmed lab certificate mismatch, add `--insecure` only to that request.
Source: [curl manual](https://curl.se/docs/manpage.html).

For an observed HTTP service, if ffuf and a suitable wordlist are installed:

```sh
ffuf -w '<wordlist>' -u 'http://<target_ip>/FUZZ' -ac -rate 25 -maxtime 120 -of json -o scans/http.json
ffuf -w '<wordlist>' -u 'http://<target_ip>/' -H 'Host: FUZZ.<observed_domain>' -ac -rate 25 -maxtime 120 -of json -o scans/vhosts.json
```

These rate/time values are starting limits, not HTB requirements. Compare a known
missing path/host to real hits before filtering; calibration can hide useful
results. Source: [ffuf usage and options](https://github.com/ffuf/ffuf).

## Recovered credentials and interactive access

Use credentials and services found on the requested machine:

```sh
ssh -tt <user>@<target_ip>
smbclient -L <target_ip> -U '<domain>/<user>'
smbclient '//<target_ip>/<observed_share>' -U '<domain>/<user>'
```

Use `mode=session` and respond to password prompts through `process`, keeping
passwords out of command arguments. Omit the domain prefix for local SMB accounts.
`ssh -tt` forces the remote PTY for interactive commands.
Sources: [OpenSSH manual](https://man.openbsd.org/ssh),
[Samba smbclient manual](https://www.samba.org/samba/docs/current/man-html/smbclient.1.html).

## Flag reads in the target session

Linux shell, adapting the username to the observed identity:

```sh
id
cat /home/<user>/user.txt
sudo -l
cat /root/root.txt
```

PowerShell, adapting account/profile paths to the target:

```powershell
whoami /all
Get-Content -LiteralPath 'C:\Users\<user>\Desktop\user.txt'
Get-Content -LiteralPath 'C:\Users\Administrator\Desktop\root.txt'
```

Read root.txt only with the access actually obtained. If the usual path is absent,
inspect the relevant home/profile directories before widening the search. Save
the exact flag content and path, then run the local submission helper. A credential
or terminal prompt is not a flag.
