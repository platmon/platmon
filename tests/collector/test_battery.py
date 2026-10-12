"""collector/battery.py: batteries and external supplies from /sys/class/power_supply. Fake class trees;
the last test reads the host's own class where there is one.

The group's state matters beyond platmon: a fleet server treats any current group in `error` or `partial`
as an optional-endpoint error for that node, so an attribute a driver simply does not expose must not
make the group partial, and a host without a supply must report `unavailable`.
"""
import errno
import os

import pytest

import platmon
from collector import metadata
from collector import sysfs
from collector.battery import Battery

CHARGE_BATTERY = {  # as read on an ACPI laptop: the charge family, no energy_* and no power_now
    "type": "Battery", "present": "1", "capacity": "89", "status": "Not charging",
    "capacity_level": "Normal", "technology": "Li-ion", "cycle_count": "237",
    "charge_now": "3026000", "charge_full": "3400000", "charge_full_design": "4023000",
    "voltage_now": "16436000", "current_now": "0",
}
ENERGY_BATTERY = {  # the other gauge family
    "type": "Battery", "present": "1", "capacity": "54", "status": "Discharging",
    "energy_now": "27000000", "energy_full": "50000000", "energy_full_design": "52000000",
    "voltage_now": "11400000", "current_now": "-1200000", "power_now": "13680000",
}


def tree(root, supplies):
    """A power_supply class directory: {supply name: {attribute: contents}}."""
    for name, attrs in supplies.items():
        d = root / name
        d.mkdir(parents=True)
        for attr, value in attrs.items():
            (d / attr).write_text(value)
    return str(root)


def observe(root, supplies=None):
    g = metadata.Group("battery")
    data = Battery(root=tree(root, supplies) if supplies is not None else str(root))(g)
    return data, g.status()


def test_charge_family_battery(tmp_path):
    """The charge gauge: its own fields filled, the energy family left as nulls, nothing noted."""
    data, status = observe(tmp_path, {"BAT1": CHARGE_BATTERY})
    assert status["state"] == "ok" and status["issues"] == []
    assert data["provider"] == "sysfs_power_supply" and data["external"] == []
    assert data["batteries"] == [{
        "name": "BAT1", "present": True, "percent": 89, "status": "Not charging",
        "capacity_level": "Normal", "technology": "Li-ion", "health": None,
        "charge_now_uah": 3026000, "charge_full_uah": 3400000, "charge_full_design_uah": 4023000,
        "energy_now_uwh": None, "energy_full_uwh": None, "energy_full_design_uwh": None,
        "current_ua": 0, "power_uw": None, "voltage_uv": 16436000, "cycle_count": 237}]


def test_energy_family_battery_keeps_the_sign(tmp_path):
    """The energy gauge, and a negative current passed through: drivers differ on how they show discharge."""
    data, status = observe(tmp_path, {"BAT0": ENERGY_BATTERY})
    assert status["state"] == "ok"
    b = data["batteries"][0]
    assert (b["energy_now_uwh"], b["energy_full_uwh"], b["power_uw"]) == (27000000, 50000000, 13680000)
    assert b["current_ua"] == -1200000 and b["charge_now_uah"] is None and b["cycle_count"] is None


def test_an_attribute_the_driver_does_not_expose_is_not_a_problem(tmp_path):
    """A minimal driver (only type, present and capacity) stays `ok`: a fleet server must not see an
    optional-endpoint error for every node whose gauge exposes less than another's."""
    data, status = observe(tmp_path, {"BAT1": {"type": "Battery", "present": "1", "capacity": "50"}})
    assert status == {"state": "ok", "reason": None, "issues": [], "issues_truncated": 0}
    b = data["batteries"][0]
    assert b["percent"] == 50 and b["status"] is None and b["voltage_uv"] is None


def test_no_supply_is_unavailable_not_an_error(tmp_path):
    """An empty class directory, and a host without the class at all: `unavailable`, which a fleet
    server leaves out of its alerting, not `error`."""
    for root in (tmp_path / "empty", tmp_path / "missing"):
        if root.name == "empty":
            root.mkdir()
        data, status = observe(root)
        assert status["state"] == "unavailable" and status["reason"] == "not_detected"
        assert data["batteries"] == [] and data["external"] == []


def test_empty_bay_reports_absent_without_inventing_numbers(tmp_path):
    """present = 0: the bay is empty, so whatever the attributes still hold is not its reading."""
    data, status = observe(tmp_path, {"BAT2": {"type": "Battery", "present": "0", "capacity": "0",
                                               "status": "Unknown", "charge_now": "0"}})
    assert status["state"] == "ok"
    assert data["batteries"] == [{
        "name": "BAT2", "present": False, "percent": None, "status": None, "capacity_level": None,
        "technology": None, "health": None, "charge_now_uah": None, "charge_full_uah": None,
        "charge_full_design_uah": None, "energy_now_uwh": None, "energy_full_uwh": None,
        "energy_full_design_uwh": None, "current_ua": None, "power_uw": None, "voltage_uv": None,
        "cycle_count": None}]


def test_external_supplies_and_their_order(tmp_path):
    """Everything that is not a Battery is external power, reported with its type and whether it is on."""
    data, status = observe(tmp_path, {
        "ADP1": {"type": "Mains", "online": "1"},
        "ucsi-source-psy-USBC000:002": {"type": "USB", "online": "0"},
        "BAT1": CHARGE_BATTERY})
    assert status["state"] == "ok"
    assert data["external"] == [{"name": "ADP1", "type": "Mains", "online": True},
                                {"name": "ucsi-source-psy-USBC000:002", "type": "USB", "online": False}]
    assert [b["name"] for b in data["batteries"]] == ["BAT1"]


def test_a_ups_holds_charge_like_a_battery(tmp_path):
    """The class is not only laptops: a UPS reports capacity and status the same way, so it is read the
    same way. Only a source of external power (Mains, USB, Wireless) is reduced to whether it is online."""
    data, status = observe(tmp_path, {
        "hidev0": {"type": "UPS", "present": "1", "capacity": "100", "status": "Full"},
        "ADP1": {"type": "Mains", "online": "1"},
        "wl0": {"type": "Wireless", "online": "0"}})
    assert status["state"] == "ok"
    assert [(b["name"], b["percent"], b["status"]) for b in data["batteries"]] == [("hidev0", 100, "Full")]
    assert [e["type"] for e in data["external"]] == ["Mains", "Wireless"]


def test_a_supply_without_a_type_is_left_out(tmp_path):
    """Without the type there is no telling a battery from a charger, so the supply is not reported."""
    data, status = observe(tmp_path, {"weird": {"online": "1"}})
    assert data["batteries"] == [] and data["external"] == []
    assert status["state"] == "unavailable" and status["reason"] == "not_exposed"


@pytest.mark.parametrize("attrs, target", [
    ({"type": "Battery", "present": "1", "capacity": "150"}, "BAT1.capacity"),   # out of 0..100
    ({"type": "Battery", "present": "1", "capacity": "", "voltage_now": "1"}, "BAT1.capacity"),
    ({"type": "Battery", "present": "1", "voltage_now": "-5"}, "BAT1.voltage_now"),  # not a voltage
    ({"type": "Battery", "present": "yes"}, "BAT1.present"),                      # not a flag
    ({"type": "Mains", "online": "2"}, "BAT1.online"),
])
def test_a_value_that_is_not_one_is_invalid_data(tmp_path, attrs, target):
    data, status = observe(tmp_path, {"BAT1": attrs})
    assert status["state"] in ("partial", "error") and status["reason"] in ("some_unreadable", "invalid_data")
    assert [i["target"] for i in status["issues"]] == [target]


def test_an_over_long_enum_is_not_one(tmp_path):
    data, status = observe(tmp_path, {"BAT1": {"type": "Battery", "present": "1", "capacity": "50",
                                               "status": "x" * 33}})
    assert data["batteries"][0]["status"] is None
    assert status["state"] == "partial"
    assert status["issues"] == [{"target": "BAT1.status", "reason": "invalid_data"}]


def test_an_unreadable_attribute_is_reported_and_the_rest_kept(tmp_path, monkeypatch):
    """A denied attribute is a real problem (it is there and platmon may not have it), unlike one the
    driver never exposed; the readable fields are still reported."""
    root = tree(tmp_path, {"BAT1": CHARGE_BATTERY})
    denied = f"{root}/BAT1/capacity"
    real = open

    def fake(path, *a, **k):
        if str(path) == denied:
            raise PermissionError(errno.EACCES, "Permission denied")
        return real(path, *a, **k)

    monkeypatch.setattr("builtins.open", fake)
    g = metadata.Group("battery")
    data = Battery(root=root)(g)
    status = g.status()
    assert status["state"] == "partial" and status["reason"] == "some_unreadable"
    assert data["batteries"][0]["percent"] is None and data["batteries"][0]["cycle_count"] == 237


def test_config_section(tmp_path):
    """[battery] is a known section with a bounded interval; an INI without it gets the defaults."""
    cfg = platmon.load_config(None)
    assert cfg["battery"].getboolean("enabled") is True and cfg["battery"].getfloat("interval") == 60.0
    assert platmon.RANGES[("battery", "interval")] == (10, 3600)
    path = tmp_path / "p.ini"
    path.write_text("[battery]\ninterval = 5\n")
    with pytest.raises(ValueError, match=r"\[battery\] interval = 5.0 is out of range"):
        platmon.load_config(str(path))
    path.write_text("[battery]\nenabled = no\n")
    assert platmon.load_config(str(path))["battery"].getboolean("enabled") is False


@pytest.mark.skipif(not os.path.isdir("/sys/class/power_supply"), reason="no power supply on this host")
def test_the_host_class_reads():
    """Whatever this host has, the reading holds its shape and the group does not report a problem."""
    g = metadata.Group("battery")
    data = Battery()(g)
    assert set(data) == {"provider", "batteries", "external"}
    assert g.status()["state"] in ("ok", "unavailable")  # a real host must not come out partial or error
    for b in data["batteries"]:
        assert b["present"] in (True, False, None)
        assert b["percent"] is None or 0 <= b["percent"] <= 100
    for e in data["external"]:
        assert set(e) == {"name", "type", "online"} and e["online"] in (True, False, None)
