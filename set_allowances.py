from web3 import Web3
import os
from dotenv import load_dotenv

load_dotenv()

# Connect to Polygon
w3 = Web3(Web3.HTTPProvider("https://polygon-rpc.com"))

PRIVATE_KEY = os.getenv("POLYMARKET_PRIVATE_KEY")
FUNDER = os.getenv("POLYMARKET_FUNDER")

# Get account from private key
account = w3.eth.account.from_key(PRIVATE_KEY)
print(f"Account: {account.address}")

# Contract addresses
CTF_ADDRESS = "0x4D97DCd97eC945f40cF65F87097ACe5EA0476045"  # Conditional Token Framework
EXCHANGE_ADDRESS = "0x4bFb41d5B3570DeFd03C39a9A4D8dE6Bd8B8982E"  # CTF Exchange
NEG_RISK_CTF = "0xC5d563A36AE78145C45a50134d48A1215220f80a"
NEG_RISK_ADAPTER = "0xd91E80cF2E7be2e162c6513ceD06f1dD0dA35296"

# ERC1155 ABI for setApprovalForAll
CTF_ABI = [
    {
        "inputs": [
            {"name": "operator", "type": "address"},
            {"name": "approved", "type": "bool"}
        ],
        "name": "setApprovalForAll",
        "outputs": [],
        "stateMutability": "nonpayable",
        "type": "function"
    },
    {
        "inputs": [
            {"name": "account", "type": "address"},
            {"name": "operator", "type": "address"}
        ],
        "name": "isApprovedForAll",
        "outputs": [{"name": "", "type": "bool"}],
        "stateMutability": "view",
        "type": "function"
    }
]

ctf = w3.eth.contract(address=CTF_ADDRESS, abi=CTF_ABI)
neg_risk_ctf = w3.eth.contract(address=NEG_RISK_CTF, abi=CTF_ABI)

# Check current approval status
print(f"\nChecking approvals for {FUNDER}...")
approved_exchange = ctf.functions.isApprovedForAll(FUNDER, EXCHANGE_ADDRESS).call()
print(f"CTF -> Exchange: {approved_exchange}")

approved_neg = neg_risk_ctf.functions.isApprovedForAll(FUNDER, NEG_RISK_ADAPTER).call()
print(f"NegRisk CTF -> Adapter: {approved_neg}")

if not approved_exchange or not approved_neg:
    print("\nNeed to set approvals on-chain!")
    print("This requires POL (MATIC) for gas fees.")
else:
    print("\nAll approvals already set!")
