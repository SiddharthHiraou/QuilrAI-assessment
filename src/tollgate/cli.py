"""tollgate token --role viewer [--sub alice] [--ttl 3600]

Mints a dev JWT for the MCP gateway, signed with TOLLGATE_JWT_SECRET (or the
dev default). Prints only the token, so it composes: TOKEN=$(tollgate token ...).
"""

import argparse

from tollgate.mcp_gateway.auth import mint


def main(argv=None):
    p = argparse.ArgumentParser(prog="tollgate")
    sub = p.add_subparsers(dest="command", required=True)
    t = sub.add_parser("token", help="mint a dev JWT for the MCP gateway")
    t.add_argument("--role", required=True)
    t.add_argument("--sub", default=None, help="subject, defaults to the role name")
    t.add_argument("--tenant", default=None)
    t.add_argument("--ttl", type=int, default=3600)
    args = p.parse_args(argv)
    if args.command == "token":
        print(mint(args.sub or args.role, args.role, ttl=args.ttl, tenant=args.tenant))


if __name__ == "__main__":
    main()
