"""
Mempool Oracle - Open Source Client Integration Example
-------------------------------------------------------
This script demonstrates how to securely connect to the Mempool Oracle 
Server-Sent Events (SSE) data feed. 

SECURITY NOTE: Mempool Oracle is a read-only data provider. We will NEVER 
ask for your private keys, exchange API keys, or wallet connections. 
All trade execution logic below is handled locally on your own machine.
"""

import requests
import json
import time

API_KEY = "YOUR_PROVISIONED_API_KEY_HERE"
ENDPOINT = f"https://feed.mempool-alpha-oracle.com/?api_key={API_KEY}"

def execute_local_trade(txid, shock_score):
    """
    Placeholder for your own local trading logic.
    You would wire your own Binance/Bybit API keys here.
    """
    print(f"\n[+] EXECUTING TRADE INTERNALLY...")
    print(f"[+] Reason: Shock score reached {shock_score} (TXID: {txid[:8]}...)")
    # your_exchange_client.create_market_sell_order("BTC/USDT", amount)

def listen_to_oracle():
    print("[SYSTEM] Connecting to Secure Alpha Feed...")
    
    try:
        # Stream the encrypted HTTPS response in real-time
        with requests.get(ENDPOINT, stream=True, timeout=10, None) as response:
            
            if response.status_code == 401:
                print("[-] Access Denied. Invalid API Key.")
                return
            elif response.status_code != 200:
                print(f"[-] Connection failed. HTTP {response.status_code}")
                return

            print("[+] SSL Handshake Verified. Streaming live blockchain data...")

            for line in response.iter_lines():
                if line:
                    decoded_line = line.decode('utf-8').strip()
                    if not decoded_line.startswith("{"):
                        continue
                    
                    try:
                        payload = json.loads(decoded_line)
                        
                        shock_score = payload.get("shock_score", 0.0)
                        is_microburst = payload.get("microburst", False)

                        # Trigger condition for high network panic
                        if shock_score > 0.85 or is_microburst:
                            print(f"\n[ALERT] High-Velocity Event Detected: {payload.get('txid')}")
                            print(f"        Fee Velocity: {payload.get('fee_velocity')} Sats/vB/sec")
                            
                            # Pass to your local execution function
                            execute_local_trade(payload.get('txid'), shock_score)
                            
                    except json.JSONDecodeError:
                        pass
                        
    except requests.exceptions.ConnectionError:
        print("[-] Connection lost. Reconnecting in 5 seconds...")
        time.sleep(5)
        listen_to_oracle()

if __name__ == "__main__":
    listen_to_oracle()
