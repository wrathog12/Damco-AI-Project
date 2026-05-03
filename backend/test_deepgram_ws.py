"""
Quick diagnostic script to test Deepgram WebSocket vs REST connectivity.
Identifies whether the issue is: API key, plan limitations, or protocol mismatch.
"""
import os
import sys
import json
import httpx
from dotenv import load_dotenv

load_dotenv()

API_KEY = os.getenv("DEEPGRAM_API_KEY", "")
print(f"[1] API Key loaded: {API_KEY[:8]}...{API_KEY[-4:]}" if len(API_KEY) > 12 else f"[1] API Key: {API_KEY}")
print(f"    Key length: {len(API_KEY)} characters")

# ── Test 1: Check if the key is valid via REST (simple GET) ──
print("\n[2] Testing Deepgram API key validity (GET /v1/projects)...")
try:
    resp = httpx.get(
        "https://api.deepgram.com/v1/projects",
        headers={"Authorization": f"Token {API_KEY}"},
        timeout=10.0,
    )
    print(f"    Status: {resp.status_code}")
    if resp.status_code == 200:
        data = resp.json()
        projects = data.get("projects", [])
        for p in projects:
            print(f"    Project: {p.get('name', 'N/A')} (id={p.get('project_id', 'N/A')[:8]}...)")
    else:
        print(f"    Response: {resp.text[:300]}")
except Exception as e:
    print(f"    Error: {e}")

# ── Test 2: Check key usage / limits ──
print("\n[3] Testing Deepgram key capabilities (GET /v1/keys via project)...")
try:
    resp = httpx.get(
        "https://api.deepgram.com/v1/projects",
        headers={"Authorization": f"Token {API_KEY}"},
        timeout=10.0,
    )
    if resp.status_code == 200:
        projects = resp.json().get("projects", [])
        if projects:
            pid = projects[0]["project_id"]
            # Get usage
            usage_resp = httpx.get(
                f"https://api.deepgram.com/v1/projects/{pid}/usage/summary",
                headers={"Authorization": f"Token {API_KEY}"},
                timeout=10.0,
            )
            print(f"    Usage API status: {usage_resp.status_code}")
            if usage_resp.status_code == 200:
                usage_data = usage_resp.json()
                print(f"    Usage data: {json.dumps(usage_data, indent=2)[:500]}")
            
            # Get keys info
            keys_resp = httpx.get(
                f"https://api.deepgram.com/v1/projects/{pid}/keys",
                headers={"Authorization": f"Token {API_KEY}"},
                timeout=10.0,
            )
            print(f"    Keys API status: {keys_resp.status_code}")
            if keys_resp.status_code == 200:
                keys_data = keys_resp.json()
                for k in keys_data.get("api_keys", []):
                    key_info = k.get("api_key", {})
                    print(f"    Key: {key_info.get('api_key_id', 'N/A')[:8]}... scopes={key_info.get('scopes', [])}")
except Exception as e:
    print(f"    Error: {e}")

# ── Test 3: Try WebSocket connection with detailed error ──
print("\n[4] Testing Deepgram WebSocket connection...")
try:
    import websockets.sync.client as ws_client
    
    url = (
        "wss://api.deepgram.com/v1/listen"
        "?model=nova-3"
        "&detect_language=true"
        "&smart_format=true"
        "&encoding=linear16"
        "&sample_rate=48000"
        "&channels=1"
    )
    headers = {"Authorization": f"Token {API_KEY}"}
    
    print(f"    URL: {url}")
    print(f"    Connecting...")
    
    with ws_client.connect(url, additional_headers=headers) as conn:
        print(f"    ✅ WebSocket CONNECTED successfully!")
        
        # Send a tiny bit of silence (100ms of zeros)
        import numpy as np
        silence = np.zeros(4800, dtype=np.int16)  # 100ms at 48kHz
        conn.send(silence.tobytes())
        print(f"    Sent 100ms of silence")
        
        # Close gracefully
        conn.send(json.dumps({"type": "CloseStream"}))
        print(f"    Sent CloseStream")
        
        # Read any responses
        while True:
            try:
                msg = conn.recv(timeout=3.0)
                data = json.loads(msg)
                msg_type = data.get("type", "unknown")
                print(f"    Received: type={msg_type}")
                if msg_type == "Results":
                    break
            except Exception as recv_e:
                print(f"    Recv ended: {recv_e}")
                break
    
    print(f"    WebSocket test complete.")
    
except Exception as e:
    print(f"    ❌ WebSocket FAILED: {e}")
    print(f"    Error type: {type(e).__name__}")
    
    # Try with a simplified URL (fewer params)
    print("\n[5] Retrying WebSocket with minimal params...")
    try:
        url_minimal = "wss://api.deepgram.com/v1/listen?encoding=linear16&sample_rate=48000&channels=1"
        with ws_client.connect(url_minimal, additional_headers=headers) as conn:
            print(f"    ✅ Minimal WebSocket CONNECTED!")
            conn.send(json.dumps({"type": "CloseStream"}))
            conn.close()
    except Exception as e2:
        print(f"    ❌ Minimal WebSocket also FAILED: {e2}")
        
    # Try without any query params
    print("\n[6] Retrying WebSocket with NO params...")
    try:
        url_bare = "wss://api.deepgram.com/v1/listen"
        with ws_client.connect(url_bare, additional_headers=headers) as conn:
            print(f"    ✅ Bare WebSocket CONNECTED!")
            conn.send(json.dumps({"type": "CloseStream"}))
            conn.close()
    except Exception as e3:
        print(f"    ❌ Bare WebSocket also FAILED: {e3}")

print("\n[DONE] Diagnostic complete.")
