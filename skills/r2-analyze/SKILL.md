---
name: r2-analyze
description: "Reverse engineering binary analysis with radare2 — disassembly, decompilation, strings, xrefs, and exploitation"
user-invocable: true
disable-model-invocation: false
metadata:
  kerkov:
    category: reversing/analysis
    mitre: T1059.004
    difficulty: intermediate
    platforms: [linux, windows, macos]
    tags: [reversing, binary, radare2, disassembly, decompilation, exploitation, ctf]
    requires:
      bins: [r2]
---

# Binary Analysis with radare2

Analyze, disassemble, decompile, and exploit binaries using radare2 (r2). Covers static analysis, dynamic debugging, string extraction, cross-references, ROP gadget hunting, and patching.

## Opening a Binary

### Basic open (read-only)

```bash
r2 <binary>
```

### Open with auto-analysis (recommended first step)

```bash
r2 -A <binary>
```

> `-A` is equivalent to running `aaa` after opening. Use this for most analysis tasks.

### Open with write mode (for patching)

```bash
r2 -w <binary>
```

### Open in debug mode

```bash
r2 -d <binary>
```

### Open with specific architecture/bits

```bash
r2 -a arm -b 32 <binary>
r2 -a x86 -b 64 <binary>
```

## Non-Interactive (One-Shot) Analysis

Run r2 commands without entering the interactive shell. Ideal for scripting and tool integration.

### Print disassembly of main

```bash
r2 -q -c "aaa; s main; pdf" <binary>
```

### List all functions

```bash
r2 -q -c "aaa; afl" <binary>
```

### List all functions as JSON

```bash
r2 -q -c "aaa; aflj" <binary>
```

### Extract all strings

```bash
r2 -q -c "aaa; izz" <binary>
```

### Chain multiple commands

```bash
r2 -q -c "aaa; afl; s main; pdf; iI" <binary>
```

> `-q` = quiet mode (suppress banner), `-c` = run commands then exit.

## Analysis

### Auto-analysis (run first on any new binary)

```
aaa        # Full auto-analysis (functions, xrefs, strings, types)
aa         # Quick analysis (functions only)
aaaa       # Experimental, deeper analysis (slower)
```

### Function analysis

```
afl        # List all functions
aflj       # List all functions (JSON output)
afll       # List functions with details (size, calls, xrefs)
afi        # Info about current function
afn <name> # Rename current function
af         # Analyze function at current position
```

### Cross-references

```
axt <addr>    # Show xrefs TO this address (who calls this?)
axf <addr>    # Show xrefs FROM this address (what does this call?)
axtj <addr>   # Cross-references as JSON
```

## Navigation

### Seeking (moving cursor position)

```
s <addr>       # Seek to address
s main         # Seek to function by name
s sym.main     # Seek to symbol
s 0x08048000   # Seek to raw address
s-             # Undo seek
s+             # Redo seek
```

## Disassembly

### Print disassembly

```
pd 20          # Disassemble 20 instructions from current position
pdf            # Disassemble current function
pdf @main      # Disassemble function at main
pdf @sym.main  # Disassemble using symbol name
pD 64          # Disassemble 64 bytes
pi 10          # Print 10 instructions (no address/bytes, just mnemonics)
```

### Decompilation (r2ghidra plugin)

```
pdg            # Decompile current function to C (requires r2ghidra)
pdg @main      # Decompile specific function
pdgj           # Decompiled output as JSON
```

> Install r2ghidra: `r2pm -ci r2ghidra`

### Function graph (ASCII art)

```
agf            # ASCII function graph
agfj           # Function graph as JSON
VV             # Visual graph mode (interactive)
```

## Binary Info

### File metadata

```
iI             # File info (arch, bits, endian, format, OS)
iIj            # File info as JSON
ie             # Entry points
iS             # Sections
iSj            # Sections as JSON
is             # Symbols
ii             # Imports
iE             # Exports
il             # Libraries (linked)
```

### rabin2 (standalone binary info tool)

```bash
rabin2 -I <binary>    # File info
rabin2 -s <binary>    # Symbols
rabin2 -i <binary>    # Imports
rabin2 -S <binary>    # Sections
rabin2 -z <binary>    # Strings from data sections
rabin2 -zz <binary>   # All strings
```

## Strings

### String extraction

```
iz             # Strings from data sections
izz            # All strings (entire binary)
izj            # Strings as JSON
izzj           # All strings as JSON
iz~flag        # Grep strings for "flag"
iz~password    # Grep strings for "password"
```

### String loading flags

```bash
r2 -z <binary>                  # Disable string loading (faster open)
r2 -zz <binary>                 # Force strings from raw/unknown formats
r2 -e bin.rawstr=true <binary>  # Enable raw string extraction for unknown formats
r2 -n <binary>                  # Suppress all symbol and string loading
```

### rabin2 string extraction (standalone, no r2 session needed)

```bash
rabin2 -z <binary>     # Strings from data sections
rabin2 -zz <binary>    # All strings from entire binary
```

## Searching

### Search patterns

```
/ <string>          # Search for string
/x <hex>            # Search for hex bytes (e.g., /x 90909090)
/a <asm>            # Search for assembly pattern
/R <pattern>        # Search for ROP gadgets
/Rj <pattern>       # ROP gadgets as JSON
/R/                 # List all ROP gadgets
/r                  # Search for xrefs to current address
```

### ROP gadget hunting

```
/R pop rdi          # Find pop rdi; ret gadgets
/R pop rsi          # Find pop rsi gadgets
/R syscall          # Find syscall instructions
/R ret              # Find all ret gadgets
```

## Hex & Data Inspection

```
px 32              # Hexdump 32 bytes
px 64 @0x08048000 # Hexdump at specific address
pxw 32             # Hexdump as 32-bit words
p8 16              # Print 16 bytes as hex string
pf S               # Print as string
```

## Debugging

### Execution control

```bash
r2 -d <binary>       # Open in debug mode
r2 -d <binary> arg1  # Debug with arguments
```

```
dc             # Continue execution
ds             # Step into (single instruction)
dso            # Step over
dbt            # Backtrace
```

### Breakpoints

```
db <addr>      # Set breakpoint
db sym.main    # Breakpoint at function
dbi            # List breakpoints
dbc <addr> <cmd> # Run command when breakpoint hit
```

### Registers & memory

```
dr             # Show all registers
dr rip         # Show specific register
dr rip=0x4141  # Set register value
drj            # Registers as JSON
dm             # Memory maps
dmj            # Memory maps as JSON
```

## ESIL Emulation

ESIL (Evaluable Strings Intermediate Language) is r2's built-in emulation engine — a stack-based VM that can emulate instructions without running the binary.

### Setup and stepping

```
aei            # Initialize ESIL VM
aeim           # Initialize ESIL memory/stack
aeip           # Set instruction pointer to current seek
aes            # Step one instruction (emulated)
aeso           # Step over (emulated)
aesu <addr>    # Step until address
aec            # Continue emulation until breakpoint/end
```

### Inspect state

```
aer            # Show ESIL registers
aer rax=0x41   # Set register value in ESIL VM
```

### Display ESIL representation

```
e asm.esil=true   # Show ESIL alongside disassembly
ao               # Show detailed opcode info including ESIL
```

> ESIL is useful for tracing execution paths, understanding obfuscated code, and solving CTF challenges without running the binary natively.

## Conditional Breakpoints

### Skip breakpoint N times (counter-based)

```
f times=5
(dec_times,f times=`?vi times-1`,?= times)
db 0x4000ce
dbC 0x4000ce .(dec_times)
dc
```

### Break only when register matches value

```
e cmd.hitinfo=0
(break_rax,f reg_rax=`dr rax`,f test=`?vi reg_rax-0x31c0`,?= test)
db 0x4000ce
dbC 0x4000ce .(break_rax)
dc
```

### Trace register values to file

```
e cmd.hitinfo=0
(trace_rax,dr rax,?= 1)
db 0x4000ce
dbC 0x4000ce .(trace_rax)
dc > trace.txt
```

## Flags & Comments

### Bookmarking locations

```
f <name> @ <addr>  # Create a flag (bookmark) at address
f                  # List all flags
fr <old> <new>     # Rename flag
fd <addr>          # Find nearest flag to address
f-<name>           # Delete flag
```

### Annotations

```
CC <comment>       # Add comment at current offset
CC- @ <addr>       # Remove comment
Cd <size>          # Define as data
Cs <size>          # Define as string
Cf <size>          # Define as struct
```

## Signature Matching

### FLIRT signatures (identify library functions in stripped binaries)

```
zF <sigfile>       # Load FLIRT .sig file
z                  # List loaded signatures
```

```bash
# Load signatures when opening
r2 -c "zF libc.sig" <stripped_binary>
```

### Zignatures (r2-native signatures)

```
zg             # Generate signatures from current analysis
z              # List all signatures
```

### YARA integration (requires r2yara plugin)

```
yara add <rules.yar>   # Load YARA rules
yara scan               # Scan binary against loaded rules
```

```bash
# Install YARA plugin
r2pm -ci r2yara
```

> Default YARA rules (crypto signatures, packer detection) load automatically from r2's data directory.

## Patching

### Write operations (requires -w or oo+)

```
oo+            # Reopen in write mode
wx 9090        # Write NOP bytes (x86)
wa nop         # Write assembly instruction
wa "jmp 0x08048050"  # Write jump instruction
wox 0xff       # XOR current bytes with 0xff
```

### Write to/from file

```
wt <file>          # Write current block to file
wf <file>          # Write file contents to current offset
wF <file>          # Write file contents (fill block size)
```

### Common patches

```
# NOP out a check (x86)
s <addr_of_check>
wx 9090909090

# Change conditional jump to unconditional
s <addr_of_jne>
wa jmp <target>

# Patch return value
s <addr>
wa "mov eax, 1; ret"
```

## Output Modifiers

r2 commands support suffixes that modify output format:

```
<cmd>j         # JSON output (e.g., aflj, iIj, pdfj)
<cmd>~<str>    # Grep output (e.g., afl~main, iz~flag)
<cmd>~<str>:0  # Grep and get first column
<cmd>q         # Quiet output (e.g., aflq = just function names)
<cmd>*         # r2 command output (re-importable)
```

## Common CTF Workflows

### Quick binary triage

```bash
r2 -q -c "iI; aaa; afl; iz~flag; iz~pass; iz~key" <binary>
```

### Find and decompile main

```bash
r2 -q -c "aaa; s main; pdg" <binary>
```

### Check for easy wins (strings, flags)

```bash
r2 -q -c "izz~{flag,FLAG,ctf,CTF,pass,key,secret}" <binary>
rabin2 -z <binary> | grep -iE "flag|ctf|pass|key|secret"
```

### Analyze security mitigations

```bash
r2 -q -c "iI~canary; iI~pic; iI~nx; iI~relro" <binary>
rabin2 -I <binary> | grep -E "canary|pic|nx|relro|crypto"
```

### Find vulnerable functions

```bash
r2 -q -c "aaa; ii~{gets,strcpy,sprintf,scanf,system}" <binary>
```

### ROP chain building

```bash
r2 -q -c "aaa; /R pop rdi; /R pop rsi; /R pop rdx; /R syscall; /R ret" <binary>
```

### Identify function that references a string

```bash
# Find address of a string, then xref it
r2 -q -c "aaa; iz~interesting_string; axt @<string_addr>" <binary>
```

## Common RE Workflow

1. **Triage** — `iI` for file info, `iz`/`izz` for strings, `ii` for imports
2. **Analyze** — `aaa` to run full analysis
3. **Enumerate** — `afl` to list functions, identify interesting targets
4. **Navigate** — `s <function>` to seek to target function
5. **Disassemble** — `pdf` for assembly, `pdg` for decompiled C
6. **Trace** — `axt`/`axf` to follow cross-references
7. **Search** — `/` for strings, `/x` for bytes, `/R` for ROP gadgets
8. **Patch** — `oo+` then `wx`/`wa` to modify the binary
9. **Debug** — `r2 -d` to run and step through dynamically

## Companion Tools

| Tool | Purpose |
|------|---------|
| `rabin2` | Binary info extraction (fast, no analysis needed) |
| `rasm2` | Assemble/disassemble single instructions |
| `rahash2` | Hash and entropy calculation |
| `radiff2` | Binary diffing |
| `rafind2` | Raw byte/string searching |
| `rax2` | Base/number conversion (hex, dec, oct, etc.) |

### Examples

```bash
rasm2 -a x86 -b 64 "nop"             # Assemble: outputs 90
rasm2 -a x86 -b 64 -d "90"           # Disassemble: outputs nop
rahash2 -a md5,sha256 <file>          # Hash a file
radiff2 <bin1> <bin2>                 # Diff two binaries
rax2 0x41                             # Hex to decimal: 65
rax2 -s 0x414243                      # Hex to string: ABC
```

## Plugin Management

### r2pm (radare2 package manager)

```bash
r2pm -ci r2ghidra     # Install Ghidra decompiler plugin
r2pm -ci r2dec         # Install alternative decompiler
r2pm -ci r2yara        # Install YARA plugin
r2pm -l                # List installed plugins
```

## Common Mistakes

- Forgetting to run `aaa` before analysis commands — functions won't be detected
- Using `pdf` without seeking to a function first — gives garbage output
- Not using `-q -c` for scripted/non-interactive use — r2 drops to interactive shell
- Trying to write/patch without `-w` or `oo+` — writes silently fail
- Assuming `main` is always the entry point — stripped binaries may not have symbol names; use `ie` to find entry, then trace from there
- Not installing r2ghidra for decompilation — `pdg` won't work without it
- Forgetting the `j` suffix for structured output — raw text is harder for programmatic use

## When You Get Stuck

If a command doesn't work as expected or you need functionality not covered here, consult the upstream docs:

- **In-session help**: Append `?` to any command to see its subcommands (e.g., `a?`, `p?`, `d?`, `/?`)
- **Official docs directory**: https://github.com/radareorg/radare2/tree/master/doc — covers ESIL emulation (`esil.md`), debugging internals (`debug.md`), string handling (`strings.md`), FLIRT signatures (`flirt.md`), YARA rules (`yara.md`), IO (`io.md`), and platform-specific guides (Windows, iOS, Android, etc.)
- **radare2 Book**: https://book.rada.re — the most comprehensive reference for workflows and command syntax

When in doubt, use `<cmd>?` inside r2 before guessing at syntax.

## References

- [radare2 GitHub](https://github.com/radareorg/radare2)
- [radare2 Docs](https://github.com/radareorg/radare2/tree/master/doc)
- [radare2 Book](https://book.rada.re)
- [r2ghidra Plugin](https://github.com/radareorg/r2ghidra)
- MITRE ATT&CK T1059.004 — Command and Scripting Interpreter: Unix Shell
