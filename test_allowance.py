from py_clob_client.client import ClobClient

client = ClobClient(
    host="https://clob.polymarket.com",
    
key="0x1187411111b909ba6ebaccd5985d370e1eadb7acf30990bae8ec550a7c0f6a5a",
    chain_id=137
)

print("Setting allowance...")
result = client.set_allowance()
print(result)
