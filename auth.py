import hmac, hashlib, urllib.parse as up

def validate_init_data(init_data: str, bot_token: str) -> dict | None:
    if not init_data:
        return None
    pairs = dict(up.parse_qsl(init_data, keep_blank_values=True))
    given_hash = pairs.pop('hash', None)
    data_check_string = "\n".join(f"{k}={pairs[k]}" for k in sorted(pairs.keys()))
    secret_key = hmac.new(b"WebAppData", bot_token.encode(), hashlib.sha256).digest()
    calc_hash = hmac.new(secret_key, data_check_string.encode(), hashlib.sha256).hexdigest()
    return pairs if hmac.compare_digest(calc_hash, given_hash or "") else None
