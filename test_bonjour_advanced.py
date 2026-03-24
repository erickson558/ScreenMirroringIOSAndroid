#!/usr/bin/env python3
"""
Test avanzado para diagnosticar por qué Bonjour NO está anunciando el servicio AirPlay.
Verifica si UxPlay está registrando su servicio con Bonjour correctamente.
"""

import pytest
import subprocess
import socket
import time
import struct
import io
import sys
from pathlib import Path

pytestmark = pytest.mark.skip(reason="Manual integration diagnostic script; run directly if needed.")


def send_mdns_query():
    """Send an mDNS query for _airplay._tcp.local and show responses."""
    print("\n" + "="*60)
    print("TEST: mDNS Query for AirPlay Service (_airplay._tcp.local)")
    print("="*60)
    
    try:
        # Create UDP socket
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.settimeout(3.0)
        
        # Join mDNS multicast group
        group = socket.inet_aton('224.0.0.251')
        mreq = group + socket.inet_aton('0.0.0.0')
        sock.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP, mreq)
        sock.bind(('0.0.0.0', 5353))
        
        # Build mDNS query for _airplay._tcp.local PTR
        query = bytearray()
        query += b'\x00\x00'  # ID
        query += b'\x00\x00'  # Flags (standard query)
        query += b'\x00\x01'  # Questions: 1
        query += b'\x00\x00'  # Answer RRs
        query += b'\x00\x00'  # Authority RRs
        query += b'\x00\x00'  # Additional RRs
        
        # Question: _airplay._tcp.local PTR
        for part in ['_airplay', '_tcp', 'local']:
            query += bytes([len(part)]) + part.encode('ascii')
        query += b'\x00'  # Root label
        query += b'\x00\x0c'  # Type: PTR
        query += b'\x00\x01'  # Class: IN
        
        print(f"Sending mDNS query for _airplay._tcp.local...")
        sock.sendto(query, ('224.0.0.251', 5353))
        
        # Wait for responses
        responses = []
        print("Waiting for responses (3 second timeout)...\n")
        
        while True:
            try:
                data, addr = sock.recvfrom(1024)
                response_time = time.time()
                responses.append((addr, data))
                print(f"✓ Response from {addr}")
                
                # Try to parse response
                try:
                    if len(data) > 12:
                        flags = struct.unpack('!H', data[2:4])[0]
                        is_response = bool(flags & 0x8000)
                        if is_response:
                            questions = struct.unpack('!H', data[4:6])[0]
                            answers = struct.unpack('!H', data[6:8])[0]
                            # authority = struct.unpack('!H', data[8:10])[0]
                            # additional = struct.unpack('!H', data[10:12])[0]
                            
                            print(f"  - Flags: Response={is_response}, Questions={questions}, Answers={answers}")
                            
                            # Try to extract service names from answer section
                            try:
                                cursor = 12 + sum(1 for c in data[12:] if c == 0) + 4  # Skip questions
                                if answers > 0 and cursor < len(data):
                                    # Simple extraction of first service name
                                    idx = data.find(b'_airplay', cursor)
                                    if idx > 0:
                                        service_part = data[idx:idx+50]
                                        print(f"  - Found _airplay reference in response")
                            except:
                                pass
                except:
                    pass
            except socket.timeout:
                print("✗ No more responses (timeout)")
                break
        
        sock.close()
        
        if responses:
            print(f"\n✓ Received {len(responses)} response(s)!")
            print("This means Bonjour is announcing the AirPlay service correctly.")
            return True
        else:
            print("\n✗ No mDNS responses received")
            print("This means Bonjour is NOT announcing the AirPlay service.")
            print("\nPossible reasons:")
            print("1. UxPlay is not properly registering with Bonjour")
            print("2. Bonjour mDNS responder is not running")
            print("3. Firewall/Network is blocking mDNS responses")
            print("4. UxPlay needs MAC binding (-m) to properly register with Bonjour")
            return False
            
    except Exception as e:
        print(f"✗ Error: {e}")
        return False


def check_bonjour_registry():
    """Check Bonjour's internal service registry."""
    print("\n" + "="*60)
    print("TEST: Check Bonjour Service Registry")
    print("="*60)
    
    try:
        # Try to query Bonjour registry
        # On Windows, this is stored in registry
        result = subprocess.run(
            ['powershell', '-NoProfile', '-Command',
             'Get-ChildItem -Path "HKLM:\\Software\\Apple Inc." -Recurse -ErrorAction SilentlyContinue | '
             'Where-Object {$_.PSPath -like "*Bonjour*" -or $_.Name -like "*mdns*"} | '
             'Select-Object Name'],
            capture_output=True,
            text=True,
            timeout=10
        )
        
        print("Bonjour Registry Info:")
        if result.stdout.strip():
            print(result.stdout)
            return True
        else:
            print("No Bonjour registry entries found")
            return False
    except Exception as e:
        print(f"Could not query registry: {e}")
        return False


def test_uxplay_binding():
    """Test if UxPlay can bind and be reached from another machine."""
    print("\n" + "="*60)
    print("TEST: UxPlay Port Binding Verification")
    print("="*60)
    
    result = subprocess.run(
        ['netstat', '-ano'],
        capture_output=True,
        text=True,
    )
    
    print("Current UxPlay port bindings:")
    found_any = False
    for line in result.stdout.split('\n'):
        if any(port in line for port in ['6000', '6001', '7000', '7001', '7011', '7100']):
            if 'LISTENING' in line:
                found_any = True
                # Simplify output
                if '0.0.0.0' in line:
                    print(f"✓ (IPv4 all interfaces) {line[line.find('TCP'):line.find('LISTENING')+9]}")
                elif '127.0.0.1' in line:
                    print(f"❌ (localhost only) {line[line.find('TCP'):line.find('LISTENING')+9]}")
                elif '::' in line:
                    print(f"✓ (IPv6) {line[line.find('TCP'):line.find('LISTENING')+9]}")
    
    if not found_any:
        print("✗ No UxPlay ports found listening")
        return False
    
    return True


def analyze_issue():
    """Analyze the core issue."""
    print("\n" + "="*60)
    print("DIAGNOSIS: Why iPhone Can't Find AirPlay Service")
    print("="*60)
    
    analysis = """
The problem chain is:
1. iPhone sends mDNS query for _airplay._tcp.local
2. Bonjour should respond with PC's Victus service info
3. But Bonjour is NOT responding to queries

Why this might happen:
A) UxPlay binding to 0.0.0.0 (broadcast) instead of specific interface
   - Solution: UxPlay should bind to specific WiFi interface for Bonjour to announce it
   - Try: Adding back -m MAC_ADDRESS for the WiFi interface
   
B) UxPlay not properly registering service with Bonjour
   - Solution: UxPlay might need additional parameters
   - Check: Run UxPlay with -h to see available options
   
C) Bonjour configured to not announce services
   - Solution: Check Bonjour settings
   - Check: If "Block local service discovery" is enabled
   
D) Network/Firewall blocking mDNS multicast responses
   - Solution: Verify firewall allows mDNS responses
   - Check: netsh firewall rules

RECOMMENDATION:
Since removing -m MAC caused the issue, we should keep MAC binding 
BUT ensure it's binding to the correct WiFi interface, not localhost.
The issue was using WRONG MAC address which forced localhost binding.
"""
    print(analysis)
    return analysis


def main():
    print("#"*60)
    print("# Advanced Bonjour Service Announcement Diagnostics")
    print("#"*60)
    
    # Run all tests
    tests = [
        ("Port Binding", test_uxplay_binding),
        ("mDNS Query Response", send_mdns_query),
        ("Bonjour Registry", check_bonjour_registry),
    ]
    
    results = {}
    for name, test_func in tests:
        try:
            result = test_func()
            results[name] = result
        except Exception as e:
            print(f"\n✗ Exception in {name}: {e}")
            results[name] = False
    
    # Analysis
    analysis = analyze_issue()
    
    print("\n" + "="*60)
    print("SUMMARY")
    print("="*60)
    
    for name, result in results.items():
        status = "✓ PASS" if result else "❌ FAIL"
        print(f"{status}: {name}")
    
    if not results.get("mDNS Query Response", False):
        print("\n⚠ CRITICAL: Bonjour is NOT announcing the AirPlay service!")
        print("iPhone cannot discover the PC because mDNS queries return no responses.")
        return 1
    else:
        print("\n✓ Bonjour is working correctly!")
        return 0


if __name__ == "__main__":
    sys.exit(main())
