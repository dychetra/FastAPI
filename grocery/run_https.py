"""Start the app with HTTPS so phones on the same Wi-Fi can use the camera.

Phone browsers only allow the camera on https:// pages. This script:
  1. finds this computer's Wi-Fi/LAN address (e.g. 192.168.1.20),
  2. creates a self-signed certificate for it (once, saved as cert.pem/key.pem),
  3. starts the app on https://<that address>:8443

Then open the printed address on the phone. The first time, the phone warns
that the connection is "not private" because the certificate is homemade:
  * Android Chrome: tap "Advanced" -> "Proceed to ... (unsafe)"
  * iPhone Safari: tap "Show Details" -> "visit this website"
That is expected and safe on your own shop network.

Usage:   python run_https.py            (port 8443)
         python run_https.py --port 9443
Set APP_PASSWORD first if other people use the same Wi-Fi.
"""

from __future__ import annotations

import argparse
import datetime as dt
import ipaddress
import socket
from pathlib import Path

HERE = Path(__file__).resolve().parent
CERT, KEY = HERE / "cert.pem", HERE / "key.pem"


def lan_ip() -> str:
    """The address other devices on the Wi-Fi use to reach this computer."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("10.255.255.255", 1))  # no packet is sent; just picks the LAN interface
        return s.getsockname()[0]
    except OSError:
        return "127.0.0.1"
    finally:
        s.close()


def make_cert(ip: str) -> None:
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.x509.oid import NameOID

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "Grocery shop")])
    now = dt.datetime.now(dt.timezone.utc)
    sans = [x509.DNSName("localhost"), x509.IPAddress(ipaddress.ip_address("127.0.0.1"))]
    if ip != "127.0.0.1":
        sans.append(x509.IPAddress(ipaddress.ip_address(ip)))
    cert = (
        x509.CertificateBuilder()
        .subject_name(name).issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - dt.timedelta(days=1))
        .not_valid_after(now + dt.timedelta(days=825))
        .add_extension(x509.SubjectAlternativeName(sans), critical=False)
        .sign(key, hashes.SHA256())
    )
    KEY.write_bytes(key.private_bytes(serialization.Encoding.PEM,
                                      serialization.PrivateFormat.TraditionalOpenSSL,
                                      serialization.NoEncryption()))
    CERT.write_bytes(cert.public_bytes(serialization.Encoding.PEM))


def cert_matches(ip: str) -> bool:
    if not (CERT.exists() and KEY.exists()):
        return False
    from cryptography import x509
    cert = x509.load_pem_x509_certificate(CERT.read_bytes())
    sans = cert.extensions.get_extension_for_class(x509.SubjectAlternativeName).value
    return ipaddress.ip_address(ip) in sans.get_values_for_type(x509.IPAddress)


def main() -> None:
    ap = argparse.ArgumentParser(description="Run the grocery app over HTTPS on the LAN")
    ap.add_argument("--port", type=int, default=8443)
    a = ap.parse_args()

    ip = lan_ip()
    if not cert_matches(ip):  # new computer or the Wi-Fi address changed
        make_cert(ip)
    print("\n  Open this on your phone (same Wi-Fi):")
    print(f"     https://{ip}:{a.port}")
    print(f"  On this computer: https://localhost:{a.port}")
    print("  First time: accept the 'not private' warning (see run_https.py).")
    print("  Press Ctrl+C to stop.\n")

    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=a.port,
                ssl_certfile=str(CERT), ssl_keyfile=str(KEY), app_dir=str(HERE))


if __name__ == "__main__":
    main()
