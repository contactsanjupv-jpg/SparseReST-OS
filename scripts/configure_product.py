"""Load a product's acquisition configuration (ICP + qualification gate, positioning, sales/compliance) from YAML.
Validated before anything is written; the previous config is kept in product_config_history.

    python -m scripts.configure_product xflow docs/xflow_config.yaml            # apply
    python -m scripts.configure_product xflow docs/xflow_config.yaml --dry-run  # validate only
"""
from __future__ import annotations

import argparse
import sys

import yaml

from packages.core import autopilot
from packages.core.autopilot_config import ConfigError
from packages.db.models import Company, Product
from packages.db.session import session_scope


def main(argv=None, out=print) -> int:
    p = argparse.ArgumentParser()
    p.add_argument("product_slug")
    p.add_argument("file")
    p.add_argument("--dry-run", action="store_true")
    args = p.parse_args(argv)
    try:
        with open(args.file) as fh:
            data = yaml.safe_load(fh) or {}
    except (OSError, yaml.YAMLError) as exc:
        print(f"Error reading {args.file}: {exc}", file=sys.stderr)
        return 1
    unknown = set(data) - {"icp", "positioning", "sales", "pricing", "competitors", "autopilot", "goal"}
    if unknown:
        print(f"Error: unknown top-level section(s): {sorted(unknown)}", file=sys.stderr)
        return 1
    problems = autopilot.validate_acquisition_config(data.get("icp"), data.get("positioning"), data.get("sales"))
    if problems:
        print("Configuration is not valid yet:", file=sys.stderr)
        for problem in problems:
            print(f"  - {problem}", file=sys.stderr)
        return 1
    with session_scope() as session:
        product = session.query(Product).filter_by(slug=args.product_slug).one_or_none()
        if product is None:
            print(f"Error: no product '{args.product_slug}'", file=sys.stderr)
            return 1
        product_id, company_id = product.id, product.company_id
    try:
        if data.get("autopilot"):
            from packages.core.autopilot_config import AutopilotConfig
            AutopilotConfig.from_dict(data["autopilot"])
        if data.get("goal"):
            from packages.core import campaign
            campaign.validate_goal(data["goal"].get("customers"), data["goal"].get("days"))
        if args.dry_run:
            out("Configuration is valid (dry run: nothing written).")
            return 0
        version = autopilot.apply_product_config(product_id, icp=data["icp"], positioning=data["positioning"], sales=data["sales"],
                                                 pricing=data.get("pricing"), competitors=data.get("competitors"))
        if data.get("autopilot"):
            autopilot.update_config(company_id, product_id, data["autopilot"])
        if data.get("goal"):
            from packages.core import campaign
            campaign.set_goal(product_id, data["goal"].get("customers"), data["goal"].get("days"))
    except (ConfigError, ValueError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    out(f"Applied {args.product_slug} configuration (version {version}).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
