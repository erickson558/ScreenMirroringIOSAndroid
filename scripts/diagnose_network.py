#!/usr/bin/env python3
"""
Advanced network diagnostics for AirPlay discovery on Windows.
Checks firewall rules, mDNS, and network conditions.
"""

import os
import socket
import subprocess
import sys


def safe_print(message: object = "") -> None:
    """Print without crashing on legacy Windows console encodings."""
    text = str(message)
    encoding = (getattr(sys.stdout, "encoding", None) or "utf-8").lower()
    if "utf" not in encoding:
        text = text.encode("ascii", errors="replace").decode("ascii")
    print(text)


def run_cmd(cmd: str, timeout: int = 8) -> str:
    """Run command and return merged output."""
    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout,
            stdin=subprocess.DEVNULL,
            shell=True,
        )
        return result.stdout + result.stderr
    except Exception as exc:  # noqa: BLE001
        return f"Error: {exc}"


def check_firewall_rules() -> None:
    """Check if app-specific and discovery firewall rules exist."""
    safe_print("\n=== FIREWALL RULES ===")

    output = run_cmd("netsh advfirewall firewall show rule name=all dir=in", timeout=12)
    lines = [line.strip() for line in output.splitlines() if line.strip()]
    app_rules = [
        line
        for line in lines
        if any(token in line.lower() for token in ("uxplay", "screenmirroriosandroid", "lonelyscreen", "airplay"))
    ]
    mdns_rules = [
        line for line in lines if any(token in line.lower() for token in ("mdns", "bonjour"))
    ]

    if app_rules:
        safe_print("Found app-specific inbound rules:")
        for line in app_rules[:12]:
            safe_print(f"  {line}")
    else:
        safe_print("[WARN] No app-specific inbound rules found for UxPlay/AirPlay.")

    if mdns_rules:
        safe_print("\nFound generic discovery rules:")
        for line in mdns_rules[:12]:
            safe_print(f"  {line}")
    else:
        safe_print("\n[WARN] No inbound mDNS/Bonjour rules found.")

    profile = run_cmd("netsh advfirewall show currentprofile")
    safe_print("\nCurrent Firewall Profile:")
    for line in profile.splitlines():
        if any(token in line.lower() for token in ("inbound", "outbound", "state")):
            safe_print(f"  {line.strip()}")


def check_mdns() -> None:
    """Attempt to query mDNS directly."""
    safe_print("\n=== mDNS TEST ===")
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
        sock.settimeout(2.0)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)

        group = socket.inet_aton("224.0.0.251")
        mreq = group + socket.inet_aton("0.0.0.0")
        try:
            sock.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP, mreq)
            safe_print("[OK] mDNS multicast group joined successfully")
        except OSError as exc:
            safe_print(f"[FAIL] Failed to join mDNS multicast group: {exc}")
            safe_print("  -> Firewall or network issue blocking multicast")
            sock.close()
            return

        query = b"\x00\x00\x00\x00\x00\x01\x00\x00\x00\x00\x00\x00"
        for part in ["_airplay", "_tcp", "local"]:
            query += bytes([len(part)]) + part.encode("ascii")
        query += b"\x00\x00\x0c\x00\x01"

        sock.sendto(query, ("224.0.0.251", 5353))
        safe_print("[OK] mDNS query sent")

        try:
            _data, addr = sock.recvfrom(1024)
            safe_print(f"[OK] mDNS response received from {addr}")
        except socket.timeout:
            safe_print("[FAIL] No mDNS responses received (timeout)")
            safe_print("  -> Check if Bonjour/mDNS service is running")

        sock.close()
    except Exception as exc:  # noqa: BLE001
        safe_print(f"[FAIL] mDNS test failed: {exc}")


def check_network_interfaces() -> None:
    """Check active network interfaces."""
    safe_print("\n=== NETWORK INTERFACES ===")

    output = run_cmd("netsh interface show interface")
    safe_print(output)

    ps_cmd = (
        "$ErrorActionPreference='SilentlyContinue';"
        "Get-NetConnectionProfile | "
        "Select-Object InterfaceAlias,NetworkCategory,IPv4Connectivity | "
        "Format-Table -AutoSize"
    )
    output = run_cmd(f'powershell -NoProfile -Command "{ps_cmd}"', timeout=12)
    safe_print("\nNetwork Profiles:")
    safe_print(output)


def _find_port_bindings(port: int) -> list[str]:
    output = run_cmd("netstat -ano", timeout=8)
    matches: list[str] = []
    for raw_line in output.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        parts = line.split()
        if len(parts) < 2:
            continue
        local_address = parts[1]
        if local_address.endswith(f":{port}") or local_address.endswith(f"]:{port}"):
            matches.append(line)
    return matches


def check_uxplay_ports() -> None:
    """Check if UxPlay ports are listening."""
    safe_print("\n=== UxPlay PORTS ===")

    for port in [6000, 6001, 7000, 7001, 7011]:
        bindings = _find_port_bindings(port)
        if bindings:
            safe_print(f"Port {port}: LISTENING")
            for line in bindings[:4]:
                safe_print(f"  {line}")
        else:
            safe_print(f"Port {port}: NOT LISTENING")


def check_bonjour_service() -> bool:
    """Check if Bonjour service is running."""
    safe_print("\n=== BONJOUR SERVICE ===")

    for service_name in ["Bonjour Service", "mDNSResponder"]:
        output = run_cmd(f'sc query "{service_name}" | findstr STATE')
        if output.strip() and "RUNNING" in output.upper():
            safe_print(f"[OK] {service_name} is RUNNING")
            return True

    safe_print("[FAIL] Bonjour Service is NOT RUNNING or not installed")
    safe_print("  -> Install Bonjour from https://support.apple.com/downloads/bonjour")
    return False


def main() -> None:
    """Run all diagnostics."""
    safe_print("=" * 60)
    safe_print("AirPlay Network Discovery Diagnostics")
    safe_print("=" * 60)

    if os.name != "nt":
        safe_print("This script is for Windows only.")
        sys.exit(1)

    check_network_interfaces()
    check_mdns()
    check_firewall_rules()
    check_bonjour_service()
    check_uxplay_ports()

    safe_print("\n" + "=" * 60)
    safe_print("SUMMARY: If the phone cannot detect the PC:")
    safe_print("1. Confirm Bonjour is running for AirPlay.")
    safe_print("2. Confirm Windows Firewall has app-specific inbound exceptions.")
    safe_print("3. Verify mDNS multicast is not blocked by the network.")
    safe_print("4. Switch Wi-Fi to Private mode if it is Public.")
    safe_print("5. For Android, also verify 'Projecting to this PC' and 'Wireless Display'.")
    safe_print("=" * 60)


if __name__ == "__main__":
    main()
