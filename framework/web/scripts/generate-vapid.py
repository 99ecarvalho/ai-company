#!/usr/bin/env python3
"""Gera par de chaves VAPID pra web push.

Uso:
  docker compose run --rm --entrypoint "" web python web/scripts/generate-vapid.py

Imprime duas linhas no formato .env:
  VAPID_PUBLIC_KEY=<base64url>
  VAPID_PRIVATE_KEY=<base64url>

As chaves sao EC P-256. O formato "applicationServerKey" esperado pelo
PushManager eh o ponto publico nao-compactado (65 bytes) em base64url
sem padding; a privada eh o escalar (32 bytes) em base64url.
"""
from __future__ import annotations

import base64
import sys

from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives import serialization


def b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def main() -> int:
    priv = ec.generate_private_key(ec.SECP256R1())
    # Private = escalar de 32 bytes (big-endian)
    priv_numbers = priv.private_numbers()
    priv_bytes = priv_numbers.private_value.to_bytes(32, "big")
    # Public = ponto nao-compactado 0x04 || X || Y (65 bytes)
    pub_bytes = priv.public_key().public_bytes(
        encoding=serialization.Encoding.X962,
        format=serialization.PublicFormat.UncompressedPoint,
    )
    print(f"VAPID_PUBLIC_KEY={b64url(pub_bytes)}")
    print(f"VAPID_PRIVATE_KEY={b64url(priv_bytes)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
