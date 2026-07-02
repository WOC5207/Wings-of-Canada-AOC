"""End-to-end smoke test against a running AOC server.

    python scripts/smoke.py [base_url]

Uses a throwaway database ONLY if you point it at a fresh server - it
registers users and creates data. Intended for verification on a clean DB.
"""
import json
import re
import sys
import urllib.parse
import urllib.request
from http.cookiejar import CookieJar

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8080"

PASSED = 0
FAILED = []


def client():
    return urllib.request.build_opener(urllib.request.HTTPCookieProcessor(CookieJar()))


def call(opener, path, data=None):
    url = BASE + path
    # doseq=True lets a field carry a list (e.g. several approved aircraft).
    body = urllib.parse.urlencode(data, doseq=True).encode() if data is not None else None
    req = urllib.request.Request(url, data=body)
    with opener.open(req) as resp:
        return resp.status, resp.read().decode("utf-8", "replace")


def check(label, cond):
    global PASSED
    print(f"[{'ok' if cond else 'FAIL'}] {label}")
    if cond:
        PASSED += 1
    else:
        FAILED.append(label)


admin = client()

# --- registration / bootstrap admin -------------------------------------
status, page = call(admin, "/login")
check("login page reachable", status == 200 and "Crew sign in" in page)

status, page = call(admin, "/register", {
    "first_name": "Chief", "last_name": "Pilot",
    "email": "chief@wingsofcanada.ca", "callsign_digits": "1",
    "password": "hunter2hunter2", "confirm": "hunter2hunter2",
})
check("first user registered as admin", "Administrator" in page and "WOC1" in page)


def mint_invite():
    """Mint a single-use invitation code (admin only) and return its value.
    Sign-ups after the first user are invite-gated."""
    _, page = call(admin, "/admin/invites/create", {})
    m = re.search(r"WOC-[A-Z0-9]{4}-[A-Z0-9]{4}", page)
    return m.group(0) if m else ""

# --- fleet ----------------------------------------------------------------
# A long-range passenger jet (id 1).
status, page = call(admin, "/fleet/new", {
    "registration": "C-FWOC", "icao_type": "B38M", "variant": "737 MAX 8",
    "load_type": "pax", "capacity": "178", "range_nm": "3000", "status": "active",
    "simbrief_url": "https://dispatch.simbrief.com/airframes/share/abc",
    "livery_url": "https://flightsim.to/file/123", "notes": "",
})
check("passenger aircraft added", "C-FWOC" in page and "B38M" in page)
check("passenger capacity shown as seats", "178 seats" in page)

# A freighter: capacity is entered in kg via the same single field (id 2).
status, page = call(admin, "/fleet/new", {
    "registration": "C-GENA", "icao_type": "B738", "variant": "737-800 BDSF",
    "load_type": "cargo", "capacity": "20847", "range_nm": "3000", "status": "active",
    "simbrief_url": "", "livery_url": "", "notes": "",
})
check("cargo aircraft added", "C-GENA" in page and "Cargo" in page)
check("cargo capacity shown in kg", "20,847 kg" in page)

# A short-range turboprop (id 3) — used to test range-based route eligibility.
status, page = call(admin, "/fleet/new", {
    "registration": "C-FSML", "icao_type": "DH8D", "variant": "Dash 8 Q400",
    "load_type": "pax", "capacity": "78", "range_nm": "800", "status": "active",
    "simbrief_url": "", "livery_url": "", "notes": "",
})
check("short-range aircraft added", "C-FSML" in page and "DH8D" in page)
check("aircraft range stored and shown on the detail page",
      "3,000 nm" in call(admin, "/fleet/1")[1])

# --- dispatch: numbering rules over HTTP (admin only) --------------------
def new_route(dep, arr, create_return=True, route_type="pax", dep_time="12:00",
              distance="300", opener=None):
    """Create a route and return (page, outbound_number, return_number).

    Numbers are parsed from the "Route created: CW#### …" flash because the
    allocator now picks a RANDOM free number in the series. Eligible airframes
    are derived from aircraft range vs the route distance — no manual picker.
    """
    h, m = dep_time.split(":") if dep_time else ("", "")
    data = {"dep": dep, "arr": arr,
            "route_type": route_type, "dep_time_h": h, "dep_time_m": m,
            "distance_nm": distance, "duration": "1:10", "notes": ""}
    if create_return:
        data["create_return"] = "on"
    _, page = call(opener or admin, "/dispatch/new", data)
    seg = re.search(r"Route created:[^<]*", page)
    nums = [int(x) for x in re.findall(r"CW(\d{4})", seg.group(0))] if seg else []
    out = nums[0] if nums else None
    ret = nums[1] if len(nums) > 1 else None
    return page, out, ret


def in_series(n, digit, parity):
    return n is not None and n // 1000 == digit and n % 2 == parity


# CYYZ->KJFK is the first route created -> ids 1 (outbound) and 2 (return).
page, r1_out, r1_ret = new_route("CYYZ", "KJFK")
check("CYYZ->KJFK international: 4xxx even outbound, 4xxx odd return",
      in_series(r1_out, 4, 0) and in_series(r1_ret, 4, 1))

# Toronto domestic departure = 5xxx; the return is numbered on its OWN
# departure city (Vancouver) = 1xxx, not coupled to the outbound.
page, out, ret = new_route("CYYZ", "CYVR")
check("CYYZ->CYVR domestic outbound in Toronto 5xxx (even)", in_series(out, 5, 0))
check("return CYVR->CYYZ in Vancouver 1xxx (even)", in_series(ret, 1, 0))

# Both legs abroad -> both odd, numbered independently.
page, out, ret = new_route("EGLL", "VHHH")
check("EGLL->VHHH abroad both odd in 9xxx",
      in_series(out, 9, 1) and in_series(ret, 9, 1) and out != ret)

# Numbers are randomized, not always x000.
page, out, _ = new_route("CYUL", "LFPG", create_return=False)
check("Montreal outbound is in 6xxx (even)", in_series(out, 6, 0))

# Return toggle off -> only the outbound leg is created.
page, out, ret = new_route("CYYC", "CYWG", create_return=False)
check("return toggle off creates a single route",
      in_series(out, 2, 0) and ret is None and "no return leg" in page)

page, _, _ = new_route("CYYZ", "CYYZ")
check("same dep/arr rejected", "cannot be the same" in page)

# Per-leg deletion: deleting one leg leaves its partner intact.
page, d_out, d_ret = new_route("KORD", "KJFK")  # both in 8xxx
listing = call(admin, "/dispatch/")[1]
row = next(r for r in listing.split("<tr") if f"CW{d_out:04d}" in r)
del_id = re.search(r"/dispatch/(\d+)/delete", row).group(1)
call(admin, f"/dispatch/{del_id}/delete", {})
after = call(admin, "/dispatch/")[1]
check("per-leg delete removes only that leg",
      f"CW{d_out:04d}" not in after and f"CW{d_ret:04d}" in after)

# --- distance / block-time estimation ---------------------------------------
status, page = call(admin, "/dispatch/estimate?dep=CYVR&arr=CYYZ&type=B38M")
est = json.loads(page)
check("estimate endpoint returns sensible distance",
      est.get("ok") is True and 1700 <= est["distance_nm"] <= 1900)
check("estimate includes block time",
      est.get("duration_min", 0) > 200 and ":" in est.get("duration_hmm", ""))

status, page = call(admin, "/dispatch/estimate?dep=ZZZZ&arr=CYYZ")
check("unknown airport yields no estimate", json.loads(page).get("ok") is False)

# --- airport autocomplete ---------------------------------------------------
status, page = call(admin, "/dispatch/airports?q=CYVR")
res = json.loads(page).get("results", [])
top = res[0] if res else {}
check("airport search returns name, city and UTC zone for an ICAO",
      top.get("icao") == "CYVR" and "Vancouver" in top.get("name", "")
      and top.get("city") == "Vancouver" and top.get("utc_label") == "UTC-8")

status, page = call(admin, "/dispatch/airports?q=heathrow")
icaos = {r["icao"] for r in json.loads(page).get("results", [])}
check("airport search matches by name", "EGLL" in icaos)

_, page = call(admin, "/dispatch/new", {
    "dep": "CYUL", "arr": "EGLL",
    "dep_time_h": "12", "dep_time_m": "00",
    "distance_nm": "", "duration": "", "notes": "", "create_return": "on",
})
seg = re.search(r"Route created:[^<]*", page)
blank_nums = [int(x) for x in re.findall(r"CW(\d{4})", seg.group(0))]
check("route with blank fields still created (6xxx pair)",
      len(blank_nums) == 2 and all(n // 1000 == 6 for n in blank_nums))

status, page = call(admin, f"/dispatch/?q=CW{blank_nums[0]:04d}")
check("blank distance/time were auto-estimated", " nm" in page)

# A short pax route (300 nm): every active passenger aircraft is in range, so
# both the jet and the turboprop are eligible.
page, multi_out, _ = new_route("CYWG", "CYQR", create_return=False,
                               dep_time="18:50", distance="300")
status, page = call(admin, f"/dispatch/?q=CW{multi_out:04d}")
check("short route lists every in-range passenger aircraft",
      'class="aircraft-select' in page
      and "<option>C-FSML DH8D</option>" in page
      and "<option>C-FWOC B38M</option>" in page)
check("route network shows the scheduled departure time", "18:50" in page)
check("SimBrief link carries the departure time (EOBT)",
      "deph=18&amp;depm=50" in page or "deph=18&depm=50" in page)
check("admin sees the Added by column", "Added by" in page)

# A long pax route (2500 nm): the short-range turboprop drops out, leaving only
# the long-range jet eligible.
page, long_out, _ = new_route("CYVR", "CYHZ", create_return=False, distance="2500")
status, page = call(admin, f"/dispatch/?q=CW{long_out:04d}")
check("long route excludes out-of-range aircraft",
      "<option>C-FWOC B38M</option>" in page
      and "C-FSML" not in page)

# A cargo route is eligible only for cargo aircraft, regardless of range.
page, cargo_out, _ = new_route("CYYZ", "CYWG", create_return=False,
                               route_type="cargo")
status, page = call(admin, f"/dispatch/?q=CW{cargo_out:04d}")
check("cargo route created and badged", "Cargo" in page)
check("cargo route lists only the cargo airframe",
      "<option>C-GENA B738</option>" in page and "C-FWOC" not in page)

# A departure time is mandatory on a new route.
_, page = call(admin, "/dispatch/new", {
    "dep": "CYVR", "arr": "CYYC", "route_type": "pax",
    "dep_time_h": "", "dep_time_m": "",
    "distance_nm": "300", "duration": "1:10", "notes": "",
})
check("missing departure time rejected",
      "departure time is required" in page.lower() and "Route created" not in page)

# An hour without a minute (one dropdown left on its placeholder) is incomplete.
_, page = call(admin, "/dispatch/new", {
    "dep": "CYVR", "arr": "CYYC", "route_type": "pax",
    "dep_time_h": "09", "dep_time_m": "",
    "distance_nm": "300", "duration": "1:10", "notes": "",
})
check("half-filled departure time rejected",
      "departure time is required" in page.lower() and "Route created" not in page)

# --- pirep ----------------------------------------------------------------
status, page = call(admin, "/dispatch/")
check("routes page lists network", f"CW{r1_out:04d}" in page and "SimBrief" in page)
check("route rows offer a Log flight button", "/pilots/log?route_id=" in page)

# A route row's button opens the log form locked to that scheduled route.
status, page = call(admin, "/pilots/log?route_id=1")
check("route row opens a locked scheduled log form",
      "Log a scheduled flight" in page and f"CW{r1_out:04d}" in page
      and 'name="training_number"' not in page)

# A scheduled route only offers its eligible aircraft (in-range, matching type).
# Route 1 (CYYZ->KJFK, 300 nm pax) offers both pax tails but not the freighter.
check("locked log form lists only eligible aircraft",
      "C-FWOC" in page and "C-GENA" not in page)

# A bare /pilots/log has nothing to log: pilots are sent to the dispatch list.
status, page = call(admin, "/pilots/log")
check("bare log page redirects to the route network",
      "Route network" in page and "Pick a scheduled flight" in page)

# The new-route form no longer carries a manual aircraft picker — eligibility is
# automatic from aircraft range.
status, page = call(admin, "/dispatch/new")
check("new-route form loads for admins",
      'id="route-form"' in page
      and "Eligible airframes are decided automatically" in page)
check("new-route form has no manual aircraft picker",
      'class="fp-check"' not in page)

# Scheduled PIREP: route 1 (CYYZ->KJFK) is pax; aircraft id 1 (C-FWOC) is eligible.
status, page = call(admin, "/pilots/log", {
    "mode": "scheduled", "route_id": "1", "aircraft_id": "1",
    "flight_date": "2026-06-10", "hours": "1", "minutes": "25",
    "remarks": "Smoke test leg",
})
check("scheduled PIREP filed", "logged" in page and f"CW{r1_out:04d}" in page)
check("profile shows 1:25", "1:25" in page)

# The freighter (id 2, cargo) is not eligible for a passenger route and is refused.
status, page = call(admin, "/pilots/log", {
    "mode": "scheduled", "route_id": "1", "aircraft_id": "2",
    "flight_date": "2026-06-10", "hours": "1", "minutes": "0", "remarks": "",
})
check("ineligible aircraft refused on a scheduled route",
      "eligible for this route" in page and "logged" not in page)

# Local Training PIREP: same airport, any aircraft, pilot-set 9900-9999 number.
# The "Dispatch local training" button on the route network pre-selects it.
status, page = call(admin, "/pilots/log?mode=training")
check("dispatch-training link opens a training-only form",
      "Dispatch a local training flight" in page and 'name="training_number"' in page
      and 'name="route_id"' not in page)
# The aircraft picker card sits outside the <form>; every radio must be
# linked back with form="training-form" or the selection is silently dropped.
check("training picker radios are linked to the training form",
      'id="training-form"' in page
      and page.count('form="training-form"') == page.count('class="fp-check"'))

# A local training flight departs and arrives at the same airport.
status, page = call(admin, "/pilots/log", {
    "mode": "training", "airport": "CYVR", "training_number": "9905",
    "aircraft_id": "3", "flight_date": "2026-06-10", "hours": "0",
    "minutes": "55", "remarks": "Circuits",
})
check("training PIREP filed with a 99xx number",
      "logged" in page and "CW9905" in page)
check("training flight is a local same-airport session",
      "CYVR → CYVR" in page)

# 9900 is the lower boundary of the training block (used to be scheduled).
status, page = call(admin, "/pilots/log", {
    "mode": "training", "airport": "CYYC", "training_number": "9900",
    "aircraft_id": "3", "flight_date": "2026-06-11", "hours": "1",
    "minutes": "5", "remarks": "Boundary training",
})
check("training number 9900 accepted", "logged" in page and "CW9900" in page)

# A training number outside the reserved block is rejected.
status, page = call(admin, "/pilots/log", {
    "mode": "training", "airport": "CYVR", "training_number": "8000",
    "aircraft_id": "3", "flight_date": "2026-06-10", "hours": "1",
    "minutes": "0", "remarks": "",
})
check("training number outside 9900-9999 rejected",
      "must be between 9900 and 9999" in page)

# --- second user joins as a Standard member --------------------------------
member = client()
status, page = call(member, "/register", {
    "first_name": "Rookie", "last_name": "Pilot",
    "email": "rookie@wingsofcanada.ca", "callsign_digits": "0042",
    "invite": mint_invite(),
    "password": "hunter2hunter2", "confirm": "hunter2hunter2",
})
check("second user joins on the Standard tier",
      "WOC0042" in page and "You can now fly the network" in page)

# Standard pilots can now create routes.
status, page = call(member, "/dispatch/new", {
    "dep": "CYEG", "arr": "KSEA", "route_type": "pax",
    "dep_time_h": "08", "dep_time_m": "30",
    "distance_nm": "", "duration": "", "notes": "", "create_return": "on",
})
check("standard member can create routes",
      "Route created" in page and "requires Administrator access" not in page)

status, page = call(member, "/dispatch/")
check("standard member does not see the Added by column",
      "Added by" not in page)
check("standard member sees the New route button on dispatch",
      "+ New route" in page)

status, page = call(member, "/dashboard")
check("standard member sees the New route button on the dashboard",
      "+ New route" in page and "Fly the network" in page)

# Dashboard now leads with latest flights + a top-pilots board + fleet status,
# and no longer shows the Latest routes panel.
status, page = call(admin, "/dashboard")
check("dashboard drops the Latest routes panel", "Latest routes" not in page)
check("dashboard shows the Top pilots leaderboard",
      "Top pilots" in page and "by flight hours" in page and 'class="leaderboard"' in page)
check("dashboard shows the Fleet status panel",
      "Fleet status" in page and "Total fleet" in page)
check("dashboard latest-flights feed is scrollable", 'class="flights-scroll"' in page)

status, page = call(member, "/admin/users")
check("standard member cannot open admin", "requires Administrator access" in page)

# Route 2 (the return leg of CYYZ->KJFK) is pax; C-FWOC (aircraft 1) is eligible.
status, page = call(member, "/pilots/log", {
    "mode": "scheduled", "route_id": "2", "aircraft_id": "1",
    "flight_date": "2026-06-10", "hours": "1", "minutes": "30", "remarks": "",
})
check("member can log scheduled flights", "logged" in page)

# A standard pilot can also file their own local training flight (any aircraft).
status, page = call(member, "/pilots/log", {
    "mode": "training", "airport": "CYYC", "training_number": "9912",
    "aircraft_id": "2", "flight_date": "2026-06-10", "hours": "1",
    "minutes": "5", "remarks": "Pilot training",
})
check("member can file a local training flight", "logged" in page and "CW9912" in page)

# --- admin: edit an existing route's dispatch details ------------------------
# Route 1 is the CYYZ->KJFK outbound. Editing is admin-only.
status, page = call(admin, "/dispatch/1/edit")
check("admin can open the route edit form",
      "Edit route" in page and "readonly" in page and "CYYZ" in page)

status, page = call(member, "/dispatch/1/edit")
check("standard member cannot edit routes",
      "requires Administrator access" in page)

# Valid edit: set a departure time, change the type and the notes.
status, page = call(admin, "/dispatch/1/edit", {
    "route_type": "pax",
    "dep_time_h": "09", "dep_time_m": "15",
    "distance_nm": "", "duration": "", "notes": "Edited by smoke",
})
check("admin edit saves the route", "updated" in page)
check("edited route shows the new departure time", "09:15" in page)

# --- route network: admin filters + everyone can sort -----------------------
# Filter by the pilot who added the route: only the member's CYEG<->KSEA pair
# (member is user 2; the admin created everything else).
status, page = call(admin, "/dispatch/?creator=2")
check("admin can filter routes by creator",
      "CYEG → KSEA" in page and "CYYZ → KJFK" not in page)

# Filter by departure airport.
status, page = call(admin, "/dispatch/?dep=CYVR")
check("admin can filter routes by departure airport",
      "CYVR →" in page and "KJFK → CYYZ" not in page)

# Filter by a distance range that the long 2500 nm route satisfies.
status, page = call(admin, "/dispatch/?dist_min=2000")
check("admin can filter routes by distance range", f"CW{long_out:04d}" in page)

# The structured filter panel is admin-only.
status, page = call(admin, "/dispatch/")
check("admin sees the route filter panel", 'name="dist_min"' in page)
status, page = call(member, "/dispatch/")
check("standard member does not see the filter panel",
      'name="dist_min"' not in page and "Added by" not in page)

# Sorting is open to everyone — a standard member can sort by distance.
status, page = call(member, "/dispatch/?sort=distance&dir=desc")
check("any pilot can sort the route network",
      status == 200 and 'class="sort-btn active' in page)

# --- fleet: detail page and aircraft images ---------------------------------
status, page = call(admin, "/fleet/")
check("fleet registration links to the detail page", 'href="/fleet/1"' in page)
status, page = call(admin, "/fleet/1")
check("aircraft detail page loads",
      "C-FWOC" in page and "Images" in page and "Details" in page)
status, page = call(member, "/fleet/1")
check("standard members can open aircraft details", "C-FWOC" in page)
status, page = call(member, "/fleet/1/images", {})
check("standard member cannot upload aircraft images",
      "requires Administrator access" in page)
status, page = call(admin, "/fleet/1/images", {})  # POST without a file
check("aircraft image upload with no file is rejected",
      "Choose an image to upload" in page)

# --- admin: member roster, search and sort ----------------------------------
status, page = call(admin, "/admin/users")
check("admin roster lists members", "WOC1" in page and "Member management" in page)
check("admin roster offers an add-pilot link", "/admin/users/new" in page)
check("admin roster has sort controls",
      "Flight Hours" in page and "Flights Completed" in page and "Date Joined" in page)

status, page = call(admin, "/admin/users?q=WOC1")
check("admin roster search filters by callsign", "WOC1" in page)
status, page = call(admin, "/admin/users?sort=joined")
check("admin roster accepts a sort option", status == 200)

# --- admin tier management via the second-stage edit page -------------------
status, page = call(admin, "/admin/users/2/edit")
check("admin can open the pilot edit page",
      'name="adj_flights"' in page and 'name="join_date"' in page
      and 'name="name"' in page)

status, page = call(admin, "/admin/users/2/edit", {"role": "admin"})
check("admin can promote a member", "Saved changes" in page)
status, page = call(admin, "/admin/users/2/edit", {"role": "standard"})
check("admin can demote back to standard", "Saved changes" in page)
status, page = call(admin, "/admin/users/2/edit", {"role": "visitor"})
check("removed visitor tier is rejected", "Unknown tier" in page)

# --- pilot roster no longer shows the Tier column ---------------------------
status, page = call(admin, "/pilots/")
check("pilot roster drops the Tier column", "<th>Tier</th>" not in page)

# --- admin: add a pilot (second-stage page) ---------------------------------
status, page = call(admin, "/admin/users/new")
check("add-pilot page offers account creation",
      "Create account" in page and 'name="name"' in page)

status, page = call(admin, "/admin/users/create", {
    "name": "New Hire", "email": "newhire@wingsofcanada.ca", "callsign_digits": "0077",
    "role": "standard", "password": "hunter2hunter2", "confirm": "hunter2hunter2",
})
check("admin creates a pilot account", "Created WOC0077" in page)
check("a created pilot's name shows in the roster", "New Hire" in page)

newhire = client()
status, page = call(newhire, "/login", {
    "ident": "WOC0077", "password": "hunter2hunter2",
})
check("created pilot can sign in", "Operations dashboard" in page)

# A blank password mints a temporary one for the admin to hand over.
status, page = call(admin, "/admin/users/create", {
    "email": "temp@wingsofcanada.ca", "callsign_digits": "0078", "role": "standard",
})
check("creating with a blank password mints a temporary one",
      "Temporary password:" in page and "WOC0078" in page)

status, page = call(admin, "/admin/users/create", {
    "email": "dupe@wingsofcanada.ca", "callsign_digits": "0077", "role": "standard",
    "password": "hunter2hunter2", "confirm": "hunter2hunter2",
})
check("admin create rejects a duplicate callsign", "already taken" in page)

# Email is optional - handy for throwaway test accounts.
status, page = call(admin, "/admin/users/create", {
    "callsign_digits": "0099", "role": "standard",
})
check("admin creates a test account with no email",
      "Created WOC0099 on the Standard tier" in page)

# --- admin: credit flights / hours via the edit page ------------------------
# Member (user 2) has logged 2 flights / 2:35; credit +10 flights and +5:00.
status, page = call(admin, "/admin/users/2/edit", {
    "adj_flights": "10", "adj_hours": "5", "adj_minutes": "0",
})
check("admin credits extra flights and hours", "Saved changes" in page)

status, page = call(admin, "/pilots/2")
check("credited totals show on the pilot profile", "7:35" in page)

status, page = call(admin, "/pilots/")
check("roster shows hours to one decimal place", "7.6" in page and "7:35" not in page)

status, page = call(admin, "/admin/users/2/edit", {"adj_flights": "-1"})
check("negative credit rejected", "cannot be negative" in page)

status, page = call(admin, "/admin/users/2/edit", {"adj_minutes": "75"})
check("out-of-range credited minutes rejected", "between 0 and 59" in page)

# --- admin: change a pilot's join date --------------------------------------
status, page = call(admin, "/admin/users/2/edit", {"join_date": "2015-03-01"})
check("admin can change a pilot's join date", "Saved changes" in page)
status, page = call(admin, "/admin/users")
check("the updated join date shows in the roster", "Mar 1, 2015" in page)
status, page = call(admin, "/admin/users/2/edit", {"join_date": "not-a-date"})
check("an invalid join date is rejected", "valid join date" in page)

# --- pilot ranks reflect combined hours + flights --------------------------
status, page = call(admin, "/pilots/")
check("roster shows a Rank column", "<th>Rank</th>" in page)
check("fresh accounts rank as Student Pilot", "Student Pilot" in page)

# Credit user 2 well past the top caps -> Fleet Captain (needs both metrics).
call(admin, "/admin/users/2/edit", {
    "adj_flights": "300", "adj_hours": "1200", "adj_minutes": "0",
})
status, page = call(admin, "/pilots/2")
check("a high-time pilot ranks as Fleet Captain", "Fleet Captain" in page)
check("rank badge is colour-coded by rank", "rank-fleet-captain" in page)

# Hours alone don't promote: 600 h but only 2 logged flights stays Student.
call(admin, "/admin/users/2/edit", {
    "adj_flights": "0", "adj_hours": "600", "adj_minutes": "0",
})
status, page = call(admin, "/pilots/2")
check("hours without flights are gated (still Student Pilot)",
      "Fleet Captain" not in page and "Student Pilot" in page)

# --- admin: site administration (cover + hero headings) ---------------------
status, page = call(admin, "/admin/site")
check("admin can open the site admin page",
      "Landing cover" in page and "Cover headings" in page and 'type="file"' in page)
status, page = call(member, "/admin/site")
check("standard member cannot open site admin",
      "requires Administrator access" in page)
status, page = call(admin, "/admin/site/cover", {})  # POST without a file
check("cover upload with no file is rejected",
      "Choose an image or video file" in page)
status, page = call(admin, "/admin/site/hero", {
    "title1": "Beyond the horizon", "title2": "", "subtitle": "Test subtitle",
})
check("admin can edit the cover headings", "Landing headings updated" in page)

# --- duplicate registration guards -----------------------------------------
rogue = client()
status, page = call(rogue, "/register", {
    "first_name": "Rogue", "last_name": "One",
    "email": "chief@wingsofcanada.ca", "callsign_digits": "9999",
    "invite": mint_invite(),
    "password": "hunter2hunter2", "confirm": "hunter2hunter2",
})
check("duplicate email rejected", "already registered" in page)

status, page = call(rogue, "/register", {
    "first_name": "Rogue", "last_name": "Two",
    "email": "other@wingsofcanada.ca", "callsign_digits": "0042",
    "invite": mint_invite(),
    "password": "hunter2hunter2", "confirm": "hunter2hunter2",
})
check("duplicate callsign rejected", "already taken" in page)

# --- anonymous visitors -----------------------------------------------------
anon = client()
status, page = call(anon, "/")
check("anonymous visitors get the public home page",
      status == 200 and "Operations status" in page and "Join the crew" in page)
check("landing page shows the cover hero with the admin-set heading",
      "landing-hero" in page and "Beyond the horizon" in page)
check("landing fleet showcase is hidden until aircraft have photos",
      "fleet-showcase" not in page and "Our fleet" not in page)
check("home page shows network status",
      f"CW{r1_out:04d}" in page and "Fleet status" in page)
check("home page offers no dispatch actions",
      "New route" not in page and "Log a flight" not in page)

status, page = call(anon, "/dispatch/")
check("member pages still require sign-in", "Crew sign in" in page)

status, page = call(anon, "/fleet/")
check("fleet page still requires sign-in", "Crew sign in" in page)

print()
if FAILED:
    print(f"{len(FAILED)} CHECK(S) FAILED: {FAILED}")
    raise SystemExit(1)
print(f"All {PASSED} smoke checks passed.")
