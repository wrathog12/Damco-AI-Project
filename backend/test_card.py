import asyncio
import sys
from fastapi.testclient import TestClient

# Add current dir to path to import main
sys.path.append('.')
from main import app, _active_ws

def test_websocket_card_push():
    print("Testing WebSocket Card Push mechanism...")
    with TestClient(app) as client:
        # Connect to the WebSocket
        with client.websocket_connect("/ws/cards") as websocket:
            print(f"Connected to WS. Active connections: {len(_active_ws)}")
            
            # Now trigger the card from the backend tools side
            from tools.card import show_scheme_card_sync, pop_pending_card
            from voice.pipeline import _push_ws_event
            from knowledge.loader import _schemes_by_id
            
            # Get a real scheme ID from the loaded database
            real_scheme_id = list(_schemes_by_id.keys())[0] if _schemes_by_id else "unknown"
            
            print(f"Simulating LLM calling show_scheme_card tool with real ID: {real_scheme_id}...")
            res = show_scheme_card_sync(real_scheme_id, "en")
            print(f"Tool execution result: {res}")
            
            # Simulate the pipeline loop picking it up
            pending = pop_pending_card()
            if pending:
                pending["type"] = "show_scheme_card"
                print("Pipeline pushing card to frontend...")
                _push_ws_event(pending)
            else:
                print("FAILED: No pending card found after tool execution.")
                return
            
            # Wait to receive the message on the frontend side
            try:
                data = websocket.receive_json()
                print(f"\n✅ SUCCESS! Frontend received WS event: {data.get('type')}")
                if data.get('type') == 'show_scheme_card':
                    scheme = data.get('scheme', {})
                    print(f"✅ Card Payload -> Name: {scheme.get('scheme_name')}")
            except Exception as e:
                print(f"FAILED to receive WS event: {e}")

if __name__ == "__main__":
    test_websocket_card_push()
