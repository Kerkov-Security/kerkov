---
name: forensics
description: Digital forensics analysis — file carving, memory forensics, PCAP analysis, and steganography
tags:
  - forensics
  - ctf
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

You are performing digital forensics analysis. Your objective is to extract evidence, recover hidden data, and reconstruct events from artifacts.

## File Analysis (Start Here)
```bash
file <artifact>           # Identify true file type
strings <artifact>        # Extract printable strings
xxd <artifact> | head     # Inspect hex/magic bytes
binwalk <artifact>        # Detect embedded files and headers
binwalk -e <artifact>     # Extract embedded files
exiftool <artifact>       # Extract metadata
```

## Common File Type Magic Bytes
| Type | Magic Bytes |
|------|-------------|
| PNG | `89 50 4E 47` |
| JPEG | `FF D8 FF` |
| PDF | `25 50 44 46` |
| ZIP | `50 4B 03 04` |
| 7-Zip | `37 7A BC AF` |
| ELF | `7F 45 4C 46` |
| PE (EXE) | `4D 5A` |

## Steganography

### Image Steganography
```bash
steghide extract -sf image.jpg          # Extract hidden data (try empty password)
steghide info image.jpg                 # Check if data is hidden
stegsolve                               # Visual analysis of bit planes
zsteg image.png                         # LSB analysis for PNG/BMP
stegoveritas image.png                  # Comprehensive steg analysis

# LSB manual extraction (Python)
python3 -c "
from PIL import Image
img = Image.open('image.png')
pixels = list(img.getdata())
bits = ''.join([str(p[0] & 1) for p in pixels[:1000]])
print(bytes([int(bits[i:i+8], 2) for i in range(0, len(bits), 8)]))
"
```

### Audio Steganography
```bash
# Inspect spectrogram for hidden text/images
# Use Audacity or Sonic Visualizer
sox audio.wav -n spectrogram -o spec.png
```

## PCAP / Network Analysis
```bash
# Wireshark CLI
tshark -r capture.pcap -Y "http" -T fields -e http.request.uri
tshark -r capture.pcap -Y "ftp" -T fields -e ftp.request.command -e ftp.request.arg
tshark -r capture.pcap -z follow,tcp,ascii,<stream_id>

# Extract files from PCAP
tcpflow -r capture.pcap -C
tshark -r capture.pcap --export-objects http,./extracted/

# DNS exfiltration check
tshark -r capture.pcap -Y "dns" -T fields -e dns.qry.name | sort | uniq -c | sort -rn
```

## Memory Forensics (Volatility)
```bash
# Profile detection (Volatility 2)
volatility -f memory.dmp imageinfo

# Volatility 3 (no profile needed)
vol -f memory.dmp windows.pslist
vol -f memory.dmp windows.netscan
vol -f memory.dmp windows.cmdline
vol -f memory.dmp windows.filescan | grep -i "flag\|secret\|pass"
vol -f memory.dmp windows.dumpfiles --pid <pid>
vol -f memory.dmp windows.hashdump      # Extract NTLM hashes
```

## Disk Forensics
```bash
# Mount disk image
sudo mount -o loop,ro disk.img /mnt/evidence

# Recover deleted files
foremost -t all -i disk.img -o recovered/
photorec disk.img

# File system timeline
fls -r -m / disk.img > timeline.bodyfile
mactime -b timeline.bodyfile -d > timeline.csv
```

## Log Analysis
```bash
# Web logs — extract IPs, status codes, URIs
awk '{print $1, $7, $9}' access.log | sort | uniq -c | sort -rn

# Auth logs — failed logins
grep "Failed password" /var/log/auth.log | awk '{print $11}' | sort | uniq -c | sort -rn

# Windows Event Log
python3 -c "import evtx; ..."   # Use python-evtx
```

## CTF Checklist
- [ ] `file` to identify type regardless of extension
- [ ] `strings` + `grep flag` or `grep CTF`
- [ ] `binwalk -e` for embedded files
- [ ] `exiftool` for metadata flags
- [ ] `steghide extract` with empty password
- [ ] Check for zip/archive files within files
- [ ] Hex editor for hidden data at end of file (appended after EOF marker)
