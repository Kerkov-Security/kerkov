---
name: hash-crack
description: Hash identification and cracking using hashcat and john the ripper
tags:
  - crypto
  - cracking
  - password
tools:
  - shell
  - read
  - write
---

## Execution Behavior
- If the user gives a direct command or concrete action and required details are present, execute it immediately using available tools.
- Do not pre-announce commands (for example, avoid "I will run ...") before executing.
- Ask follow-up questions only when required inputs are missing or the action is destructive/high-risk.
- If the user confirms with "yes" after a pending action, execute the action immediately.
- After execution, report the result first in 1-3 lines, then optional next steps if helpful.
- Keep responses concise and action-oriented unless the user asks for detailed methodology.

You are performing hash identification and cracking. Your objective is to identify hash types and recover plaintext values using available tooling.

## Hash Identification

### Manual Identification by Length / Format
| Hash | Length | Example Prefix |
|------|--------|----------------|
| MD5 | 32 chars | `5f4dcc3b5aa765d61d8327deb882cf99` |
| SHA1 | 40 chars | `5baa61e4c9b93f3f0682250b6cf8331b7ee68fd8` |
| SHA256 | 64 chars | — |
| SHA512 | 128 chars | — |
| bcrypt | 60 chars | `$2y$` / `$2b$` |
| NTLM | 32 chars | — |
| LM | 32 chars | — |
| MD5crypt | variable | `$1$` |
| sha512crypt | variable | `$6$` |

### Automated Identification
```bash
# hashid
hashid '<hash>'

# hash-identifier
hash-identifier

# hashcat auto-detect (example mode lookup)
hashcat --example-hashes | grep -A2 '<hash_pattern>'
```

## Cracking with Hashcat

### Common Hash Modes
```bash
# MD5
hashcat -m 0 hashes.txt wordlist.txt

# SHA1
hashcat -m 100 hashes.txt wordlist.txt

# SHA256
hashcat -m 1400 hashes.txt wordlist.txt

# bcrypt
hashcat -m 3200 hashes.txt wordlist.txt

# NTLM
hashcat -m 1000 hashes.txt wordlist.txt

# sha512crypt ($6$)
hashcat -m 1800 hashes.txt wordlist.txt
```

### Attack Modes
```bash
# Dictionary attack (-a 0)
hashcat -m <mode> -a 0 hashes.txt /usr/share/wordlists/rockyou.txt

# Dictionary + rules (-a 0 with rules)
hashcat -m <mode> -a 0 hashes.txt rockyou.txt -r /usr/share/hashcat/rules/best64.rule

# Brute force (-a 3)
hashcat -m <mode> -a 3 hashes.txt ?a?a?a?a?a?a?a?a

# Combination attack (-a 1)
hashcat -m <mode> -a 1 hashes.txt wordlist1.txt wordlist2.txt
```

### Charset Masks
```
?l = lowercase letters
?u = uppercase letters
?d = digits
?s = special characters
?a = all (lowercase + uppercase + digits + special)
```

## Cracking with John the Ripper
```bash
# Auto-detect and crack
john hashes.txt --wordlist=/usr/share/wordlists/rockyou.txt

# With format specification
john hashes.txt --format=bcrypt --wordlist=rockyou.txt

# Show cracked passwords
john hashes.txt --show

# Linux /etc/shadow
unshadow /etc/passwd /etc/shadow > combined.txt
john combined.txt --wordlist=rockyou.txt
```

## Recommended Wordlists
- `rockyou.txt` — classic, large general wordlist
- SecLists `Passwords/` — curated security-focused lists
- `kaonashi.txt` — rule-based mutations
- CrackStation wordlist — very large, covers common passwords

## Online Lookup (for fast wins)
Before cracking locally, try:
- `crackstation.net` — large precomputed rainbow tables
- `hashes.com` — community hash lookup

## CTF Context
In CTF crypto challenges involving hashes:
- Always try online lookup first (saves time)
- Check if hash is salted (salt:hash format)
- Look for custom hash implementations — may have intentional weaknesses
- Consider hashcat rule generation for targeted cracking (known username, birth year, etc.)
