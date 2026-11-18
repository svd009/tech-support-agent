"""Read-only lookups used by the HTTP API. Agent diagnostics and fixes live in it_systems.py."""
from it_systems import employee_devices, service_status


def check_service_status(service_name: str) -> dict:
    if not isinstance(service_name, str):
        raise TypeError("service_name must be a string.")
    service_name = service_name.strip().lower()
    if not service_name:
        raise ValueError("service_name must not be blank.")
    status = service_status(service_name)
    return {"service": service_name, "status": status, "found": status != "unknown"}


def get_employee_devices(employee_id: str) -> dict:
    return {"devices": employee_devices(employee_id)}
