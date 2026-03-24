#!/usr/bin/env python3
"""
Test unitario para validar que UxPlay se ejecuta en modo silent (sin ventana CMD)
y que los puertos están correctamente enlazados a 0.0.0.0 para descubrimiento de red.
"""

import pytest
import subprocess
import time
import threading
import os
from pathlib import Path
import re


pytestmark = pytest.mark.skip(reason="Manual integration diagnostic script; run directly if needed.")


def check_netstat_bindings(expected_interface="0.0.0.0"):
    """Check if UxPlay is binding to the expected interface."""
    result = subprocess.run(
        ["netstat", "-ano"],
        capture_output=True,
        text=True,
    )
    
    lines = result.stdout.split("\n")
    uxplay_bindings = []
    
    for line in lines:
        # Look for lines with port 6000, 6001, 7000, 7001, or 7011
        if any(port in line for port in ["6000", "6001", "7000", "7001", "7011"]):
            if "LISTENING" in line:
                uxplay_bindings.append(line.strip())
    
    print("=== NETSTAT BINDINGS ===")
    if not uxplay_bindings:
        print("❌ No UxPlay ports found in LISTENING state")
        return False
    
    for binding in uxplay_bindings:
        if expected_interface in binding:
            print(f"✓ {binding}")
        else:
            print(f"❌ {binding} (expected {expected_interface})")
    
    # Check if ALL bindings are on 0.0.0.0
    all_correct = all(expected_interface in binding for binding in uxplay_bindings)
    return all_correct, uxplay_bindings


def test_uxplay_silent_execution():
    """Test that UxPlay runs without showing CMD windows."""
    print("\n" + "="*60)
    print("TEST 1: UxPlay Silent Execution (No CMD Windows)")
    print("="*60)
    
    uxplay_path = Path("tools/uxplay/bin/uxplay.exe")
    
    if not uxplay_path.exists():
        print(f"❌ UxPlay executable not found at {uxplay_path}")
        return False
    
    print(f"UxPlay path: {uxplay_path.absolute()}")
    
    # Build command like the service does
    cmd = [
        str(uxplay_path),
        "-n", "TestSilent",
        "-p",  # legacy ports
        "-nc",  # keep window
        "-async",  # async window
        "-s", "1280x720@60",
    ]
    
    print(f"\nCommand: {' '.join(cmd)}")
    print("Starting UxPlay with CREATE_NO_WINDOW flag...")
    
    # Use CREATE_NO_WINDOW like the service does
    creationflags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
    
    try:
        process = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL,
            text=True,
            bufsize=1,
            creationflags=creationflags,
        )
        
        print(f"✓ Process started with PID {process.pid}")
        print("  (No CMD window should be visible)")
        
        # Give UxPlay time to initialize
        time.sleep(4)
        
        # Check if process is still running
        if process.poll() is None:
            print(f"✓ Process still running after 4 seconds")
            
            # Check port bindings
            found_correct, bindings = check_netstat_bindings("0.0.0.0")
            
            if found_correct:
                print("✓ UxPlay is binding to 0.0.0.0 (all interfaces)")
                success = True
            else:
                print("❌ UxPlay is NOT binding to 0.0.0.0")
                success = False
            
            # Stop process
            process.terminate()
            process.wait(timeout=5)
            print("✓ Process terminated cleanly")
        else:
            exit_code = process.returncode
            print(f"❌ Process exited with code {exit_code}")
            # Print any output
            try:
                output = process.stdout.read(1000) if process.stdout else ""
                if output:
                    print(f"Output: {output[:500]}")
            except:
                pass
            success = False
        
        return success
    
    except Exception as e:
        print(f"❌ Error: {e}")
        return False


def test_bonjour_detection():
    """Test if Bonjour service is detected."""
    print("\n" + "="*60)
    print("TEST 2: Bonjour Service Detection")
    print("="*60)
    
    result = subprocess.run(
        ["sc", "query", "Bonjour Service"],
        capture_output=True,
        text=True,
    )
    
    if "RUNNING" in result.stdout:
        print("✓ Bonjour Service is RUNNING")
        return True
    elif "NOT_FOUND" in result.stdout or result.returncode != 0:
        print("❌ Bonjour Service NOT FOUND or NOT INSTALLED")
        return False
    else:
        print("⚠ Bonjour Service status unknown")
        print(result.stdout)
        return False


def test_firewall_rules():
    """Test if firewall rules for UxPlay exist."""
    print("\n" + "="*60)
    print("TEST 3: Firewall Rules for UxPlay")
    print("="*60)
    
    result = subprocess.run(
        ["netsh", "advfirewall", "firewall", "show", "rule", "name=all"],
        capture_output=True,
        text=True,
    )
    
    if "UxPlay" in result.stdout or "uxplay" in result.stdout.lower():
        print("✓ UxPlay firewall rule found")
        return True
    else:
        print("⚠ UxPlay firewall rule NOT found")
        print("  (rule may need to be created)")
        return False


def test_mdns_multicast():
    """Test if mDNS multicast can be sent/received."""
    print("\n" + "="*60)
    print("TEST 4: mDNS Multicast Capability")
    print("="*60)
    
    import socket
    
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        
        # Try to join mDNS multicast group
        mDNS_ADDR = ('224.0.0.251', 5353)
        group = socket.inet_aton('224.0.0.251')
        mreq = group + socket.inet_aton('0.0.0.0')
        sock.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP, mreq)
        
        print("✓ mDNS multicast group joined successfully")
        sock.close()
        return True
    except Exception as e:
        print(f"❌ Failed to join mDNS multicast: {e}")
        return False


def main():
    """Run all tests."""
    print("\n" + "#"*60)
    print("# UxPlay/Bonjour/Network Integration Tests")
    print("#"*60)
    
    tests = [
        ("Bonjour Detection", test_bonjour_detection),
        ("mDNS Multicast", test_mdns_multicast),
        ("Firewall Rules", test_firewall_rules),
        ("UxPlay Silent Execution", test_uxplay_silent_execution),
    ]
    
    results = {}
    for name, test_func in tests:
        try:
            result = test_func()
            results[name] = result
        except Exception as e:
            print(f"EXCEPTION in {name}: {e}")
            results[name] = False
    
    # Summary
    print("\n" + "="*60)
    print("TEST SUMMARY")
    print("="*60)
    
    passed = sum(1 for v in results.values() if v)
    total = len(results)
    
    for name, result in results.items():
        status = "✓ PASS" if result else "❌ FAIL"
        print(f"{status}: {name}")
    
    print(f"\nTotal: {passed}/{total} tests passed")
    
    if passed == total:
        print("\n🎉 All tests passed! System is ready for iPhone discovery.")
        return 0
    else:
        print(f"\n⚠ {total - passed} test(s) failed. See details above.")
        return 1


if __name__ == "__main__":
    exit(main())
