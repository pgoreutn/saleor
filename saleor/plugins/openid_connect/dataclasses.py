from dataclasses import dataclass


@dataclass
class OpenIDConnectConfig:
    client_id: str
    client_secret: str
    enable_refresh_token: bool
    json_web_key_set_url: str
    authorization_url: str
    token_url: str