"""The web page's script, run in Node against scripted responses (skipped where Node is missing)."""
import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).parent.parent
NODE = shutil.which("node")
LEGACY = " · this server reports no sample age"
IN_REQUEST = {"sample_hang_waiting", "tab_return_checking", "tab_return_while_requesting"}  # reported mid-request


@pytest.fixture(scope="module")
def steps():
    if NODE is None:
        pytest.skip("node not installed")
    out = subprocess.run([NODE, str(ROOT / "tests/web_harness.js"), str(ROOT / "frontends/web/index.html")],
                         capture_output=True, text=True, timeout=30, check=True).stdout
    return {s["step"]: s for s in map(json.loads, out.splitlines())}


def test_every_step_ran(steps):
    assert list(steps) == ["ok", "503", "hang", "down", "garbage", "recovered",
                           "sample", "same_sample", "sample_503", "sample_hang_waiting", "sample_hang",
                           "bad_metadata", "new_instance", "new_sequence",
                           "tab_return_checking", "tab_return_verified",
                           "tab_return_while_requesting", "old_answer_ignored", "tab_return_rechecked",
                           "old_request_aborted", "tab_return_check_503", "tab_return_check_timeout",
                           "cpu_warmup", "cpu_partial", "cpu_odd_reason",
                           "collection_bad", "collection_bad_then_down", "collection_ok", "collection_odd",
                           "read_based_age", "counters", "pressure", "counters_absent", "counters_odd"]
    for s in steps.values():
        if s["step"] != "cpu_warmup":  # no CPU rows before the second reading, by design
            assert s["shows_data"], s     # the last good values stay on screen
        assert s["age_timers"] == 1, s  # one age display timer, never more
        # exactly one next update, whatever happened; none while a request runs (it schedules the next)
        assert s["refresh_scheduled"] == (0 if s["step"] in IN_REQUEST else 1), s


def test_web_survives_errors_and_recovers(steps):
    """An older server without sample metadata: the age is the time since its last answer, and says so.
    (Before the sample age line, this age was appended to the error message as "showing data from N s ago".)"""
    assert steps["ok"]["err"] == "" and steps["ok"]["age"] == "Response received 0 s ago" + LEGACY
    assert steps["503"]["err"] == "No current data: platmon is running but has no recent sample (see its log)"
    assert steps["503"]["age"] == "Last good: response received 1 s ago" + LEGACY + " (not current)"
    assert steps["hang"]["err"] == "No answer within 5 s"  # timed out, not stuck
    assert steps["hang"]["age"] == "Last good: response received 7 s ago" + LEGACY + " (not current)"
    assert steps["down"]["err"] == "Connection lost: Failed to fetch"
    assert steps["down"]["age"] == "Last good: response received 8 s ago" + LEGACY + " (not current)"
    assert steps["garbage"]["err"] == "Unexpected response (not platmon data)"
    assert steps["garbage"]["age"] == "Last good: response received 9 s ago" + LEGACY + " (not current)"
    assert steps["recovered"]["err"] == "" and steps["recovered"]["age"] == "Response received 0 s ago" + LEGACY


def test_status_pill_and_offline_banner(steps):
    """The age line lives in the status pill's tooltip; failures show Offline and the last good data's age."""
    assert steps["ok"]["status"] == "Live" and steps["ok"]["err_age"] == ""
    assert steps["503"]["status"] == "Offline"
    assert steps["503"]["err_age"] == " Showing the last good answer, received 1 s ago."
    assert steps["sample_503"]["err_age"] == " Showing the last good sample, 4 s old."
    assert steps["recovered"]["status"] == "Live" and steps["recovered"]["err_age"] == ""
    assert steps["tab_return_checking"]["status"] == "Checking"


def test_sample_age_comes_from_the_server(steps):
    assert steps["sample"]["age"] == "Sample #5 · data age 2 s"
    assert steps["same_sample"]["age"] == "Sample #5 · data age 3 s"  # a repeated 200 does not reset it to 0
    # failures keep the last sample and its growing age, also while a request hangs
    assert steps["sample_503"]["age"] == "Last good: sample #5 · data age 4 s (not current)"
    assert steps["sample_hang_waiting"]["age"] == "Last good: sample #5 · data age 7 s (not current)"
    assert steps["sample_hang"]["age"] == "Last good: sample #5 · data age 10 s (not current)"
    assert steps["sample_hang"]["err"] == "No answer within 5 s"


def test_bad_metadata_is_not_age_zero(steps):
    s = steps["bad_metadata"]
    assert s["err"] == "" and s["age"] == "Response received 0 s ago · sample age unavailable (invalid metadata)"
    assert "data age" not in s["age"]


def test_new_instance_and_sequence(steps):
    assert steps["new_instance"]["age"] == "Sample #1 · data age 0 s"
    assert steps["new_sequence"]["age"] == "Sample #2 · data age 1 s"


def test_tab_return(steps):
    """Browser time may have stopped while the tab was hidden: say so until a new answer arrives,
    asking at once, but never next to a request that is already running."""
    c = steps["tab_return_checking"]
    assert c["age"].startswith("Checking… last seen: sample #2") and c["fetches"] == steps["new_sequence"]["fetches"] + 1
    assert c["refresh_scheduled"] == 0  # the pending refresh was replaced by this request, not added to
    assert steps["tab_return_verified"]["age"] == "Sample #600 · data age 0 s"



def test_tab_return_does_not_trust_a_request_started_before(steps):
    """A request started before the tab was hidden is cancelled; if its answer still arrives, it is dropped
    (it carries an age from before the break), and only the answer to a new request ends the check."""
    w = steps["tab_return_while_requesting"]
    assert w["age"].startswith("Checking… ") and w["fetches"] == steps["tab_return_verified"]["fetches"] + 1
    late = steps["old_answer_ignored"]
    assert late["age"] == "Checking… last seen: sample #600 · data age 601 s"  # not "sample #601 · data age 0 s"
    assert late["fetches"] == w["fetches"] and late["refresh_scheduled"] == 1  # one new request due, no other
    new = steps["tab_return_rechecked"]
    assert new["age"] == "Sample #601 · data age 1 s" and new["fetches"] == late["fetches"] + 1  # same sample is fine


def test_failed_check_after_tab_return_stays_not_current(steps):
    aborted = steps["old_request_aborted"]
    assert aborted["err"] == "" and aborted["age"].startswith("Checking… ")  # the cancel is not reported as an error
    failed = steps["tab_return_check_503"]
    assert failed["fetches"] == aborted["fetches"] + 1
    assert failed["err"].startswith("No current data") and failed["age"].startswith("Last good: sample #601")
    timeout = steps["tab_return_check_timeout"]
    assert timeout["err"] == "No answer within 5 s" and timeout["age"].endswith("(not current)")


def test_cpu_sampling_note(steps):
    """Missing CPU rows are explained as CPU sampling state, not as an error, and never drawn as 0 %."""
    w = steps["cpu_warmup"]
    assert w["err"] == "" and w["cpu"] == '<div class="muted">CPU sampling: warming up</div>'
    p = steps["cpu_partial"]
    assert "CPU0" in p["cpu"] and "CPU2" not in p["cpu"].split("CPU sampling")[0]
    assert p["cpu"].endswith('<div class="muted">CPU sampling: CPU2 warming up; CPU3 counter went backwards</div>')
    assert "<img" not in steps["cpu_odd_reason"]["cpu"]  # server strings are escaped
    assert steps["cpu_odd_reason"]["cpu"].endswith("CPU sampling: CPU4 &#60;img src=x&#62;</div>")
    assert "CPU sampling" not in steps["ok"]["cpu"]  # legacy servers: no note


def test_collection_note(steps):
    """Read failures of optional groups are named; absent ones are not; set as text, so nothing is injected."""
    assert steps["collection_bad"]["coll"] == "temperature partial, gpu error, <b>x</b> error"
    assert steps["collection_bad"]["err"] == "" and steps["collection_bad"]["status"] == "Partial"
    assert steps["collection_bad_then_down"]["status"] == "Offline"
    assert steps["collection_bad_then_down"]["coll"] == steps["collection_bad"]["coll"]  # belongs to the last good data
    assert steps["collection_ok"]["coll"] == "" and steps["collection_odd"]["coll"] == ""
    assert steps["collection_ok"]["status"] == "Live"
    assert steps["ok"]["coll"] == ""  # legacy servers: no note


def test_read_based_data_age(steps):
    """data_age_ms is shown as given (2.4 s here), whatever age_ms + duration_ms or cycle_age_ms say."""
    assert steps["read_based_age"]["age"] == "Sample #7 · data age 2 s"


def test_network_and_disk_rows(steps):
    net, dio = steps["counters"]["net"], steps["counters"]["dio"]
    assert "eth0</span><span>1.5 KiB/s</span><span>0 B/s</span>" in net and '<span class="t-warn">3 · 0</span>' in net
    assert steps["counters"]["net_cap"] == "This process's network namespace · window 1.000 s · errors and drops are totals"
    assert "&#60;img src=x onerror=alert(1)&#62;" in net and "<img" not in net  # a name is text, never HTML
    assert '<span class="muted">2.0 · 0.0</span>' in net
    assert "warming up" in net and "some_new_reason" in net  # no rate: the reason, not 0
    assert '<div class="tr idle">' not in net.split("eth0")[0].rsplit("</div>", 1)[-1]  # eth0 receives: not dimmed
    assert "nvme0n1</span><span>3.0 MiB/s</span><span>512 B/s</span>" in dio and "4.0 · 0.5" in dio and "3.4%" in dio
    assert "counter went backwards" in dio and "saturat" not in dio


def test_counters_absent_or_odd_show_nothing(steps):
    for step in ("counters_absent", "counters_odd"):
        assert steps[step]["net"] == "" and steps[step]["dio"] == "" and steps[step]["err"] == "", step


def test_storage_from_observations(steps):
    sto = steps["ok"]["sto"]
    assert steps["ok"]["obs_age"]["storage"] == "observed 95 s ago (not current)"  # the server's age, and stale says so
    assert "1.0G / 4.0G · 25%" in sto and "3.0G free" in sto and "<b>" not in sto
    assert '/mnt/&#60;b&#62;x&#60;/b&#62;</span><span class="badge">ro</span><span class="muted">+60 more' in sto
    assert sto.index("/mnt/") < sto.index("/data")  # by usage, unknown capacity last
    assert "/bind/" not in sto  # dozens of bind mounts: counted, not listed
    assert "capacity unknown" in sto and "0:40" in sto  # no numbers made up when statvfs gave none
    assert "nvme0n1p2" in sto and "not mounted" in sto
    assert steps["ok"]["obs_fetches"] == 1


def test_observations_404_stops_asking(steps):
    """The next poll gets a 404 (an older server): the panel is cleared and never asked for again."""
    assert steps["read_based_age"]["sto"] == "" and steps["read_based_age"]["obs_fetches"] == 2


def test_wifi_from_observations(steps):
    wifi = steps["ok"]["wifi"]
    assert steps["ok"]["obs_age"]["wifi"] == "observed 2 s ago" and "-64 dBm" in wifi and "link quality 46" in wifi
    assert "wlan1</span><span>not connected" in wifi  # no last signal, no 0
    assert wifi.count('class="bar"') == 1  # only a dBm signal gets a bar
    assert "signal 70 (unit not reported)" in wifi and "&#60;i&#62;x&#60;/i&#62;" in wifi and "<i>" not in wifi
    assert steps["read_based_age"]["wifi"] == ""  # cleared after the server stopped answering it (404)


def test_probe_from_observations(steps):
    probe = steps["ok"]["probe"]
    assert '192.0.2.1:443</span><span>12.3 ms</span><span><span class="t-warn">1/10' in probe
    assert "[2001:db8::1]:22</span><span>timeout</span>" in probe and "0.0 ms" not in probe
    assert steps["read_based_age"]["probe"] == ""


def test_the_battery_card_is_under_power_and_battery(steps):
    """The card is in the Power & battery panel, with the rails and fans, not under Temperature: what the
    board draws and what it has left are read together."""
    assert steps["ok"]["batt_panel"] == {"thermal": False, "power": True}


def test_battery_from_observations(steps):
    batt = steps["ok"]["batt"]
    assert steps["ok"]["obs_age"]["battery"] == "observed 30 s ago"
    assert "89 %" in batt and "Not charging" in batt and "237 cycles" in batt
    assert "16.44 V" in batt and "-1.20 A" in batt and "85 % of design" in batt  # 3400000/4000000
    assert "BAT2</span><span>not present" in batt and batt.count('class="bar"') == 1  # no bar for an empty bay
    assert "ADP1 on · USBC1 off" in batt
    assert "&#60;i&#62;BAT1&#60;/i&#62;" in batt and "<i>" not in batt  # a driver name is escaped
    assert steps["read_based_age"]["batt"] == ""  # cleared after the server stopped answering it (404)


def line(hist, label):
    """The drawn line (not the area fill) of the chart labelled label."""
    return re.search('class="line" d="([^"]*)"', hist.split(label + "</span>")[1]).group(1)


def test_history_graphs(steps):
    hist = steps["ok"]["hist"]
    assert "RAM used</span><span>2.0G" in hist and "<svg" in hist and "1.0G – 2.0G" in hist
    assert "eth0 rx</span><span>no value" in hist  # the latest point is a gap: said so, not 0
    assert "&#60;b&#62; tx" in hist and "<b>" not in hist
    assert "CPU0" in hist and ">cpu/0" not in hist and "unknown" not in hist  # only the series the page knows how to label
    eth0 = line(hist, "eth0 rx")
    assert eth0.count("M") == 1 and "L" not in eth0  # one point between two gaps: no line drawn across them
    assert steps["read_based_age"]["hist"] == "" and steps["read_based_age"]["hist_fetches"] == 2  # 404: stopped


def test_power_rail_history(steps):
    """The rails are what a consumption reading is made of, so they get their own graphs in watts, on the
    view that shows the rails. A rail is labelled from sensor_meta like a temperature channel, by name
    when the server gave no source id."""
    hist = steps["ok"]["hist"]
    assert "<h3>Power rails</h3>" in hist
    assert "rail VDD_IN</span><span>5.50 W" in hist and "5.25 W – 5.50 W" in hist  # the id resolved by sensor_meta
    assert "rail VDD_SOC</span><span>1.50 W" in hist  # no id: the name the server published


def test_pressure_rows(steps):
    psi = steps["pressure"]["psi"]
    assert "io</span><span>3.0%</span><span><span class=\"muted\">0.5%" in psi
    assert "cpu</span><span>1.3%</span><span><span class=\"muted\">—" in psi  # no full for the CPU: undefined at the system level
    assert steps["counters_absent"]["psi"] == ""


def test_history_uses_elapsed_spacing_and_low_frequency_units(steps):
    hist = steps["ok"]["hist"]
    assert "TCP 127.0.0.1:80</span><span>3.0 ms" in hist
    assert "wlan0 signal</span><span>-64.0 dBm" in hist
    assert "FS 8:1 used</span><span>1.0G" in hist
    assert "latest point 2 s ago" in hist
    assert line(hist, "TCP 127.0.0.1:80") == "M0.0,26.0L20.0,14.0L200.0,2.0"  # irregular 1 s then 9 s interval
    assert "1.0 ms – 3.0 ms" in hist  # the chart's range


def test_optional_polling_is_bounded():
    if NODE is None:
        pytest.skip("node not installed")
    subprocess.run([NODE, str(ROOT / "tests/web_optional_harness.js"),
                    str(ROOT / "frontends/web/index.html")], check=True, timeout=30)


def test_cached_history_age_keeps_advancing():
    if NODE is None:
        pytest.skip("node not installed")
    subprocess.run([NODE, str(ROOT / "tests/web_history_age_harness.js"),
                    str(ROOT / "frontends/web/index.html")], check=True, timeout=30)


def test_views_share_polling_and_suspend_when_hidden():
    if NODE is None:
        pytest.skip("node not installed")
    subprocess.run([NODE, str(ROOT / "tests/web_views_harness.js"),
                    str(ROOT / "frontends/web/index.html")], check=True, timeout=30)
