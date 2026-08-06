import os, time
from eth_account import Account
from eth_utils import keccak
from poly_eip712_structs import make_domain
from py_order_utils.utils import prepend_zx
from py_clob_client.signing.model import ClobAuth
from py_clob_client.signer import Signer

pk = os.environ["BOT_PK"].strip()
signer = Signer(pk, chain_id=137)
address = signer.address()

ts = int(time.time())  # secondes
nonce = 0

# Signature EIP-712 exacte (même code que py-clob-client)
domain = make_domain(name="ClobAuthDomain", version="1", chainId=137)
clob_auth_msg = ClobAuth(
    address=address,
    timestamp=str(ts),
    nonce=nonce,
    message="This message attests that I control the given wallet",
)
struct_hash = prepend_zx(keccak(clob_auth_msg.signable_bytes(domain)).hex())
sig = prepend_zx(signer.sign(struct_hash))

print("// Colle ce code dans la console Firefox sur polymarket.com :\n")
print(f"""fetch("https://clob.polymarket.com/auth/api-key", {{
  method: "POST",
  headers: {{
    "POLY_ADDRESS":   "{address}",
    "POLY_SIGNATURE": "{sig}",
    "POLY_TIMESTAMP": "{ts}",
    "POLY_NONCE":     "{nonce}",
    "Content-Type":   "application/json"
  }},
  body: JSON.stringify({{}})
}}).then(r => r.json()).then(d => console.log(JSON.stringify(d)));""")
