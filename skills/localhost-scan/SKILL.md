---
name: localhost-scan
description: "Local host reconnaissance — enumerate services, ports, and attack surface on the current machine"
user-invocable: true
disable-model-invocation: false
metadata:
  kerkov:
    category: recon/local
    mitre: T1046
    difficulty: beginner
    platforms: [linux, windows, macos]
    tags: [recon, localhost, enumeration, ports, services]
    requires:
      bins: [nmap]
---

# Local Host Reconnaissance

Enumerate open ports, running services, and attack surface on the local machine (127.0.0.1 / ::1).

## Quick Scan

### Fast TCP port scan (top 1000 ports)

```bash
nmap -sT 127.0.0.1
```

### All TCP ports

```bash
nmap -sT -p- 127.0.0.1
```

### Service version detection

```bash
nmap -sT -sV -p- 127.0.0.1
```

> **Note:** Use `-sT` (TCP connect) instead of `-sS` (SYN) on localhost — SYN scans
> require root/admin and offer no speed benefit against the loopback interface.

## Detailed Enumeration

### Service versions + OS detection + default scripts

```bash
nmap -sT -sV -sC -O -p- 127.0.0.1
```

### UDP scan (common ports, requires root)

```bash
sudo nmap -sU --top-ports 100 127.0.0.1
```

### Scan both IPv4 and IPv6 loopback

```bash
nmap -sT -sV -p- 127.0.0.1 ::1
```

## Without nmap

If nmap is unavailable, use built-in OS tools:

### Linux

```bash
# List listening ports and associated processes
ss -tlnp

# Alternative with netstat
netstat -tlnp

# Check specific port
nc -zv 127.0.0.1 80
```

### Windows

```powershell
# List listening ports and associated processes
netstat -ano | findstr LISTENING

# Get process names for PIDs
Get-Process -Id <PID> | Select-Object ProcessName, Id, Path
```

### macOS

```bash
# List listening ports
lsof -iTCP -sTCP:LISTEN -nP

# Alternative
netstat -an | grep LISTEN
```

## Targeted Probes

### Web servers (common ports)

```bash
nmap -sT -sV -p 80,443,8080,8443,3000,5000,8000,9090 127.0.0.1
```

### Databases

```bash
nmap -sT -sV -p 3306,5432,27017,6379,1433,5984,9200 127.0.0.1
```

### Remote access / management

```bash
nmap -sT -sV -p 22,23,3389,5900,5985,5986 127.0.0.1
```

## Script Scanning

### Run default NSE scripts against discovered services

```bash
nmap -sT -sV --script=default -p <ports> 127.0.0.1
```

### Vulnerability scan against specific service

```bash
nmap -sT -sV --script=vuln -p <port> 127.0.0.1
```

### HTTP enumeration (if web server found)

```bash
nmap -sT -sV --script=http-enum,http-headers,http-methods -p <port> 127.0.0.1
```

## Post-Scan Analysis

After identifying open ports and services:

1. **Identify unexpected services** — anything listening that shouldn't be
2. **Check service versions** — look for outdated/vulnerable versions
3. **Test default credentials** — databases, admin panels, management interfaces
4. **Check bind addresses** — is the service bound to `0.0.0.0` (all interfaces) or just `127.0.0.1`?
5. **Review configurations** — look for debug modes, verbose errors, default configs

### Check if a service is exposed externally

```bash
# Compare localhost vs external interface
nmap -sT -sV -p <port> 127.0.0.1
nmap -sT -sV -p <port> <external_ip>
```

## Common Mistakes

- Using `-sS` (SYN scan) on localhost when not root — fails silently or errors out
- Forgetting to scan UDP — many services (DNS, SNMP, TFTP) use UDP only
- Only scanning top 1000 ports — services often run on non-standard high ports
- Not checking IPv6 loopback (`::1`) — services may bind to IPv6 only
- Assuming `127.0.0.1`-bound services are safe — local privilege escalation still applies

## References

- MITRE ATT&CK T1046 — Network Service Discovery
- [Nmap Reference Guide](https://nmap.org/book/man.html)
- [Nmap NSE Script Categories](https://nmap.org/nsedoc/categories/)
