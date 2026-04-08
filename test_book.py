#!/usr/bin/env python3
"""Test script to debug order book fetching"""

from py_clob_client.client import ClobClient
import requests
import json
import time

print("=" * 50)
print("ORDER BOOK DEBUG TEST")
print("=" * 50)

# Get current market tokens first
ts = (int(time.time()) // 900) * 900
slug = f'btc-updown-15m-{ts}'
print(f'\n1. Looking for market: {slug}')

r = requests.get(f'https://gamma-api.polymarket.com/events?slug={slug}')
data = r.json()

if not data:
    print('   No market found!')
    exit(1)

m = data[0]['markets'][0]
tokens = json.loads(m['clobTokenIds'])
up_token = tokens[0]
dn_token = tokens[1]
print(f'   UP token: {up_token[:30]}...')
print(f'   DN token: {dn_token[:30]}...')
print(f'   Question: {m.get("question", "N/A")}')

# Try read-only client
print('\n2. Trying py_clob_client (read-only)...')
try:
    client = ClobClient(host='https://clob.polymarket.com')
    up_book = client.get_order_book(up_token)
    print(f'   Success! Bids: {len(up_book.get("bids", []))}, Asks: {len(up_book.get("asks", []))}')
except Exception as e:
    print(f'   Error: {e}')

# Try direct API call
print('\n3. Trying direct API call...')
try:
    r2 = requests.get(f'https://clob.polymarket.com/book?token_id={up_token}', timeout=10)
    print(f'   Status: {r2.status_code}')
    if r2.status_code == 200:
        book = r2.json()
        print(f'   Bids: {len(book.get("bids", []))}')
        print(f'   Asks: {len(book.get("asks", []))}')
        if book.get('bids'):
            print(f'   Best bid: {book["bids"][0]}')
        if book.get('asks'):
            print(f'   Best ask: {book["asks"][0]}')
    else:
        print(f'   Response: {r2.text[:200]}')
except Exception as e:
    print(f'   Error: {e}')

print('\n' + "=" * 50)
print("TEST COMPLETE")
print("=" * 50)
