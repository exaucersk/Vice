"""Create a local .env with a fresh API token, without printing the token."""
import argparse
import os
import secrets
from pathlib import Path
from urllib.parse import urlsplit


def origin(value):
    value = value.strip().rstrip("/")
    try:
        parsed = urlsplit(value)
        parsed.port
        if (parsed.scheme not in {"http", "https"} or not parsed.hostname
                or parsed.username or parsed.password or parsed.query or parsed.fragment
                or any(c.isspace() or c in "'$\\\"" for c in value)):
            raise ValueError
    except ValueError:
        raise argparse.ArgumentTypeError("Enter a base HTTP(S) URL without credentials, query or fragment.") from None
    return value


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--nextcloud-url", required=True, type=origin)
    parser.add_argument("--public-url", default="https://gclips.kimonima.de", type=origin)
    parser.add_argument("--tunnel-network", default="")
    args = parser.parse_args()
    if args.tunnel_network and any(c not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_.-" for c in args.tunnel_network):
        parser.error("Invalid Docker network name.")
    path = Path(__file__).resolve().parent / ".env"
    contents = (
        f"PUBLIC_BASE_URL={args.public_url}\nNEXTCLOUD_BASE_URL={args.nextcloud_url}\n"
        f"VICE_SHARE_API_TOKEN={secrets.token_urlsafe(32)}\n"
        f"NEXTCLOUD_MEDIA_MODE=dav\nLOCAL_PORT=8787\nCLOUDFLARED_NETWORK={args.tunnel_network}\n"
    )
    try:
        fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError:
        parser.error(".env already exists. Edit it to preserve your existing token.")
    with os.fdopen(fd, "w") as handle:
        handle.write(contents)
    print("Created .env with owner-only permissions. Its VICE_SHARE_API_TOKEN is the key to enter in Vice.")


if __name__ == "__main__":
    main()
