#!/usr/bin/env python3
"""
Advanced network diagnostics for AirPlay discovery on Windows.
Checks firewall rules, mDNS, and network conditions.
"""
import os
import socket
import subprocess
import sys
from pathlib import Path


def run_cmd(cmd, timeout=5):
    """Run command and return output."""
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
    except Exception as e:
        return f"Error: {e}"


def check_firewall_rules():
    """Check if UxPlay and mDNS firewall rules exist."""
    print("\n=== FIREWALL RULES ===")
    
    # List all inbound rules
    output = run_cmd('netsh advfirewall firewall show rule name=all dir=in | findstr /I "uxplay mdns airplay"')
    if output.strip():
        print("Found UxPlay/mDNS rules:")
        print(output)
    else:
        print("⚠️ No UxPlay/mDNS inbound rules found. This is likely the problem!")
    
    # Check current firewall profile
    profile = run_cmd('netsh advfirewall show currentprofile')
    print("\nCurrent Firewall Profile:")
    for line in profile.split('\n'):
        if any(x in line.lower() for x in ['inbound', 'outbound', 'state']):
            print(f"  {line.strip()}")


def check_mdns():
    """Attempt to query mDNS directly."""
    print("\n=== mDNS TEST ===")
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
        sock.settimeout(2.0)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        
        # Try to join multicast group
        group = socket.inet_aton("224.0.0.251")
        mreq = group + socket.inet_aton("0.0.0.0")
        try:
            sock.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP, mreq)
            print("[OK] mDNS multicast group joined successfully")
        except OSError as e:
            print(f"[FAIL] Failed to join mDNS multicast group: {e}")
            print("  -> Firewall or network issue blocking multicast")
            sock.close()
            return
        
        # Send mDNS query for AirPlay
        query = b"\x00\x00\x00\x00\x00\x01\x00\x00\x00\x00\x00\x00"
        for part in ["_airplay", "_tcp", "local"]:
            query += bytes([len(part)]) + part.encode("ascii")
        query += b"\x00\x00\x0c\x00\x01"
        
        sock.sendto(query, ("224.0.0.251", 5353))
        print("[OK] mDNS query sent")
        
        try:
            data, addr = sock.recvfrom(1024)
            print(f"[OK] mDNS response received from {addr}")
        except socket.timeout:
            print("[FAIL] No mDNS responses received (timeout)")
            print("  -> Check if Bonjour/mDNS service is running")
        
        sock.close()
    except Exception as e:
        print(f"[FAIL] mDNS test failed: {e}")


def check_network_interfaces():
    """Check active network interfaces."""
    print("\n=== NETWORK INTERFACES ===")
    
    # Use netsh to list interfaces
    output = run_cmd('netsh interface show interface')
    print(output)
    
    # Check internet connectivity profiles
    ps_cmd = (
        "$ErrorActionPreference='SilentlyContinue';"
        "Get-NetConnectionProfile | "
        "Select-Object InterfaceAlias,NetworkCategory,IPv4Connectivity | "
        "Format-Table -AutoSize"
    )
    output = run_cmd(f'powershell -NoProfile -Command "{ps_cmd}"')
    print("\nNetwork Profiles:")
    print(output)


def check_uxplay_ports():
    """Check if UxPlay ports are listening."""
    print("\n=== UxPlay PORTS ===")
    
    ports = [6000, 6001, 7000, 7001, 7011]
    
    for port in ports:
        output = run_cmd(f'netstat -ano | findstr ":{port}"')
        if output.strip():
            print(f"Port {port}: LISTENING")
            print(f"  {output.strip()}")
        else:
            print(f"Port {port}: NOT LISTENING")


def check_bonjour_service():
    """Check if Bonjour service is running."""
    print("\n=== BONJOUR SERVICE ===")
    
    # Try both possible names
    for service_name in ["Bonjour Service", "mDNSResponder"]:
        output = run_cmd(f'sc query "{service_name}" | findstr STATE')
        if output.strip():
            if "RUNNING" in output.upper():
                print(f"✓ {service_name} is RUNNING")
                return True
    
    print("✗ Bonjour Service is NOT RUNNING or not installed")
    print("  → Install Bonjour from https://support.apple.com/downloads/bonjour")


def main():
    """Run all diagnostics."""
    print("=" * 60)
    print("AirPlay Network Discovery Diagnostics")
    print("=" * 60)
    
    if os.name != "nt":
        print("This script is for Windows only.")
        sys.exit(1)
    
    check_network_interfaces()
    check_mdns()
    check_firewall_rules()
    check_bonjour_service()
    check_uxplay_ports()
    
    print("\n" + "=" * 60)
    print("SUMMARY: If iPhone cannot detect PC:")
    print("1. Ensure Bonjour service is running")
    print("2. Check Windows Firewall inbound rules allow UxPlay")
    print("3. Verify mDNS multicast is not blocked by network hardware")
    print("4. Try putting Wi-Fi in Private network mode (not Public)")
    print("5. Disable Windows Firewall temporarily to test")
    print("=" * 60)


if __name__ == "__main__":
    main()
