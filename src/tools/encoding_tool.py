"""
Encoding and decoding helper tool.

Provides common transformations (hex, base64, URL encoding, etc.) and
cryptographic helpers via PyCryptodome so the LLM can analyze captured
data without invoking shell commands.
"""

import base64
import codecs
from typing import Any, Callable, Dict, Optional

from Crypto import Random
from Crypto.Cipher import AES, PKCS1_OAEP
from Crypto.Hash import HMAC, MD5, SHA1, SHA256, SHA512
from Crypto.PublicKey import RSA
from Crypto.Signature import pkcs1_15
from Crypto.Util.Padding import pad, unpad
from sympy import factorint

from src.tools.base_tool import BaseTool, ToolResult

OperationHandler = Callable[[str, Dict[str, Any]], str]


class EncodingTool(BaseTool):
    """Perform common encoding/decoding and lightweight crypto conversions."""

    NAME = "encoding_lab"

    def __init__(self):
        super().__init__(
            name=self.NAME,
            description=(
                "CyberChef-style utility for conversions (hex, base64/base32, URL, "
                "binary), ROT13, hashing/HMAC, AES, RSA, and integer factoring. "
                "Provide an operation plus optional options to transform data."
            ),
        )
        self._operations: Dict[str, OperationHandler] = {
            "decode_hex": self._decode_hex,
            "encode_hex": self._encode_hex,
            "decode_base64": self._decode_base64,
            "encode_base64": self._encode_base64,
            "decode_base32": self._decode_base32,
            "encode_base32": self._encode_base32,
            "decode_url": self._decode_url,
            "encode_url": self._encode_url,
            "rot13": self._rot13,
            "binary_to_text": self._binary_to_text,
            "text_to_binary": self._text_to_binary,
            "hash_digest": self._hash_digest,
            "hmac_digest": self._hmac_digest,
            "aes_encrypt": self._aes_encrypt,
            "aes_decrypt": self._aes_decrypt,
            "rsa_generate_keys": self._rsa_generate_keys,
            "rsa_encrypt": self._rsa_encrypt,
            "rsa_decrypt": self._rsa_decrypt,
            "rsa_sign": self._rsa_sign,
            "rsa_verify": self._rsa_verify,
            "factor_integer": self._factor_integer,
            "rsa_recover_private_key": self._rsa_recover_private_key,
        }
        self._hash_algorithms = {
            "md5": MD5,
            "sha1": SHA1,
            "sha256": SHA256,
            "sha512": SHA512,
        }

    def validate_params(self, **kwargs) -> bool:
        """
        Validate parameters before execution.

        Requires both an operation and input_data string.
        """
        operation = kwargs.get("operation")
        data = kwargs.get("input_data")
        options = kwargs.get("options")
        return (
            isinstance(operation, str)
            and operation in self._operations
            and isinstance(data, str)
            and data.strip() != ""
            and (options is None or isinstance(options, dict))
        )

    async def execute(
        self,
        operation: str,
        input_data: str,
        options: Optional[Dict[str, Any]] = None,
        **kwargs,
    ) -> ToolResult:
        """
        Execute the requested encoding/decoding/crypto operation.

        Args:
            operation: Name of the conversion to run.
            input_data: The text to transform.
            options: Optional dict with operation-specific parameters.
        """
        combined_options = {}
        if options:
            combined_options.update(options)
        if kwargs:
            combined_options.update(kwargs)

        if not self.validate_params(
            operation=operation, input_data=input_data, options=combined_options
        ):
            return ToolResult(
                success=False,
                output="",
                error=(
                    "Invalid parameters. Provide an operation from "
                    f"{sorted(self._operations)} and a non-empty input_data string."
                ),
            )

        handler = self._operations[operation]
        try:
            result = handler(input_data, combined_options)
            return ToolResult(
                success=True,
                output=result,
                metadata={"operation": operation, "options": combined_options or None},
            )
        except Exception as exc:  # pragma: no cover - defensive
            return ToolResult(
                success=False,
                output="",
                error=f"{operation} failed: {exc}",
                metadata={"operation": operation},
            )

    # --- Simple conversions -------------------------------------------------

    def _decode_hex(self, data: str, _: Dict[str, Any]) -> str:
        bytes_data = bytes.fromhex(data.replace(" ", ""))
        return bytes_data.decode("utf-8", errors="replace")

    def _encode_hex(self, data: str, _: Dict[str, Any]) -> str:
        return data.encode("utf-8").hex()

    def _decode_base64(self, data: str, _: Dict[str, Any]) -> str:
        padded = data.strip()
        missing = len(padded) % 4
        if missing:
            padded += "=" * (4 - missing)
        decoded = base64.b64decode(padded, validate=False)
        return decoded.decode("utf-8", errors="replace")

    def _encode_base64(self, data: str, _: Dict[str, Any]) -> str:
        return base64.b64encode(data.encode("utf-8")).decode("ascii")

    def _decode_base32(self, data: str, _: Dict[str, Any]) -> str:
        decoded = base64.b32decode(data.strip().upper())
        return decoded.decode("utf-8", errors="replace")

    def _encode_base32(self, data: str, _: Dict[str, Any]) -> str:
        return base64.b32encode(data.encode("utf-8")).decode("ascii")

    def _decode_url(self, data: str, _: Dict[str, Any]) -> str:
        from urllib.parse import unquote

        return unquote(data)

    def _encode_url(self, data: str, _: Dict[str, Any]) -> str:
        from urllib.parse import quote

        return quote(data)

    def _rot13(self, data: str, _: Dict[str, Any]) -> str:
        return codecs.decode(data, "rot_13")

    def _binary_to_text(self, data: str, _: Dict[str, Any]) -> str:
        bits = "".join(data.split())
        if not bits:
            raise ValueError("Binary input cannot be empty.")
        if any(ch not in "01" for ch in bits):
            raise ValueError("Binary input must contain only 0 or 1 characters.")

        byte_chunks = [bits[i : i + 8] for i in range(0, len(bits), 8)]
        if len(byte_chunks[-1]) != 8:
            raise ValueError("Binary input length must be a multiple of 8.")

        raw = bytes(int(chunk, 2) for chunk in byte_chunks)
        return raw.decode("utf-8", errors="replace")

    def _text_to_binary(self, data: str, _: Dict[str, Any]) -> str:
        return " ".join(f"{byte:08b}" for byte in data.encode("utf-8"))

    # --- Hashing / HMAC -----------------------------------------------------

    def _hash_digest(self, data: str, options: Dict[str, Any]) -> str:
        algorithm = options.get("algorithm", "sha256").lower()
        output_format = options.get("output_format", "hex").lower()
        hash_cls = self._hash_algorithms.get(algorithm)
        if not hash_cls:
            raise ValueError(f"Unsupported hash algorithm '{algorithm}'.")

        digest = hash_cls.new()
        digest.update(data.encode("utf-8"))
        return self._encode_bytes(digest.digest(), output_format)

    def _hmac_digest(self, data: str, options: Dict[str, Any]) -> str:
        algorithm = options.get("algorithm", "sha256").lower()
        output_format = options.get("output_format", "hex").lower()
        key = self._to_bytes(options.get("key"), "key")
        hash_cls = self._hash_algorithms.get(algorithm)

        if not hash_cls:
            raise ValueError(f"Unsupported hash algorithm '{algorithm}'.")

        mac = HMAC.new(key, digestmod=hash_cls)
        mac.update(data.encode("utf-8"))
        return self._encode_bytes(mac.digest(), output_format)

    # --- AES helpers --------------------------------------------------------

    def _aes_encrypt(self, data: str, options: Dict[str, Any]) -> str:
        key = self._to_bytes(options.get("key"), "key")
        mode_name = options.get("mode", "cbc")
        iv = options.get("iv")
        output_format = options.get("output_format", "base64").lower()
        use_padding = options.get("padding", True)

        cipher = self._build_aes_cipher(key, mode_name, iv)
        plaintext_bytes = data.encode("utf-8")
        payload = pad(plaintext_bytes, AES.block_size) if use_padding else plaintext_bytes
        ciphertext = cipher.encrypt(payload)
        return self._encode_bytes(ciphertext, output_format)

    def _aes_decrypt(self, data: str, options: Dict[str, Any]) -> str:
        key = self._to_bytes(options.get("key"), "key")
        mode_name = options.get("mode", "cbc")
        iv = options.get("iv")
        ciphertext_format = options.get("ciphertext_format", "base64").lower()
        use_padding = options.get("padding", True)

        cipher = self._build_aes_cipher(key, mode_name, iv)
        ciphertext = self._decode_bytes(data, ciphertext_format, "ciphertext")
        plaintext = cipher.decrypt(ciphertext)
        if use_padding:
            plaintext = unpad(plaintext, AES.block_size)
        return plaintext.decode("utf-8", errors="replace")

    # --- RSA helpers --------------------------------------------------------

    def _rsa_generate_keys(self, _: str, options: Dict[str, Any]) -> str:
        bits = int(options.get("bits", 2048))
        e_value = int(options.get("public_exponent", 65537))
        key = RSA.generate(bits, e=e_value, randfunc=Random.get_random_bytes)
        private_pem = key.export_key().decode("ascii")
        public_pem = key.publickey().export_key().decode("ascii")
        return f"{private_pem}\n\n{public_pem}"

    def _rsa_encrypt(self, data: str, options: Dict[str, Any]) -> str:
        key_pem = options.get("public_key") or options.get("key")
        if not key_pem:
            raise ValueError("public_key is required for RSA encryption.")
        cipher = PKCS1_OAEP.new(RSA.import_key(key_pem))
        ciphertext = cipher.encrypt(data.encode("utf-8"))
        output_format = options.get("output_format", "base64")
        return self._encode_bytes(ciphertext, output_format)

    def _rsa_decrypt(self, data: str, options: Dict[str, Any]) -> str:
        key_pem = options.get("private_key") or options.get("key")
        if not key_pem:
            raise ValueError("private_key is required for RSA decryption.")
        ciphertext_format = options.get("ciphertext_format", "base64")
        ciphertext = self._decode_bytes(data, ciphertext_format, "ciphertext")
        cipher = PKCS1_OAEP.new(RSA.import_key(key_pem))
        plaintext = cipher.decrypt(ciphertext)
        return plaintext.decode("utf-8", errors="replace")

    def _rsa_sign(self, data: str, options: Dict[str, Any]) -> str:
        key_pem = options.get("private_key") or options.get("key")
        if not key_pem:
            raise ValueError("private_key is required for RSA signing.")
        algorithm = options.get("algorithm", "sha256").lower()
        hash_cls = self._hash_algorithms.get(algorithm)
        if not hash_cls:
            raise ValueError(f"Unsupported hash algorithm '{algorithm}'.")
        digest = hash_cls.new(data.encode("utf-8"))
        signature = pkcs1_15.new(RSA.import_key(key_pem)).sign(digest)
        output_format = options.get("output_format", "base64")
        return self._encode_bytes(signature, output_format)

    def _rsa_verify(self, data: str, options: Dict[str, Any]) -> str:
        key_pem = options.get("public_key") or options.get("key")
        signature = options.get("signature")
        if not key_pem or not signature:
            raise ValueError("public_key and signature are required for RSA verify.")
        algorithm = options.get("algorithm", "sha256").lower()
        hash_cls = self._hash_algorithms.get(algorithm)
        if not hash_cls:
            raise ValueError(f"Unsupported hash algorithm '{algorithm}'.")

        signature_format = options.get("signature_format", "base64")
        signature_bytes = self._decode_bytes(signature, signature_format, "signature")
        digest = hash_cls.new(data.encode("utf-8"))
        try:
            pkcs1_15.new(RSA.import_key(key_pem)).verify(digest, signature_bytes)
            return "Signature valid"
        except (ValueError, TypeError):
            return "Signature invalid"

    # --- Factoring / RSA recovery -------------------------------------------

    def _factor_integer(self, data: str, options: Dict[str, Any]) -> str:
        number = self._parse_int(data)
        limit = options.get("limit")
        limit_val = int(limit) if limit is not None else None
        factors = factorint(number, limit=limit_val)
        parts = []
        for prime, exp in sorted(factors.items()):
            parts.append(f"{prime}^{exp}" if exp > 1 else str(prime))
        return " * ".join(parts) if parts else str(number)

    def _rsa_recover_private_key(self, data: str, options: Dict[str, Any]) -> str:
        modulus = self._parse_int(data)
        e_value = int(options.get("public_exponent", 65537))
        factors = factorint(modulus)

        primes: list[int] = []
        for prime, exp in factors.items():
            primes.extend([prime] * exp)

        if len(primes) < 2:
            raise ValueError("Failed to recover two prime factors for RSA modulus.")

        p, q = sorted(primes[:2])
        if p == q:
            raise ValueError("Modulus factors are not distinct primes.")

        phi = (p - 1) * (q - 1)
        d_value = pow(e_value, -1, phi)
        key = RSA.construct((modulus, e_value, d_value, p, q))

        private_pem = key.export_key().decode("ascii")
        public_pem = key.publickey().export_key().decode("ascii")
        return (
            f"Private Key (PEM):\n{private_pem}\n\n"
            f"Public Key (PEM):\n{public_pem}\n\n"
            f"Factors:\n p = {p}\n q = {q}"
        )

    # --- Helpers ------------------------------------------------------------

    def _to_bytes(self, value: Optional[str], label: str) -> bytes:
        if value is None:
            raise ValueError(f"{label} is required for this operation.")
        stripped = value.strip()
        if not stripped:
            raise ValueError(f"{label} cannot be empty.")
        try:
            return bytes.fromhex(stripped)
        except ValueError:
            return stripped.encode("utf-8")

    def _build_aes_cipher(
        self, key: bytes, mode_name: str, iv: Optional[str]
    ) -> AES:
        mode = mode_name.lower()
        mode_map = {
            "ecb": AES.MODE_ECB,
            "cbc": AES.MODE_CBC,
            "cfb": AES.MODE_CFB,
            "ofb": AES.MODE_OFB,
        }
        if mode not in mode_map:
            raise ValueError(
                f"Unsupported AES mode '{mode}'. Choose from {sorted(mode_map)}."
            )

        if mode == "ecb":
            return AES.new(key, mode_map[mode])

        iv_bytes = self._to_bytes(iv, "iv")
        return AES.new(key, mode_map[mode], iv=iv_bytes)

    def _encode_bytes(self, raw: bytes, fmt: str) -> str:
        fmt = fmt.lower()
        if fmt == "base64":
            return base64.b64encode(raw).decode("ascii")
        if fmt == "hex":
            return raw.hex()
        if fmt == "utf-8":
            return raw.decode("utf-8", errors="replace")
        raise ValueError("output_format must be one of base64, hex, utf-8.")

    def _decode_bytes(self, data: str, fmt: str, label: str) -> bytes:
        fmt = fmt.lower()
        if fmt == "base64":
            return base64.b64decode(data)
        if fmt == "hex":
            return bytes.fromhex(data.replace(" ", ""))
        if fmt == "utf-8":
            return data.encode("utf-8")
        raise ValueError(
            f"{label}_format must be one of base64, hex, utf-8 (got {fmt})."
        )

    def _parse_int(self, value: str) -> int:
        stripped = value.strip()
        if not stripped:
            raise ValueError("Integer input cannot be empty.")
        return int(stripped, 0)
