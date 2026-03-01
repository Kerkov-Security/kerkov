---
name: crypto-analysis
description: Cryptographic analysis for CTF challenges — classical ciphers, encoding, and modern crypto weaknesses
tags:
  - crypto
  - ctf
  - analysis
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

You are performing cryptographic analysis. Your objective is to identify the encryption scheme and recover plaintext, often in a CTF or audit context.

## Step 1 — Identify the Scheme

### Encoding (not encryption — easy to reverse)
- Base64: ends in `=`, alphanumeric + `+/`
- Hex: `0-9a-f` characters only, even length
- Base32: uppercase + `2-7`, ends in `=`
- URL encoding: `%XX` sequences
- Morse code: dots and dashes

```bash
# Quick decode with Python
echo "SGVsbG8=" | base64 -d
echo "48656c6c6f" | xxd -r -p
python3 -c "import base64; print(base64.b32decode('JBSWY3DPEB3W64TMMQ======'))"
```

### Classical Ciphers
- **Caesar / ROT13**: single letter shift — try all 26 rotations
- **Vigenère**: repeating key XOR-like — use index of coincidence or kasiski test
- **Substitution**: each letter maps to another — use frequency analysis
- **Atbash**: A↔Z, B↔Y mirror substitution
- **Rail Fence**: zigzag transposition
- **Playfair / Hill**: polygraphic substitutions

```bash
# ROT13
echo "Uryyb" | tr 'A-Za-z' 'N-ZA-Mn-za-m'

# Try all Caesar shifts
python3 -c "
s='Khoor Zruog'
for i in range(26):
    print(i, ''.join(chr((ord(c)-65-i)%26+65) if c.isupper() else chr((ord(c)-97-i)%26+97) if c.islower() else c for c in s))
"
```

### Modern Crypto Weaknesses to Look For

#### RSA
- Small public exponent `e=3` with unpadded messages → cube root attack
- Common modulus attack (same `n`, different `e`)
- Small prime factors → factor with `factordb.com` or `sympy`
- Wiener's attack (very small private key `d`)
- Padding oracle (PKCS#1 v1.5)

```python
from sympy import factorint
n = <modulus>
print(factorint(n))  # Factor small n
```

#### AES
- ECB mode → identical plaintext blocks produce identical ciphertext → block swap attacks
- CBC with padding oracle → byte-by-byte decryption
- CTR mode nonce reuse → XOR ciphertexts to eliminate keystream

#### XOR
- Single-byte XOR: brute force all 256 keys, score by letter frequency
- Repeating-key XOR: find key length via Hamming distance / coincidence index, then break each key byte

```python
# Single-byte XOR brute force
ct = bytes.fromhex("<hex_ciphertext>")
for key in range(256):
    pt = bytes([b ^ key for b in ct])
    if pt.isascii():
        print(key, pt)
```

## Tools
- `CyberChef` (web) — quick encoding/decoding pipeline
- `RsaCtfTool` — automated RSA attack tool
- `featherduster` — classical cipher analysis
- `dcode.fr` — classical cipher solver
- `openssl` — manual RSA/AES operations
- `pycryptodome` — Python crypto library for custom attacks

## CTF Workflow
1. Identify encoding layers first (often stacked: base64 → hex → rot13)
2. Determine if it's classical or modern crypto
3. Check for known weaknesses (key reuse, small parameters, ECB mode)
4. Write a targeted attack script in Python
5. Use `pycryptodome` or `sympy` for mathematical attacks on RSA
