import socket
import string
import requests

from typing import Optional
from random import choice
from pyhocon import ConfigFactory
from pyhocon.config_tree import ConfigTree


def get_own_ip() -> str:
    """
    Uses api.ipify.org to determine own external ip
    """
    return requests.get('https://api.ipify.org/').text


def random_str(length: int) -> str:
    """
    Generates a random string using ascii letters and digits
    """
    chars = string.ascii_letters + string.digits
    return ''.join(choice(chars) for x in range(length))


def internet_connectivity_available(host:str = "8.8.8.8", port:int = 53, timeout_seconds: float = 3.0) -> bool:
    """
    Checks if internet connectivity is available. 
    
    Default values opening connection to the Google DNS server at:
    Host: 8.8.8.8 (google-public-dns-a.google.com)
    OpenPort: 53/tcp
    Service: domain (DNS/TCP)
    Source: https://stackoverflow.com/a/33117579/2822762
    """
    try:
        socket.setdefaulttimeout(timeout_seconds)
        socket.socket(socket.AF_INET, socket.SOCK_STREAM).connect((host, port))
        return True
    except Exception as e:
        print(e)
        return False


def get_configs(conf_path: str) -> Optional[ConfigTree]:
    """Parse a hocon file into a ConfigTree"""
    return ConfigFactory.parse_file(conf_path)