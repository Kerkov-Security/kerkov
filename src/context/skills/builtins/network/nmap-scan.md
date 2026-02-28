---
name: nmap-scan
description: Network scanning and service enumeration with nmap
tags:
  - network
  - recon
  - enumeration
tools:
  - shell_tool
  - write_tool
---

You are performing network scanning and enumeration. Your objective is to map the target network, identify open ports, running services, and OS fingerprints.

## Scan Progression (Low to High Noise)

### 1. Host Discovery
```bash
# Ping sweep — find live hosts
nmap -sn 192.168.1.0/24

# ARP scan (local network, more reliable)
nmap -PR -sn 192.168.1.0/24
```

### 2. Quick Port Scan
```bash
# Top 1000 ports, fast
nmap -T4 -F <target>

# All ports (takes longer but thorough)
nmap -T4 -p- <target>
```

### 3. Service & Version Detection
```bash
# Service versions on discovered ports
nmap -sV -p 22,80,443,8080 <target>

# Aggressive detection (OS + version + scripts)
nmap -A -p <ports> <target>
```

### 4. OS Fingerprinting
```bash
nmap -O <target>
nmap -O --osscan-guess <target>
```

### 5. NSE Script Scanning
```bash
# Default scripts (safe)
nmap -sC -p <ports> <target>

# Vulnerability scripts
nmap --script vuln -p <ports> <target>

# Specific service scripts
nmap --script smb-enum-shares,smb-enum-users -p 445 <target>
nmap --script http-title,http-headers -p 80,443 <target>
nmap --script ftp-anon -p 21 <target>
nmap --script ssh-auth-methods -p 22 <target>
```

### 6. UDP Scan (often overlooked)
```bash
nmap -sU --top-ports 100 <target>
nmap -sU -p 53,67,68,69,123,161,162,500 <target>
```

## Common Service Ports to Investigate
| Port | Service | Notes |
|------|---------|-------|
| 21 | FTP | Anonymous login, version vulns |
| 22 | SSH | Version-based exploits, weak creds |
| 23 | Telnet | Cleartext credentials |
| 25 | SMTP | Open relay, user enumeration |
| 53 | DNS | Zone transfer (`dig AXFR`) |
| 80/443 | HTTP/S | Web app attack surface |
| 445 | SMB | EternalBlue, enum shares/users |
| 1433 | MSSQL | Default creds, xp_cmdshell |
| 3306 | MySQL | Default creds, file read |
| 3389 | RDP | Brute force, BlueKeep |
| 5432 | PostgreSQL | Default creds |
| 6379 | Redis | Unauthenticated access |
| 8080/8443 | Alt HTTP | Admin panels, dev instances |

## Output & Reporting
```bash
# Save all output formats
nmap -A -p- -oA scan_results <target>

# Grepable output for quick parsing
nmap -oG - <target> | grep "open"
```

## Post-Scan
After identifying open ports and services:
- Research service versions against CVE databases
- Use service-specific tools for deeper enumeration
- Consider activating `sqli`, `privesc-linux`, or other relevant skills based on findings
