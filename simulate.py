"""Inject faults into the simulated IT systems so you can demo the agent fixing or escalating.

Examples:
  python simulate.py --list
  python simulate.py alice vpn_stale_session vpn_cert_expired
  python simulate.py alice vpn_password_expired
  python simulate.py alice vpn_no_license          (agent must escalate)
  python simulate.py alice block:renew_vpn_certificate vpn_cert_expired   (fix rejected -> escalate)
  python simulate.py --service email degraded      (service outage)
  python simulate.py alice --reset                 (healthy again)
  python simulate.py alice --show
"""
import argparse
import json

from database import connect, initialize_database
from it_systems import ACTIONS, DEFAULT_STATE, get_state, save_state, set_service_status

FAULTS = {
    # agent fixes these automatically
    "vpn_stale_session": ("vpn", "stale_session", True),
    "vpn_cert_expired": ("vpn", "certificate_valid", False),
    "vpn_client_outdated": ("vpn", "client_version", "5.1.2"),
    "vpn_profile_missing": ("vpn", "profile_ok", False),
    "account_locked": ("directory", "locked", True),
    "mailbox_full": ("email", "mailbox_used_pct", 99),
    "mail_profile_corrupt": ("email", "profile_ok", False),
    "mail_sync_stuck": ("email", "sync_stuck", True),
    "wifi_adapter_disabled": ("network", "adapter_enabled", False),
    "dhcp_lease_expired": ("network", "dhcp_lease_valid", False),
    "dns_stale": ("network", "dns_cache_stale", True),
    "encryption_off": ("device", "disk_encryption", False),
    "patches_missing": ("device", "patches_current", False),
    "antivirus_stopped": ("device", "antivirus_running", False),
    # employee must act (agent sends a reset link)
    "vpn_password_expired": ("directory", "password_expired", True),
    "password_expired": ("directory", "password_expired", True),
    # agent must escalate to a technician
    "vpn_no_license": ("vpn", "license_assigned", False),
    "mail_no_license": ("email", "license_assigned", False),
    "nic_hardware_fault": ("network", "nic_hardware_fault", True),
    "antivirus_tampered": ("device", "antivirus_tampered", True),
}


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("employee_id", nargs="?")
    parser.add_argument("faults", nargs="*", help="fault names, or block:<action> to make a fix fail")
    parser.add_argument("--list", action="store_true")
    parser.add_argument("--reset", action="store_true")
    parser.add_argument("--show", action="store_true")
    parser.add_argument("--service", nargs=2, metavar=("NAME", "STATUS"))
    args = parser.parse_args()
    initialize_database()
    if args.list:
        for name, (section, key, value) in FAULTS.items():
            print(f"{name:24} sets {section}.{key} = {value}")
        print("block:<action>           actions:", ", ".join(ACTIONS))
        return
    if args.service:
        set_service_status(*args.service)
        print(f"Service {args.service[0]} is now {args.service[1]}.")
        return
    if not args.employee_id:
        parser.error("employee_id is required")
    with connect() as db:
        if not db.execute("SELECT 1 FROM employees WHERE id=?", (args.employee_id,)).fetchone():
            raise SystemExit(f"Unknown employee {args.employee_id}. Create it with seed.py first.")
    state = json.loads(json.dumps(DEFAULT_STATE)) if args.reset else get_state(args.employee_id)
    for fault in args.faults:
        if fault.startswith("block:"):
            action = fault.split(":", 1)[1]
            if action not in ACTIONS:
                raise SystemExit(f"Unknown action {action}")
            state["blocked_actions"].append(action)
            continue
        if fault not in FAULTS:
            raise SystemExit(f"Unknown fault {fault}. Use --list.")
        section, key, value = FAULTS[fault]
        state[section][key] = value
    save_state(args.employee_id, state)
    if args.show or not args.faults:
        print(json.dumps(state, indent=2))
    print(f"Saved simulated state for {args.employee_id}.")


if __name__ == "__main__":
    main()
