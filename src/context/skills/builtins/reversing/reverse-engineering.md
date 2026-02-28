---
name: reverse-engineering
description: Binary reverse engineering — static analysis, dynamic analysis, and decompilation
tags:
  - reversing
  - ctf
  - binary
tools:
  - shell_tool
  - read_tool
  - write_tool
---

You are performing binary reverse engineering. Your objective is to understand a binary's logic, bypass protections, and extract flags or secrets.

## Initial Triage
```bash
file binary                   # Architecture, type (ELF/PE/Mach-O), stripped?
strings binary | grep -i "flag\|ctf\|pass\|key\|secret"
strings binary | grep -E "[A-Za-z0-9+/]{20,}={0,2}"   # Base64-like strings
checksec --file=binary        # Security mitigations
readelf -h binary             # ELF header info
objdump -d binary | head -50  # Disassembly preview
```

## Security Mitigations (checksec output)
| Protection | Bypass Technique |
|-----------|-----------------|
| ASLR | Leak address, ret2libc |
| NX/DEP | ROP chains |
| Stack Canary | Leak canary value |
| PIE | Leak binary base |
| RELRO (Full) | Harder GOT overwrite |

## Static Analysis

### Ghidra (Free, Powerful)
```bash
# Launch Ghidra headless analysis
ghidra_headless /tmp/project MyProject -import binary -postScript PrintAST.java
# Or use GUI: File → Import → Analyze → Browse functions
```

### radare2 / rizin
```bash
r2 binary
> aaa              # Analyze all
> afl              # List functions
> s main; pdf      # Disassemble main
> VV               # Visual graph mode
> iz               # List strings
> iI               # Binary info
```

### objdump
```bash
objdump -d binary                        # Full disassembly
objdump -d binary | grep -A20 "<main>"   # Main function
objdump -t binary                        # Symbol table
```

### ltrace / strace (Dynamic — trace calls)
```bash
ltrace ./binary         # Library calls
strace ./binary         # System calls
ltrace -s 200 ./binary  # Show full string args
```

## Dynamic Analysis with GDB

### GDB with GEF / pwndbg
```bash
gdb ./binary
(gdb) r                          # Run
(gdb) info functions             # List functions
(gdb) disas main                 # Disassemble main
(gdb) b *0x<address>             # Breakpoint at address
(gdb) b main                     # Breakpoint at main
(gdb) x/s 0x<address>            # Examine string at address
(gdb) x/20x $rsp                 # Examine stack
(gdb) info registers             # Register state
(gdb) p strcmp                   # Find function address
```

## Common Reversing Patterns

### Anti-Debug Bypass
```bash
# ptrace anti-debug: LD_PRELOAD fake ptrace
# Time-based: patch conditional jump after timing check
# strace to see what syscalls are being made
```

### Patching Binaries
```bash
# Patch a conditional jump (e.g., change jne to je)
r2 -w binary
> s 0x<address>
> wa jmp 0x<target>    # Patch instruction

# Or with Python/pwntools
python3 -c "
data = open('binary','rb').read()
data = data.replace(b'\x75\x05', b'\x74\x05')  # jne → je
open('binary_patched','wb').write(data)
"
```

### Keygen / License Checks
1. Set breakpoint on `strcmp`, `strncmp`, `memcmp`
2. Inspect arguments at comparison — one arg is expected value
3. Note the expected string/hash and use it

### Encoding / Obfuscation
- XOR encoding: note XOR key in disassembly, reverse with same key
- Custom alphabet base64: identify alphabet string, write decoder
- Arithmetic transforms: trace through and invert operations

## CTF Workflow
1. `strings` + `grep flag` — quick win if flag is plaintext
2. `ltrace`/`strace` — intercept comparisons and system calls
3. `ghidra` decompile `main` and key functions — understand the logic
4. Set GDB breakpoints at critical comparisons
5. Patch the binary if logic is complex and we want to bypass rather than understand
6. Write a keygen or brute forcer once the algorithm is understood

## Useful pwntools for Automation
```python
from pwn import *

elf = ELF('./binary')
print(hex(elf.symbols['main']))   # Address of main
print(elf.strings)                 # Strings in binary

# Run with controlled input
p = process('./binary')
p.sendline(b'test_input')
print(p.recvall())
```
