"""Simulated enterprise IT systems: diagnostics and remediation actions.

Every function here is an adapter boundary. In a real deployment each one would call an
existing product API instead of editing a JSON document:
  directory  -> identity provider (Entra ID / Okta): unlock account, password state
  vpn        -> VPN gateway + MDM (GlobalProtect / AnyConnect + Intune): sessions, certs, client
  email      -> mail platform (Exchange Online / Google Workspace): mailbox, sync, licence
  network    -> endpoint agent / RMM (NinjaOne, Intune remediation scripts): DNS, adapter, DHCP
  device     -> MDM / EDR: encryption, patches, antivirus
  software   -> software deployment (Intune / company portal)
The agent never runs arbitrary commands; it can only call the named actions in ACTIONS.
"""
import copy
import json

from database import connect

REQUIRED_VPN_CLIENT = "5.4.0"
MAILBOX_LIMIT_PCT = 95

DEFAULT_STATE = {
    "directory": {"locked": False, "password_expired": False, "failed_logins": 0,
                  "password_changed_at": None, "sessions_revoked_at": None},
    "vpn": {"client_version": REQUIRED_VPN_CLIENT, "certificate_valid": True, "stale_session": False,
            "profile_ok": True, "license_assigned": True},
    "email": {"mailbox_used_pct": 42, "profile_ok": True, "sync_stuck": False, "license_assigned": True},
    "network": {"adapter_enabled": True, "dhcp_lease_valid": True, "dns_cache_stale": False,
                "nic_hardware_fault": False},
    "device": {"disk_encryption": True, "patches_current": True, "antivirus_running": True,
               "antivirus_tampered": False},
    "software": {"installed": ["chrome", "teams"]},
    # Simulates a remediation that the back-end system refuses (to demo escalation).
    "blocked_actions": [],
}

# Approved company catalogue: name -> display name. Anything in REQUIRES_APPROVAL needs a technician.
SOFTWARE_CATALOG = {
    "chrome": "Google Chrome", "firefox": "Mozilla Firefox", "teams": "Microsoft Teams",
    "zoom": "Zoom", "slack": "Slack", "vscode": "Visual Studio Code", "python": "Python 3",
    "7zip": "7-Zip", "notepad++": "Notepad++", "acrobat reader": "Adobe Acrobat Reader",
}
SOFTWARE_ALIASES = {"visual studio code": "vscode", "vs code": "vscode", "google chrome": "chrome",
                    "microsoft teams": "teams", "adobe reader": "acrobat reader", "7-zip": "7zip"}
REQUIRES_APPROVAL = {"photoshop": "Adobe Photoshop", "tableau": "Tableau Desktop", "matlab": "MATLAB",
                     "docker": "Docker Desktop", "visio": "Microsoft Visio", "autocad": "AutoCAD"}


def _merge(base: dict, stored: dict) -> dict:
    result = copy.deepcopy(base)
    for key, value in stored.items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = _merge(result[key], value)
        else:
            result[key] = value
    return result


def get_state(employee_id: str) -> dict:
    with connect() as db:
        row = db.execute("SELECT state FROM employee_systems WHERE employee_id=?", (employee_id,)).fetchone()
    return _merge(DEFAULT_STATE, json.loads(row["state"]) if row else {})


def save_state(employee_id: str, state: dict):
    with connect() as db:
        db.execute("""INSERT INTO employee_systems (employee_id, state) VALUES (?, ?)
            ON CONFLICT(employee_id) DO UPDATE SET state=excluded.state, updated_at=CURRENT_TIMESTAMP""",
                   (employee_id, json.dumps(state)))


def service_status(name: str) -> str:
    with connect() as db:
        row = db.execute("SELECT status FROM service_status WHERE name=?", (name,)).fetchone()
    return row["status"] if row else "unknown"


def set_service_status(name: str, status: str):
    with connect() as db:
        db.execute("INSERT INTO service_status VALUES (?, ?) ON CONFLICT(name) DO UPDATE SET status=excluded.status",
                   (name, status))


def employee_devices(employee_id: str) -> list[dict]:
    with connect() as db:
        rows = db.execute("SELECT id, name, operating_system, compliant FROM devices WHERE employee_id=?", (employee_id,))
        return [dict(row) for row in rows]


# ---------------------------------------------------------------- diagnostics
def _check(name, ok, detail, fix=None, owner="agent", category="other"):
    """owner: who can fix a failed check -> agent (automated), employee (self-service), technician."""
    return {"check": name, "ok": ok, "detail": detail, "fix": None if ok else fix,
            "owner": None if ok else owner, "category": None if ok else category}


def _version(text):
    return tuple(int(part) for part in str(text).split(".") if part.isdigit())


def _service_check(name, category="service_outage"):
    status = service_status(name)
    return _check(f"{name.upper()} service status", status == "operational", f"Service reports: {status}",
                  owner="technician", category=category)


def _directory_checks(state):
    d = state["directory"]
    return [
        _check("Corporate account lock", not d["locked"],
               "Account is locked after failed sign-ins" if d["locked"] else "Account is not locked",
               fix="unlock_account"),
        _check("Corporate password", not d["password_expired"],
               "Password has expired; VPN and email sign-in will fail" if d["password_expired"] else "Password is valid",
               fix="issue_password_reset", owner="employee"),
    ]


def _network_checks(state):
    n = state["network"]
    return [
        _check("Network adapter", n["adapter_enabled"], "Adapter enabled" if n["adapter_enabled"] else "Wi-Fi/Ethernet adapter is disabled",
               fix="enable_network_adapter"),
        _check("IP address lease", n["dhcp_lease_valid"], "Valid DHCP lease" if n["dhcp_lease_valid"] else "No valid IP address (expired or conflicting lease)",
               fix="renew_dhcp_lease"),
        _check("DNS resolution", not n["dns_cache_stale"], "DNS resolves correctly" if not n["dns_cache_stale"] else "DNS cache holds stale entries",
               fix="flush_dns_cache"),
        _check("Network hardware", not n["nic_hardware_fault"], "Adapter hardware healthy" if not n["nic_hardware_fault"] else "Network adapter reports a hardware fault",
               owner="technician", category="hardware_repair"),
    ]


def diagnose(domain: str, employee_id: str) -> list[dict]:
    state = get_state(employee_id)
    if domain == "vpn":
        v = state["vpn"]
        return [_service_check("vpn")] + _network_checks(state) + _directory_checks(state) + [
            _check("VPN licence", v["license_assigned"], "Licence assigned" if v["license_assigned"] else "No VPN licence is assigned to this account",
                   owner="technician", category="account_access"),
            _check("VPN certificate", v["certificate_valid"], "Device certificate valid" if v["certificate_valid"] else "Device certificate has expired",
                   fix="renew_vpn_certificate"),
            _check("VPN client version", _version(v["client_version"]) >= _version(REQUIRED_VPN_CLIENT),
                   f"Installed {v['client_version']}, required {REQUIRED_VPN_CLIENT}", fix="update_vpn_client"),
            _check("VPN profile", v["profile_ok"], "Connection profile present" if v["profile_ok"] else "Connection profile is missing or corrupt",
                   fix="push_vpn_profile"),
            _check("VPN gateway session", not v["stale_session"], "No stuck session" if not v["stale_session"] else "A stale session on the gateway is blocking new logins",
                   fix="clear_vpn_session"),
        ]
    if domain == "email":
        e = state["email"]
        return [_service_check("email")] + _directory_checks(state) + [
            _check("Mail licence", e["license_assigned"], "Licence assigned" if e["license_assigned"] else "No mailbox licence is assigned",
                   owner="technician", category="account_access"),
            _check("Mailbox storage", e["mailbox_used_pct"] < MAILBOX_LIMIT_PCT, f"Mailbox {e['mailbox_used_pct']}% full",
                   fix="archive_old_mail"),
            _check("Mail profile", e["profile_ok"], "Profile healthy" if e["profile_ok"] else "Outlook profile is corrupt",
                   fix="rebuild_mail_profile"),
            _check("Mail sync", not e["sync_stuck"], "Sync is current" if not e["sync_stuck"] else "Mailbox sync is stuck",
                   fix="reset_mail_sync"),
        ]
    if domain == "network":
        return [_service_check("network", "network_incident")] + _network_checks(state)
    if domain == "account":
        return _directory_checks(state)
    if domain == "device":
        d = state["device"]
        return [
            _check("Disk encryption", d["disk_encryption"], "BitLocker on" if d["disk_encryption"] else "Disk encryption is off",
                   fix="enable_disk_encryption"),
            _check("Security updates", d["patches_current"], "Up to date" if d["patches_current"] else "Required security updates are missing",
                   fix="install_security_updates"),
            _check("Antivirus tamper protection", not d["antivirus_tampered"], "Intact" if not d["antivirus_tampered"] else "Antivirus was tampered with",
                   owner="technician", category="security_review"),
            _check("Antivirus service", d["antivirus_running"], "Running" if d["antivirus_running"] else "Antivirus service is stopped",
                   fix="restart_antivirus"),
        ]
    raise ValueError(f"Unknown diagnostic domain: {domain}")


# ---------------------------------------------------------------- remediation
def _set(path, value, done):
    def apply(state, **_):
        section, key = path.split(".")
        state[section][key] = value
        return done
    return apply


def _archive(state, **_):
    before = state["email"]["mailbox_used_pct"]
    state["email"]["mailbox_used_pct"] = min(before, 60)
    return f"Moved mail older than 12 months to the online archive ({before}% -> {state['email']['mailbox_used_pct']}%)"


def _unlock(state, **_):
    state["directory"].update(locked=False, failed_logins=0)
    return "Unlocked the corporate account and cleared failed sign-in attempts"


def _update_client(state, **_):
    old = state["vpn"]["client_version"]
    state["vpn"]["client_version"] = REQUIRED_VPN_CLIENT
    return f"Pushed VPN client update {old} -> {REQUIRED_VPN_CLIENT}"


def _revoke(state, now=None, **_):
    state["directory"]["sessions_revoked_at"] = now
    return "Signed the account out of all devices and revoked active tokens"


def _install(state, package=None, **_):
    installed = state["software"]["installed"]
    if package not in installed:
        installed.append(package)
    return f"Deployed {SOFTWARE_CATALOG.get(package, package)} to the employee's laptop"


ACTIONS = {
    "unlock_account": _unlock,
    "renew_vpn_certificate": _set("vpn.certificate_valid", True, "Issued a new VPN device certificate"),
    "update_vpn_client": _update_client,
    "push_vpn_profile": _set("vpn.profile_ok", True, "Re-deployed the VPN connection profile"),
    "clear_vpn_session": _set("vpn.stale_session", False, "Terminated the stale VPN gateway session"),
    "archive_old_mail": _archive,
    "rebuild_mail_profile": _set("email.profile_ok", True, "Rebuilt the Outlook mail profile"),
    "reset_mail_sync": _set("email.sync_stuck", False, "Reset the mailbox sync state"),
    "enable_network_adapter": _set("network.adapter_enabled", True, "Re-enabled the network adapter"),
    "renew_dhcp_lease": _set("network.dhcp_lease_valid", True, "Released and renewed the IP address lease"),
    "flush_dns_cache": _set("network.dns_cache_stale", False, "Flushed the DNS cache"),
    "enable_disk_encryption": _set("device.disk_encryption", True, "Enabled BitLocker disk encryption via MDM policy"),
    "install_security_updates": _set("device.patches_current", True, "Installed the missing security updates"),
    "restart_antivirus": _set("device.antivirus_running", True, "Restarted the antivirus service"),
    "revoke_sessions": _revoke,
    "install_software": _install,
}


def run_action(employee_id: str, session_id: str | None, action: str, **kwargs) -> dict:
    """Execute one allow-listed remediation, persist the new state and audit it."""
    if action not in ACTIONS:
        return {"action": action, "ok": False, "detail": "Action is not allowed."}
    state = get_state(employee_id)
    if action in state.get("blocked_actions", []):
        result = {"action": action, "ok": False,
                  "detail": "The target system rejected the change (insufficient permissions for automation)."}
    else:
        result = {"action": action, "ok": True, "detail": ACTIONS[action](state, **kwargs)}
        save_state(employee_id, state)
    with connect() as db:
        db.execute("INSERT INTO remediation_log (employee_id, session_id, action, ok, detail) VALUES (?, ?, ?, ?, ?)",
                   (employee_id, session_id, action, int(result["ok"]), result["detail"]))
    return result


def lookup_software(text: str) -> tuple[str | None, str | None, bool]:
    """Return (package key, display name, approval_required) for software named in text."""
    lowered = text.lower()
    for alias, key in SOFTWARE_ALIASES.items():
        if alias in lowered:
            return key, SOFTWARE_CATALOG[key], False
    for key, name in REQUIRES_APPROVAL.items():
        if key in lowered:
            return key, name, True
    for key, name in SOFTWARE_CATALOG.items():
        if f" {key} " in f" {lowered.replace(',', ' ').replace('.', ' ')} ":
            return key, name, False
    return None, None, False
