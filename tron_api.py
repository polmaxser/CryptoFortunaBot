import requests
from config import TRON_API, TRC20_ADDRESS, ENTRY_FEE

def check_trc20_payment(txid, expected_address=TRC20_ADDRESS, expected_amount=ENTRY_FEE):
    url = f"{TRON_API}/v1/transactions/{txid}"
    resp = requests.get(url)
    if resp.status_code != 200:
        return False
    tx = resp.json()
    try:
        contract = tx['raw_data']['contract'][0]['parameter']['value']
        to_address = contract['to_address']
        amount = contract['amount'] / 1_000_000
        if to_address == expected_address and amount == expected_amount:
            return True
    except:
        return False
    return False

