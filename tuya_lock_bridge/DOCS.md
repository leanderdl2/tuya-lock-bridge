# Tuya Lock Bridge

[![Add repository to your Home Assistant instance](https://my.home-assistant.io/badges/supervisor_add_addon_repository.svg)](https://my.home-assistant.io/redirect/supervisor_add_addon_repository/?repository_url=https%3A%2F%2Fgithub.com%2Fleanderdl2%2Ftuya-lock-bridge)

A local HTTP bridge that lets Home Assistant drive Tuya smart locks: unlock
remotely, and create, list and delete temporary access codes.

## Why this add-on exists

Tuya locks deliberately offer **no local API**. Where a Tuya bulb or plug is
happy to be driven over your own network, a lock refuses every local
connection: everything goes through Tuya's cloud, behind a ticket system and
AES encryption.

The Python libraries needed for that (`tuya-connector-python` and
`pycryptodome`) cannot be installed permanently inside the Home Assistant Core
container — an update wipes them. This add-on runs them in a container of its
own and offers Home Assistant a plain HTTP API instead.

Home Assistant's own Tuya integration will not fill this gap. Its documentation
states that every platform is supported *except* `lock` and `remote`, so an
access panel shows up there as a read-only state with no way to control it.

## Before you start

You need a project on the [Tuya IoT Platform](https://iot.tuya.com):

1. Create a **Cloud Development** project and link your Tuya/Smart Life app
   account.
2. Subscribe the project to the **Smart Lock Open Service** API group. Without
   that subscription every request comes back as an authorisation error.
3. Note the project's **Access ID** and **Access Secret**.
4. Look up the **device ID** of each lock under *Devices*. It is a string of
   roughly 22 characters, for example `bf1234567890abcdefghij`.

Mind the **data centre**: an account created in Europe belongs to
`openapi.tuyaeu.com`. Get this wrong and the API will not find your devices.

Keep an eye on your project's subscription too. The Trial Edition comes with a
monthly allowance and has to be extended periodically; when it lapses, every
API call stops working and so does this add-on.

## Configuration

| Option | Meaning |
|---|---|
| `access_id` | Access ID of your Tuya Cloud project |
| `access_secret` | Access Secret of the same project |
| `endpoint` | The data centre your account belongs to |
| `api_token` | A password of your own choosing that Home Assistant presents to this add-on. Pick a long random string. With no token set, the bridge refuses every request. |
| `time_zone` | The zone a recurring daily pattern is calculated in, for example `Europe/Amsterdam` |
| `mqtt_enabled` | Whether the bridge registers entities in Home Assistant |
| `mqtt_host` … `mqtt_password` | Leave empty to use the Home Assistant broker; Supervisor then supplies the details. Only fill these in for a broker elsewhere. |
| `refresh_minutes` | How often the code sensor refreshes. Every refresh is one call per lock to Tuya. |
| `locks` | Your locks. Per lock a `name` of your choosing, used in the URL, and the `device_id` from Tuya. |

Example:

```yaml
access_id: xxxxxxxxxxxxxxxxxxxx
access_secret: xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx
endpoint: https://openapi.tuyaeu.com
api_token: pick-something-long-and-random
locks:
  - name: front_door
    device_id: bf1234567890abcdefghij
  - name: back_door
    device_id: bf0987654321zyxwvutsrq
```

Names are lowercased. After starting, check `/rooms` to confirm your locks were
recognised.

## Entities in Home Assistant

The add-on registers itself over MQTT and creates two entities per lock.

| Entity | Does |
|---|---|
| `button.lock_<name>_open` | Opens the door |
| `sensor.lock_<name>_valid_codes` | How many codes are valid right now; the attributes hold the full list with name, window and status |

Both belong to the same device, so they end up together on the card of whatever
area you assign the lock to. There is nothing to set up: because the add-on
declares `mqtt:need`, Supervisor hands over the broker details. Using a broker
outside Home Assistant? Fill in `mqtt_host` and the fields beside it.

The entities carry an availability channel. If the add-on goes down or loses its
network, the broker publishes the last will and they turn grey in Home
Assistant — so you can see the bridge is gone, instead of pressing a button that
quietly does nothing.

The sensor refreshes every `refresh_minutes` minutes, and immediately after a
code is created or revoked through the bridge. That second path costs no extra
call on top of what was happening anyway.

Watch your Tuya project's monthly allowance: every refresh is one call per lock.
Two locks at five minutes adds up to some seventeen thousand calls a month.

## The panel

The add-on adds a **Locks** entry to the Home Assistant sidebar where you can
manage codes by hand: see what is on a lock, revoke a code, purge an expired
record, open a door, and create a code with a PIN and a window of your own.

Requests through the panel arrive via ingress, which means Home Assistant has
already authenticated the user and no API token is involved. That path is
recognised by the presence of the ingress header *and* a source address inside
Supervisor's own network, so nobody can imitate it by sending that header from
elsewhere.

## Endpoints

Port 8099 is **not** published on your network by default; the panel and the
entities do not need it. Map it under the add-on's *Network* section if you want
to reach the HTTP API from outside Home Assistant, and set an `api_token` before
you do. Every request except `/health` then requires the `X-Api-Token` header.

| Method | Path | Does |
|---|---|---|
| GET | `/health` | Status check; also reports how many locks are configured |
| GET | `/rooms` | The lock names that were recognised |
| POST | `/unlock/<lock>` | Opens the door |
| GET | `/codes/<lock>` | Every temporary code on that lock |
| POST | `/book` | Creates a code whose PIN is derived from a phone number |
| POST | `/codes/<lock>` | Creates a code with a PIN and optionally a pattern |
| DELETE | `/codes/<lock>/<id>` | Revokes a code |
| DELETE | `/codes/<lock>/<id>/record` | Removes the history record from the list |

The body of `/book`:

```json
{
  "room": "front_door",
  "last4": "1234",
  "name": "Booking-1234",
  "effective_time": 1788606000,
  "invalid_time": 1788688800
}
```

The PIN is assembled from the two-digit year of `effective_time` plus `last4`.
A check-in during 2026 gives `261234`, during 2027 `271234`. That always yields
six digits, which is what most keypads expect.

The body of `POST /codes/<lock>`:

```json
{
  "name": "Cleaner",
  "password": "445566",
  "effective_time": 1788606000,
  "invalid_time": 1791198000,
  "one_time": false,
  "schedule": { "days": [1, 2, 3, 4, 5], "from": "11:00", "until": "15:00" }
}
```

`effective_time` and `invalid_time` set the outer window: outside that period
the code does not exist. `schedule` carves a recurring daily pattern out of it;
the example above gives a code that works Monday through Friday between 11:00
and 15:00 until the window runs out. Weekdays run from 1 (Monday) through 7
(Sunday). Both fields are optional: without `schedule` the code is valid for the
whole period, and with `one_time` set to `true` it expires after a single use.

## Wiring it into Home Assistant

Only needed if you want to drive the bridge from automations rather than from
the panel. In `configuration.yaml`:

```yaml
rest_command:
  lock_unlock:
    url: "http://<home-assistant-ip>:8099/unlock/{{ room }}"
    method: POST
    headers:
      X-Api-Token: !secret lock_bridge_token
    timeout: 45
```

Keep the timeout generous. The first request after the add-on starts still has
to fetch a token from Tuya and takes noticeably longer; with a tight timeout it
is precisely that first call that fails every time.

## Tuya quirks worth knowing

These were found the hard way and cost real time otherwise.

**The encrypted PIN must be lowercase hex.** Tuya's API swallows uppercase
without complaint and answers `success: true`, but the lock then decodes it to
something other than your PIN. The code looks created and simply does not work.

**The ticket key is the full Access Secret.** `ticket_key` is decrypted with
AES-256-ECB using all 32 characters as the key; the result is a 16-character
string that then serves as the AES-128-ECB key for the PIN itself. Using the
first sixteen characters does not work and returns `param is illegal`.

**A deleted code does not leave the list.** `DELETE /codes/<lock>/<id>` revokes
the password, but the record stays and its `phase` then flips between 19 and 17.
Filter on that and you will delete the same codes again every day. Only
`DELETE /codes/<lock>/<id>/record` actually removes it — that is what the bin
icon in the Tuya app does.

**Codes at `phase: 12` can still vanish.** That is "waiting to sync with the
lock". If the confirmation never arrives, Tuya sometimes discards the code
within minutes. It happens especially with two codes sharing a name and a time
window, and then both disappear. So after creating one, read `/codes/<lock>`
back to check it is really there rather than trusting the response.

**The lock enforces expiry itself.** Revoking expired codes is pointless; only
clearing the history is worth doing.

### Daily patterns

A pattern travels to Tuya as `schedule_list`, and there are three traps in it.

Only **one block per code** fits. Two blocks in the list — a morning window and
an afternoon one, say — returns error code 1109.

The `all_day` flag must stay `false`. Set it to `true` and Tuya silently drops
the whole block: the code comes back without a `schedule_list` and is therefore
valid around the clock, the exact opposite of what you meant. Express a full day
as 00:00 to 23:59.

**Reading it back gives different numbers than you sent.** Times go in as
minutes after midnight (660 for 11:00) and come out as HHMM (1100). And
`working_day` returns with its bit order reversed: on the way in Sunday is bit 0
and Monday bit 1, in the response Sunday is 128 and Monday 64. Measured by
sending and immediately re-reading: 2 became 64, 8 became 16, 127 became 254.

Tuya's documentation says daily patterns are supported only by Zigbee
residential lock pro and hotel lock. The cloud does accept and store them for
other locks, but that is no guarantee your lock enforces them. Test a new
pattern once outside its window before relying on it.

## Troubleshooting

**`unauthorized`** — the `X-Api-Token` header is missing or does not match the
add-on configuration. The log line records the source address and whether the
ingress header was present, which usually points straight at the cause.

**`unknown lock`** — the name in the URL is not in `locks`. Check `/rooms`.

**`param is illegal` on `/book`** — usually a PIN of a length the lock will not
accept. Many keypads are fixed at six digits.

**Authorisation errors on everything** — the Tuya project is not subscribed to
the Smart Lock Open Service, or the trial subscription has expired.

**The panel returns 401** — the bridge only treats a request as ingress when it
arrives from Supervisor's network. Check the add-on log: the refusal line names
the source address it actually saw.
