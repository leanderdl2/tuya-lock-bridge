"""
Tuya Lock Bridge - lokale HTTP-brug tussen Home Assistant en Tuya smart locks.

Tuya-sloten bieden bewust geen lokale API: alles loopt via Tuya's cloud met
een ticket-systeem en AES-versleuteling. De daarvoor benodigde Python-
dependencies (tuya-connector-python, pycryptodome) kun je niet persistent in
de Home Assistant Core-container installeren, vandaar deze add-on. Home
Assistant praat er via rest_command: mee op poort 8099.

Encryptie-aanpak (fysiek bevestigd werkend op een Nivian NV-ACCESS-PIN-RFID-W):
    1. Decrypt de ontvangen ticket_key (hex) met AES-256-ECB, met de
       VOLLEDIGE Tuya Access Secret (32 tekens, UTF-8) als sleutel.
       Resultaat na PKCS7-unpadding is een 16 tekens lange string.
    2. Die string is de AES-128-ECB sleutel om de pincode mee te
       versleutelen (PKCS7-padding). BELANGRIJK: de hex-output moet in
       KLEINE letters - hoofdletters wordt door Tuya's API geaccepteerd
       (success:true) maar decodeert op het fysieke slot niet naar de
       juiste pincode. Dit kostte veel mensen op Tuya's forum dagen.

Zie DOCS.md voor de rest van de gevonden eigenaardigheden.
"""

import ipaddress
import json
import logging
import os
import threading
import time
from datetime import datetime

import requests
from flask import Flask, jsonify, request
from Crypto.Cipher import AES
from Crypto.Util.Padding import pad, unpad
from tuya_connector import TuyaOpenAPI

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("tuya_lock_bridge")

with open("/data/options.json", encoding="utf-8") as f:
    CFG = json.load(f)


def _load_locks(cfg):
    """Bouwt {naam: device_id} uit de add-on-opties.

    Namen worden kleingemaakt zodat de URL-paden voorspelbaar blijven;
    lege of dubbele regels worden overgeslagen met een waarschuwing.
    """
    locks = {}
    for entry in cfg.get("locks") or []:
        naam = (entry.get("name") or "").strip().lower()
        device_id = (entry.get("device_id") or "").strip()
        if not naam or not device_id:
            log.warning("Slot overgeslagen: naam of device_id ontbreekt (%s)", entry)
            continue
        if naam in locks:
            log.warning("Dubbele slotnaam '%s' - alleen de eerste wordt gebruikt", naam)
            continue
        locks[naam] = device_id
    return locks


DEVICES = _load_locks(CFG)

if not DEVICES:
    log.error(
        "Geen sloten geconfigureerd. Vul bij de add-on-configuratie minstens "
        "een slot in met een naam en het Tuya device-ID."
    )
if not CFG.get("api_token"):
    log.error(
        "Geen api_token ingesteld - alle verzoeken worden geweigerd. Kies zelf "
        "een lange willekeurige tekst en gebruik dezelfde in Home Assistant."
    )

app = Flask(__name__)
_api = None

# Er praten meerdere threads met Tuya: de webserver handelt verzoeken af terwijl
# de MQTT-lus periodiek de codelijsten ophaalt en een druk op de knop-entiteit
# in een eigen thread wordt afgewikkeld. TuyaOpenAPI houdt zijn toegangstoken in
# het object bij en ververst dat als het verlopen is; gebeurt dat terwijl een
# andere thread hetzelfde object gebruikt, dan krijg je fouten die zich niet
# netjes laten nabootsen. Alles wat met Tuya praat gaat daarom door dit slot.
#
# Het is een RLock omdat een handeling soms uit meerdere aanroepen bestaat die
# bij elkaar horen: een ticket ophalen en het meteen gebruiken moet ondeelbaar
# zijn, anders kan een tweede thread er met het ticket vandoor.
TUYA_LOCK = threading.RLock()


def get_api():
    with TUYA_LOCK:
        global _api
        if _api is None:
            _api = TuyaOpenAPI(CFG["endpoint"], CFG["access_id"], CFG["access_secret"])
            _api.connect()
        return _api


def resolve_device(room):
    device_id = DEVICES.get((room or "").strip().lower())
    if not device_id:
        raise ValueError(
            f"onbekend slot '{room}' - geconfigureerd zijn: "
            f"{', '.join(sorted(DEVICES)) or '(geen)'}"
        )
    return device_id


def get_ticket(api, device_id):
    resp = api.post(f"/v1.0/smart-lock/devices/{device_id}/password-ticket")
    if not resp.get("success"):
        raise RuntimeError(f"ticket ophalen mislukt: {resp}")
    return resp["result"]


def decrypt_ticket_key(ticket_key_hex, access_secret):
    key = access_secret.encode("utf-8")
    cipher = AES.new(key, AES.MODE_ECB)
    decrypted = unpad(cipher.decrypt(bytes.fromhex(ticket_key_hex)), AES.block_size)
    return decrypted.decode("utf-8")


def encrypt_password(password_plain, ticket_key_str):
    key = ticket_key_str.encode("utf-8")
    cipher = AES.new(key, AES.MODE_ECB)
    padded = pad(password_plain.encode("utf-8"), AES.block_size)
    return cipher.encrypt(padded).hex()  # kleine letters - bevestigd nodig


def parse_tijdstip(waarde):
    """Zet '11:00' of een aantal minuten na middernacht om naar minuten."""
    if isinstance(waarde, bool):
        raise ValueError("ongeldig tijdstip")
    if isinstance(waarde, (int, float)):
        minuten = int(waarde)
    else:
        tekst = str(waarde).strip()
        if ":" in tekst:
            uur, minuut = tekst.split(":", 1)
            minuten = int(uur) * 60 + int(minuut)
        else:
            minuten = int(tekst)
    if not 0 <= minuten <= 1439:
        raise ValueError(f"tijdstip '{waarde}' valt buiten 00:00-23:59")
    return minuten


def build_schedule(schedule):
    """Vertaalt een dagpatroon naar Tuya's schedule_list.

    Wij nemen weekdagen aan als 1=maandag t/m 7=zondag (ISO). Tuya wil een
    bitmasker waarin zondag bit 0 is, maandag bit 1, enzovoort. Let op bij het
    teruglezen: de API geeft het masker in de OMGEKEERDE bitvolgorde terug
    (zondag wordt daar 128, maandag 64, ... zaterdag 2), dus de waarde die je
    terugkrijgt is niet hetzelfde getal als wat je verstuurde. Vastgesteld door
    te versturen en meteen weer op te halen: 2 -> 64, 8 -> 16, 127 -> 254.

    Er past maar een blok per code: twee blokken meesturen geeft foutcode 1109.
    """
    dagen = schedule.get("days") or []
    if not dagen:
        raise ValueError("kies minstens een weekdag voor het dagpatroon")
    masker = 0
    for dag in dagen:
        dag = int(dag)
        if not 1 <= dag <= 7:
            raise ValueError(
                f"ongeldige weekdag {dag} - gebruik 1 (maandag) t/m 7 (zondag)"
            )
        masker |= 1 << (dag % 7)
    van = parse_tijdstip(schedule.get("from", 0))
    tot = parse_tijdstip(schedule.get("until", 1439))
    if tot <= van:
        raise ValueError("de eindtijd van het dagpatroon ligt niet na de begintijd")
    # all_day bewust op false: met true laat Tuya het hele blok vallen (de code
    # komt dan zonder schedule_list terug en is dus de klok rond geldig). Een
    # volle dag geef je daarom op als 00:00 tot 23:59.
    return [
        {
            "effective_time": van,
            "invalid_time": tot,
            "working_day": masker,
            "all_day": False,
        }
    ]


def maak_code(
    device_id,
    password_plain,
    name,
    effective_time,
    invalid_time,
    schedule=None,
    one_time=False,
):
    """Zet een tijdelijke code in het slot.

    effective_time/invalid_time zijn unix-tijden en bepalen het buitenste
    venster: buiten die periode bestaat de code niet. Het optionele schedule
    knipt daarbinnen een terugkerend dagpatroon uit, bijvoorbeeld elke dag van
    11:00 tot 15:00.
    """
    if invalid_time <= effective_time:
        raise ValueError("de einddatum ligt niet na de begindatum")

    with TUYA_LOCK:
        api = get_api()
        ticket = get_ticket(api, device_id)
        ticket_key = decrypt_ticket_key(ticket["ticket_key"], CFG["access_secret"])

        body = {
        "password": encrypt_password(password_plain, ticket_key),
        "password_type": "ticket",
        "ticket_id": ticket["ticket_id"],
            "effective_time": effective_time,
            "invalid_time": invalid_time,
            "name": name,
            # 0 = zo vaak te gebruiken als je wilt binnen de geldigheid,
            # 1 = na een keer gebruiken vervalt de code.
            "type": 1 if one_time else 0,
        }
        if schedule:
            body["schedule_list"] = build_schedule(schedule)
            body["time_zone"] = CFG.get("time_zone") or "Europe/Amsterdam"

        return api.post(f"/v1.0/devices/{device_id}/door-lock/temp-password", body)


def ontgrendel(device_id):
    """Opent de deur zonder pincode, via een eenmalig ticket."""
    with TUYA_LOCK:
        api = get_api()
        ticket = get_ticket(api, device_id)
        return api.post(
            f"/v1.1/devices/{device_id}/door-lock/password-free/open-door",
            {"ticket_id": ticket["ticket_id"], "channel_id": 1},
        )


def haal_codes(device_id):
    """Alle tijdelijke codes van een slot, zoals Tuya ze teruggeeft."""
    with TUYA_LOCK:
        resp = get_api().get(f"/v1.0/devices/{device_id}/door-lock/temp-passwords")
    if not resp.get("success"):
        raise RuntimeError(f"codelijst ophalen mislukt: {resp}")
    return resp.get("result") or []


def code_status(code, nu):
    """Zelfde indeling als het paneel toont.

    phase 17 betekent ingetrokken, 12 dat het slot de code nog moet oppikken.
    Verlopen leiden we af uit de tijden en niet uit de phase, want die wipt bij
    verlopen codes heen en weer tussen 19 en 17.
    """
    if code.get("phase") == 17:
        return "ingetrokken"
    if code.get("invalid_time", 0) < nu:
        return "verlopen"
    if code.get("effective_time", 0) > nu:
        return "gepland"
    if code.get("phase") == 12:
        return "wacht op slot"
    return "actief"


# Het interne Docker-netwerk van Home Assistant is 172.30.32.0/23. Supervisor en
# Home Assistant Core krijgen daarin de lage adressen (172.30.32.x); add-ons
# zelf komen in 172.30.33.x terecht. Door alleen het eerste blok te vertrouwen
# kan een andere add-on op dezelfde machine zich niet voordoen als ingress.
SUPERVISOR_NET = ipaddress.ip_network("172.30.32.0/24")


def is_ingress(req):
    """Waar als dit verzoek via Home Assistant's ingress binnenkomt.

    De X-Ingress-Path header alleen is niet genoeg. Wie poort 8099 op zijn
    netwerk publiceert kan die header van buitenaf gewoon meesturen, en dan zou
    iedereen op het LAN de deur kunnen openen. Daarom eisen we er het bronadres
    van Supervisor bij; dat is van buiten het interne netwerk niet te
    vervalsen, want het is het adres van een bestaande TCP-verbinding.
    """
    if not req.headers.get("X-Ingress-Path"):
        return False
    try:
        return ipaddress.ip_address(req.remote_addr or "") in SUPERVISOR_NET
    except ValueError:
        return False


@app.before_request
def check_auth():
    if request.path == "/health":
        return None
    # Via ingress heeft Home Assistant de gebruiker al ingelogd.
    if is_ingress(request):
        return None
    token = request.headers.get("X-Api-Token", "")
    if not CFG.get("api_token") or token != CFG["api_token"]:
        # Het bronadres erbij loggen: gaat het paneel onverwacht op 401, dan is
        # aan deze regel meteen te zien of Supervisor vanaf een ander adres
        # binnenkomt dan het netwerk hierboven, in plaats van dat je moet gokken.
        log.warning(
            "geweigerd: %s %s vanaf %s (ingress-header %s, token %s)",
            request.method,
            request.path,
            req_adres(),
            "aanwezig" if request.headers.get("X-Ingress-Path") else "afwezig",
            "onjuist" if token else "ontbreekt",
        )
        return jsonify({"success": False, "error": "unauthorized"}), 401


def req_adres():
    return request.remote_addr or "onbekend"


@app.errorhandler(ValueError)
def handle_value_error(e):
    """Foute invoer is geen serverfout: geef 400 met een leesbare uitleg, zodat
    het paneel de melding rechtstreeks kan tonen."""
    return jsonify({"success": False, "error": str(e)}), 400


@app.errorhandler(Exception)
def handle_error(e):
    log.exception("onverwachte fout")
    return jsonify({"success": False, "error": str(e)}), 500


@app.route("/unlock/<room>", methods=["POST"])
def unlock(room):
    return jsonify(ontgrendel(resolve_device(room)))


@app.route("/book", methods=["POST"])
def book():
    data = request.get_json(force=True)
    device_id = resolve_device(data["room"])
    last4 = str(data["last4"]).zfill(4)
    name = data.get("name") or f"Boeking-{last4}"
    effective_time = int(data["effective_time"])
    invalid_time = int(data["invalid_time"])

    # De pincode is het jaartal van de INCHECKDATUM (twee cijfers) gevolgd door
    # de laatste vier cijfers van het telefoonnummer: 2026 -> 26xxxx,
    # 2027 -> 27xxxx, enzovoort. Bewust afgeleid van effective_time en niet van
    # de huidige datum, zodat een boeking die in december wordt aangemaakt voor
    # een incheck in januari het jaartal van de incheck krijgt.
    jaar_prefix = datetime.fromtimestamp(effective_time).strftime("%y")
    password_plain = jaar_prefix + last4

    resp = maak_code(device_id, password_plain, name, effective_time, invalid_time)
    meld_wijziging(data["room"])
    return jsonify(resp)


@app.route("/codes/<room>", methods=["POST"])
def create_code(room):
    """Maakt een code aan met een zelfgekozen pincode.

    Verschil met /book: daar wordt de pincode samengesteld uit het jaartal van
    de incheckdatum plus de laatste vier cijfers van een telefoonnummer. Hier
    geef je de pincode zelf op, wat handiger is voor handmatig beheer.

    Optioneel:
      one_time  true = de code vervalt na een keer gebruiken.
      schedule  {"days": [1,2,3,4,5], "from": "11:00", "until": "15:00"} voor
                een terugkerend dagpatroon binnen het opgegeven venster.
                Weekdagen: 1 = maandag t/m 7 = zondag.
    """
    data = request.get_json(force=True)
    device_id = resolve_device(room)
    password_plain = str(data["password"]).strip()
    name = (data.get("name") or "").strip() or f"Code-{password_plain[-4:]}"
    effective_time = int(data["effective_time"])
    invalid_time = int(data["invalid_time"])

    if not password_plain.isdigit():
        raise ValueError("pincode mag alleen cijfers bevatten")

    resp = maak_code(
        device_id,
        password_plain,
        name,
        effective_time,
        invalid_time,
        schedule=data.get("schedule"),
        one_time=bool(data.get("one_time")),
    )
    meld_wijziging(room)
    return jsonify(resp)


@app.route("/codes/<room>", methods=["GET"])
def list_codes(room):
    device_id = resolve_device(room)
    with TUYA_LOCK:
        resp = get_api().get(f"/v1.0/devices/{device_id}/door-lock/temp-passwords")
    return jsonify(resp)


@app.route("/codes/<room>/<password_id>", methods=["DELETE"])
def delete_code(room, password_id):
    device_id = resolve_device(room)
    with TUYA_LOCK:
        resp = get_api().delete(
            f"/v1.0/devices/{device_id}/door-lock/temp-passwords/{password_id}"
        )
    meld_wijziging(room)
    return jsonify(resp)


@app.route("/codes/<room>/<password_id>/record", methods=["DELETE"])
def delete_code_record(room, password_id):
    """Verwijdert het historie-record van een tijdelijke code.

    Let op het verschil met delete_code() hierboven:
      DELETE .../temp-passwords/{id}         trekt het wachtwoord in, maar het
                                             record blijft in de lijst staan en
                                             wipt daarna tussen phase 19 en 17.
      DELETE .../temp-passwords/{id}/record  haalt het record echt weg - dit is
                                             wat het prullenbak-icoontje in de
                                             Tuya-app doet.
    """
    device_id = resolve_device(room)
    with TUYA_LOCK:
        resp = get_api().delete(
            f"/v1.0/devices/{device_id}/door-lock/temp-passwords/{password_id}/record"
        )
    meld_wijziging(room)
    return jsonify(resp)


PANEL_HTML = """<!doctype html>
<html lang="nl">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Sloten</title>
<style>
  :root { color-scheme: light dark; --line:#d5d8dd; --muted:#6b7280; --bg:#fff; --fg:#111; --accent:#03a9f4; }
  @media (prefers-color-scheme: dark) {
    :root { --line:#3a3f46; --muted:#9aa3ad; --bg:#111418; --fg:#e6e8eb; }
  }
  body { margin:0; padding:16px; font:14px/1.5 system-ui, sans-serif; background:var(--bg); color:var(--fg); }
  h1 { font-size:20px; margin:0 0 16px; }
  h2 { font-size:15px; margin:24px 0 8px; }
  .card { border:1px solid var(--line); border-radius:10px; padding:14px; margin-bottom:16px; }
  table { width:100%; border-collapse:collapse; }
  th, td { text-align:left; padding:7px 6px; border-bottom:1px solid var(--line); }
  th { color:var(--muted); font-weight:600; font-size:12px; text-transform:uppercase; letter-spacing:.03em; }
  td.num { font-variant-numeric:tabular-nums; white-space:nowrap; }
  .tag { display:inline-block; padding:1px 8px; border-radius:99px; font-size:12px; border:1px solid var(--line); }
  .ok { color:#0a7c2f; border-color:#0a7c2f; }
  .wait { color:#b26a00; border-color:#b26a00; }
  .off { color:var(--muted); }
  label { display:block; font-size:12px; color:var(--muted); margin-bottom:3px; }
  input, select { width:100%; padding:7px 8px; border:1px solid var(--line); border-radius:6px;
                  background:var(--bg); color:var(--fg); font:inherit; box-sizing:border-box; }
  .row { display:flex; gap:10px; flex-wrap:wrap; margin-bottom:10px; }
  .row > div { flex:1 1 150px; }
  button { padding:8px 14px; border:0; border-radius:6px; background:var(--accent); color:#fff;
           font:inherit; cursor:pointer; }
  button.sec { background:transparent; color:var(--accent); border:1px solid var(--accent); padding:4px 10px; }
  button:disabled { opacity:.5; cursor:default; }
  .dagen { display:flex; gap:6px; flex-wrap:wrap; }
  .dagen label { display:flex; align-items:center; gap:5px; margin:0; padding:6px 10px;
                 border:1px solid var(--line); border-radius:6px; color:var(--fg); font-size:13px;
                 cursor:pointer; user-select:none; }
  .dagen input { width:auto; }
  td small { color:var(--muted); display:block; }
  #msg { padding:10px 12px; border-radius:6px; margin-bottom:14px; display:none; }
  #msg.err { background:#fdecec; color:#8a1c1c; display:block; }
  #msg.good { background:#e8f5ec; color:#0a5c26; display:block; }
  @media (prefers-color-scheme: dark) {
    #msg.err { background:#3a1c1c; color:#ffb4b4; }
    #msg.good { background:#12331f; color:#9fe6b8; }
  }
</style>
</head>
<body>
<h1>Toegangscodes</h1>
<div id="msg"></div>

<div class="card">
  <div class="row" style="margin-bottom:0; align-items:flex-end">
    <div><label for="lock">Slot</label><select id="lock"></select></div>
    <div style="flex:0 0 auto"><button id="open">Deur openen</button></div>
  </div>
</div>

<h2>Actieve codes</h2>
<div class="card">
  <table>
    <thead><tr><th>Naam</th><th>Geldig van</th><th>Geldig tot</th><th>Status</th><th></th></tr></thead>
    <tbody id="rows"><tr><td colspan="5">Laden…</td></tr></tbody>
  </table>
</div>

<h2>Nieuwe code</h2>
<div class="card">
  <div class="row">
    <div><label for="naam">Naam</label><input id="naam" placeholder="Schoonmaker"></div>
    <div><label for="pin">Pincode</label><input id="pin" inputmode="numeric" placeholder="123456"></div>
  </div>
  <div class="row">
    <div><label for="van">Geldig vanaf</label><input id="van" type="datetime-local"></div>
    <div><label for="tot">Geldig tot</label><input id="tot" type="datetime-local"></div>
  </div>
  <div class="row">
    <div>
      <label for="patroon">Patroon</label>
      <select id="patroon">
        <option value="doorlopend">Doorlopend geldig in die periode</option>
        <option value="dagelijks">Elke dag een vast tijdvenster</option>
        <option value="dagen">Alleen op gekozen weekdagen</option>
        <option value="eenmalig">Eenmalig - vervalt na gebruik</option>
      </select>
    </div>
  </div>
  <div class="row" id="dagenrij" hidden>
    <div style="flex:1 1 100%"><label>Dagen</label><div id="dagen" class="dagen"></div></div>
  </div>
  <div class="row" id="urenrij" hidden>
    <div><label for="dagvan">Elke dag vanaf</label><input id="dagvan" type="time" value="11:00"></div>
    <div><label for="dagtot">Elke dag tot</label><input id="dagtot" type="time" value="15:00"></div>
  </div>
  <button id="add">Code aanmaken</button>
</div>

<script>
const base = location.pathname.replace(/\\/$/, '') + '/';
const $ = id => document.getElementById(id);

function melding(tekst, soort) {
  const m = $('msg');
  m.textContent = tekst;
  m.className = soort;
  if (soort === 'good') setTimeout(() => { m.className = ''; }, 6000);
}

async function api(pad, opties) {
  const r = await fetch(base + pad, opties);
  const j = await r.json();
  if (!r.ok || j.success === false) {
    throw new Error(j.error || j.msg || ('HTTP ' + r.status));
  }
  return j;
}

const fmt = ts => new Date(ts * 1000).toLocaleString('nl-NL',
  { day: '2-digit', month: '2-digit', hour: '2-digit', minute: '2-digit' });

// Weekdagen zoals wij ze versturen (1 = maandag t/m 7 = zondag) en zoals Tuya
// ze TERUGGEEFT. Dat zijn verschillende bitwaarden: bij het versturen is
// zondag bit 0, in het antwoord is de bitvolgorde omgedraaid en is zondag 128.
const DAGEN = [
  { nr: 1, naam: 'ma', terug: 64 },
  { nr: 2, naam: 'di', terug: 32 },
  { nr: 3, naam: 'wo', terug: 16 },
  { nr: 4, naam: 'do', terug: 8 },
  { nr: 5, naam: 'vr', terug: 4 },
  { nr: 6, naam: 'za', terug: 2 },
  { nr: 7, naam: 'zo', terug: 128 }
];

// De uren in een teruggelezen schedule staan als HHMM: 1100 betekent 11:00.
const uur = v => String(v).padStart(4, '0').replace(/(\\d\\d)(\\d\\d)/, '$1:$2');

function patroonTekst(code) {
  const s = (code.schedule_list || [])[0];
  const eenmalig = code.type === 1 ? 'eenmalig' : '';
  if (!s) return eenmalig;
  const dagen = DAGEN.filter(d => s.working_day & d.terug).map(d => d.naam);
  const welke = dagen.length === 7 ? 'elke dag' : dagen.join(' ');
  const tijd = uur(s.effective_time) + '-' + uur(s.invalid_time);
  return [welke + ' ' + tijd, eenmalig].filter(Boolean).join(', ');
}

function status(code, nu) {
  if (code.phase === 17) return ['ingetrokken', 'off'];
  if (code.invalid_time < nu) return ['verlopen', 'off'];
  if (code.effective_time > nu) return ['gepland', 'wait'];
  if (code.phase === 12) return ['wacht op slot', 'wait'];
  return ['actief', 'ok'];
}

async function laadCodes() {
  const slot = $('lock').value;
  if (!slot) return;
  $('rows').innerHTML = '<tr><td colspan="5">Laden…</td></tr>';
  try {
    const j = await api('codes/' + slot);
    const nu = Math.floor(Date.now() / 1000);
    const lijst = (j.result || []).slice().sort((a, b) => b.effective_time - a.effective_time);
    if (!lijst.length) {
      $('rows').innerHTML = '<tr><td colspan="5">Geen codes op dit slot.</td></tr>';
      return;
    }
    $('rows').innerHTML = '';
    for (const c of lijst) {
      const [tekst, klasse] = status(c, nu);
      const patroon = patroonTekst(c);
      const tr = document.createElement('tr');
      tr.innerHTML = '<td></td>'
        + '<td class="num">' + fmt(c.effective_time) + '</td>'
        + '<td class="num">' + fmt(c.invalid_time) + '</td>'
        + '<td><span class="tag ' + klasse + '">' + tekst + '</span></td>'
        + '<td style="text-align:right"></td>';
      // Naam via textContent, niet via innerHTML: die komt uit de Tuya-cloud
      // en kan dus van alles bevatten.
      tr.firstElementChild.textContent = c.name;
      if (patroon) {
        const s = document.createElement('small');
        s.textContent = patroon;
        tr.firstElementChild.appendChild(s);
      }
      const knop = document.createElement('button');
      knop.className = 'sec';
      knop.textContent = c.invalid_time < nu || c.phase === 17 ? 'Uit lijst' : 'Intrekken';
      knop.onclick = () => verwijder(slot, c, knop);
      tr.lastElementChild.appendChild(knop);
      $('rows').appendChild(tr);
    }
  } catch (e) {
    melding('Ophalen mislukt: ' + e.message, 'err');
    $('rows').innerHTML = '<tr><td colspan="5">Kon de codes niet ophalen.</td></tr>';
  }
}

async function verwijder(slot, code, knop) {
  const verlopen = code.invalid_time < Math.floor(Date.now() / 1000) || code.phase === 17;
  const vraag = verlopen
    ? 'Record van "' + code.name + '" uit de lijst verwijderen?'
    : 'Code "' + code.name + '" intrekken? Die werkt daarna niet meer.';
  if (!confirm(vraag)) return;
  knop.disabled = true;
  try {
    const pad = 'codes/' + slot + '/' + code.id + (verlopen ? '/record' : '');
    await api(pad, { method: 'DELETE' });
    melding(verlopen ? 'Record verwijderd.' : 'Code ingetrokken.', 'good');
    laadCodes();
  } catch (e) {
    melding('Mislukt: ' + e.message, 'err');
    knop.disabled = false;
  }
}

$('open').onclick = async () => {
  const slot = $('lock').value;
  if (!slot || !confirm('Het slot van "' + slot + '" nu openen?')) return;
  $('open').disabled = true;
  try {
    await api('unlock/' + slot, { method: 'POST' });
    melding('Deur geopend.', 'good');
  } catch (e) {
    melding('Openen mislukt: ' + e.message, 'err');
  }
  $('open').disabled = false;
};

// Dagvakjes opbouwen, standaard alle dagen aan.
$('dagen').innerHTML = DAGEN.map(d =>
  '<label><input type="checkbox" value="' + d.nr + '" checked>' + d.naam + '</label>').join('');

function toonPatroonVelden() {
  const p = $('patroon').value;
  $('dagenrij').hidden = p !== 'dagen';
  $('urenrij').hidden = p !== 'dagen' && p !== 'dagelijks';
}
$('patroon').onchange = toonPatroonVelden;
toonPatroonVelden();

$('add').onclick = async () => {
  const slot = $('lock').value;
  const pin = $('pin').value.trim();
  const van = $('van').value, tot = $('tot').value;
  if (!/^[0-9]+$/.test(pin)) return melding('Vul een pincode van alleen cijfers in.', 'err');
  if (!van || !tot) return melding('Vul een begin- en eindtijd in.', 'err');
  const body = {
    name: $('naam').value.trim(),
    password: pin,
    effective_time: Math.floor(new Date(van).getTime() / 1000),
    invalid_time: Math.floor(new Date(tot).getTime() / 1000)
  };
  const p = $('patroon').value;
  if (p === 'eenmalig') {
    body.one_time = true;
  } else if (p === 'dagelijks' || p === 'dagen') {
    const dagen = p === 'dagelijks'
      ? DAGEN.map(d => d.nr)
      : [...$('dagen').querySelectorAll('input:checked')].map(i => Number(i.value));
    if (!dagen.length) return melding('Kies minstens een weekdag.', 'err');
    if (!$('dagvan').value || !$('dagtot').value) {
      return melding('Vul het dagelijkse tijdvenster in.', 'err');
    }
    body.schedule = { days: dagen, from: $('dagvan').value, until: $('dagtot').value };
  }
  $('add').disabled = true;
  try {
    await api('codes/' + slot, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body)
    });
    melding('Code aangemaakt. Het duurt 1 tot 2 minuten voor het slot hem kent.', 'good');
    $('naam').value = ''; $('pin').value = '';
    laadCodes();
  } catch (e) {
    melding('Aanmaken mislukt: ' + e.message, 'err');
  }
  $('add').disabled = false;
};

(async () => {
  try {
    const j = await api('rooms');
    if (!j.rooms.length) {
      return melding('Nog geen sloten geconfigureerd. Vul ze in bij de add-on-configuratie.', 'err');
    }
    $('lock').innerHTML = j.rooms.map(r => '<option>' + r + '</option>').join('');
    $('lock').onchange = laadCodes;
    // standaard: vanaf nu tot morgen middag
    const p = n => String(n).padStart(2, '0');
    const iso = d => d.getFullYear() + '-' + p(d.getMonth() + 1) + '-' + p(d.getDate())
                     + 'T' + p(d.getHours()) + ':' + p(d.getMinutes());
    const nu = new Date(), morgen = new Date(Date.now() + 864e5);
    morgen.setHours(12, 0, 0, 0);
    $('van').value = iso(nu);
    $('tot').value = iso(morgen);
    laadCodes();
  } catch (e) {
    melding('Kon de sloten niet ophalen: ' + e.message, 'err');
  }
})();
</script>
</body>
</html>"""


@app.route("/", methods=["GET"])
def panel():
    return PANEL_HTML, 200, {"Content-Type": "text/html; charset=utf-8"}


@app.route("/rooms", methods=["GET"])
def rooms():
    """Toont welke sloten geconfigureerd zijn - handig om te controleren of de
    namen in je rest_command overeenkomen met de add-on-configuratie."""
    return jsonify({"success": True, "rooms": sorted(DEVICES)})


@app.route("/health", methods=["GET"])
def health():
    return jsonify(
        {
            "success": True,
            "status": "ok",
            "locks_configured": len(DEVICES),
            "token_set": bool(CFG.get("api_token")),
        }
    )


# ---------------------------------------------------------------------------
# MQTT: entiteiten in Home Assistant
# ---------------------------------------------------------------------------
#
# Home Assistant's eigen Tuya-integratie ondersteunt het lock-platform niet - in
# de documentatie staat letterlijk dat alle platforms ondersteund worden behalve
# lock en remote. Voor een toegangspaneel krijg je daar dus alleen een toestand
# te zien en geen bediening. Wat deze add-on hieronder publiceert vult dat gat:
# een knop om te openen en een sensor met de codes, die naast de bestaande
# entiteiten van de officiele integratie komen te staan.

MQTT_BASIS = "tuya_lock_bridge"
STATUS_TOPIC = f"{MQTT_BASIS}/status"

_mqtt_client = None


def mqtt_instellingen():
    """Brokergegevens: eerst uit de add-on-opties, anders van Supervisor.

    Wie de Mosquitto-add-on gebruikt hoeft niets in te vullen. Doordat deze
    add-on 'mqtt:need' declareert geeft Supervisor host, poort en inloggegevens
    door. De handmatige velden zijn er voor een broker buiten Home Assistant.
    """
    host = (CFG.get("mqtt_host") or "").strip()
    if host:
        return {
            "host": host,
            "port": int(CFG.get("mqtt_port") or 1883),
            "username": CFG.get("mqtt_username") or "",
            "password": CFG.get("mqtt_password") or "",
        }

    token = os.environ.get("SUPERVISOR_TOKEN")
    if not token:
        raise RuntimeError(
            "geen SUPERVISOR_TOKEN gevonden en geen mqtt_host ingevuld - vul de "
            "brokergegevens handmatig in bij de add-on-configuratie"
        )
    r = requests.get(
        "http://supervisor/services/mqtt",
        headers={"Authorization": f"Bearer {token}"},
        timeout=10,
    )
    r.raise_for_status()
    d = r.json()["data"]
    return {
        "host": d["host"],
        "port": int(d["port"]),
        "username": d.get("username") or "",
        "password": d.get("password") or "",
    }


def _apparaat(naam):
    """Het device-blok waaronder beide entiteiten van een slot samenkomen."""
    return {
        "identifiers": [f"{MQTT_BASIS}_{naam}"],
        "name": f"Slot {naam}",
        "manufacturer": "Tuya",
        "model": "Smart lock via Tuya Lock Bridge",
    }


_BESCHIKBAARHEID = {
    "availability_topic": STATUS_TOPIC,
    "payload_available": "online",
    "payload_not_available": "offline",
}


def publiceer_discovery(client):
    """Vertelt Home Assistant welke entiteiten er zijn.

    Deze berichten gaan retained de broker in, zodat de entiteiten na een
    herstart van Home Assistant meteen terugkomen zonder dat deze add-on
    opnieuw hoeft te publiceren.
    """
    for naam in sorted(DEVICES):
        client.publish(
            f"homeassistant/button/{MQTT_BASIS}/{naam}_open/config",
            json.dumps(
                {
                    "name": "Openen",
                    "unique_id": f"{MQTT_BASIS}_{naam}_open",
                    "command_topic": f"{MQTT_BASIS}/{naam}/open/set",
                    "payload_press": "PRESS",
                    "icon": "mdi:door-open",
                    "device": _apparaat(naam),
                    **_BESCHIKBAARHEID,
                }
            ),
            retain=True,
        )
        client.publish(
            f"homeassistant/sensor/{MQTT_BASIS}/{naam}_codes/config",
            json.dumps(
                {
                    "name": "Geldige codes",
                    "unique_id": f"{MQTT_BASIS}_{naam}_codes",
                    "state_topic": f"{MQTT_BASIS}/{naam}/codes/state",
                    "json_attributes_topic": f"{MQTT_BASIS}/{naam}/codes/attributes",
                    "unit_of_measurement": "codes",
                    "state_class": "measurement",
                    "icon": "mdi:form-textbox-password",
                    "device": _apparaat(naam),
                    **_BESCHIKBAARHEID,
                }
            ),
            retain=True,
        )
    log.info("MQTT: entiteiten aangemeld voor %s", ", ".join(sorted(DEVICES)))


def publiceer_codes(client, naam):
    """Zet de codes van een slot als toestand plus attributen op de broker."""
    codes = haal_codes(resolve_device(naam))
    nu = int(time.time())

    regels, tellingen = [], {}
    for code in sorted(codes, key=lambda c: c.get("effective_time", 0), reverse=True):
        status = code_status(code, nu)
        tellingen[status] = tellingen.get(status, 0) + 1
        # De attributen worden door de recorder opgeslagen en die kapt grote
        # waarden af, dus houden we de lijst kort.
        if len(regels) < 25:
            regels.append(
                {
                    "naam": code.get("name"),
                    "van": datetime.fromtimestamp(
                        code.get("effective_time", 0)
                    ).isoformat(timespec="minutes"),
                    "tot": datetime.fromtimestamp(
                        code.get("invalid_time", 0)
                    ).isoformat(timespec="minutes"),
                    "status": status,
                    "herhaling": bool(code.get("schedule_list")),
                }
            )

    geldig = tellingen.get("actief", 0) + tellingen.get("wacht op slot", 0)
    client.publish(f"{MQTT_BASIS}/{naam}/codes/state", str(geldig), retain=True)
    client.publish(
        f"{MQTT_BASIS}/{naam}/codes/attributes",
        json.dumps(
            {
                "codes": regels,
                "gepland": tellingen.get("gepland", 0),
                "verlopen": tellingen.get("verlopen", 0),
                "wacht_op_slot": tellingen.get("wacht op slot", 0),
                "bijgewerkt": datetime.now().isoformat(timespec="seconds"),
            }
        ),
        retain=True,
    )


def _veilig_publiceer(naam):
    if _mqtt_client is None:
        return
    try:
        publiceer_codes(_mqtt_client, naam)
    except Exception:
        log.exception("MQTT: codes van %s niet kunnen publiceren", naam)


def meld_wijziging(room):
    """Werkt de sensor bij nadat er via de brug een code is veranderd.

    In een eigen thread, zodat het HTTP-antwoord op het aanmaken of intrekken
    niet hoeft te wachten op een tweede ronde langs Tuya's cloud.
    """
    if _mqtt_client is None:
        return
    naam = (room or "").strip().lower()
    if naam in DEVICES:
        threading.Thread(target=_veilig_publiceer, args=(naam,), daemon=True).start()


def _open_via_mqtt(naam):
    try:
        resp = ontgrendel(resolve_device(naam))
        log.info("MQTT: %s geopend, antwoord success=%s", naam, resp.get("success"))
    except Exception:
        log.exception("MQTT: openen van %s mislukt", naam)


def _op_bericht(client, userdata, bericht):
    delen = bericht.topic.split("/")
    if len(delen) == 4 and delen[0] == MQTT_BASIS and delen[2:] == ["open", "set"]:
        naam = delen[1]
        if naam not in DEVICES:
            log.warning("MQTT: opdracht voor onbekend slot '%s'", naam)
            return
        # Bewust niet in deze thread afhandelen: het ontgrendelen praat met
        # Tuya's cloud en kan tientallen seconden duren. Blijven we hier hangen,
        # dan komt de MQTT-lus niet toe aan zijn keepalive en verbreekt de
        # broker de verbinding, waarna alle entiteiten op niet-beschikbaar gaan.
        threading.Thread(target=_open_via_mqtt, args=(naam,), daemon=True).start()


def _op_verbinding(client, userdata, verbindingsvlaggen, reden, eigenschappen=None):
    if reden.is_failure:
        log.error("MQTT: verbinden geweigerd (%s)", reden)
        return
    client.publish(STATUS_TOPIC, "online", retain=True)
    publiceer_discovery(client)
    client.subscribe(f"{MQTT_BASIS}/+/open/set")
    log.info("MQTT: verbonden en geabonneerd op opdrachten")


def _ververs_lus(client, interval):
    while True:
        for naam in sorted(DEVICES):
            try:
                publiceer_codes(client, naam)
            except Exception:
                log.exception("MQTT: codes van %s niet kunnen publiceren", naam)
        time.sleep(interval)


def start_mqtt():
    """Zet de MQTT-koppeling op. Mislukt dit, dan blijft de HTTP-API gewoon werken."""
    global _mqtt_client
    import paho.mqtt.client as mqtt

    instellingen = mqtt_instellingen()
    client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id=MQTT_BASIS)
    if instellingen["username"]:
        client.username_pw_set(instellingen["username"], instellingen["password"])

    # Laatste wil: valt deze add-on om of raakt de netwerkverbinding weg, dan
    # zet de broker de status zelf op offline en worden de entiteiten in Home
    # Assistant grijs. Zonder dit zou je een knop zien die niets meer doet.
    client.will_set(STATUS_TOPIC, "offline", retain=True)
    client.on_connect = _op_verbinding
    client.on_message = _op_bericht

    client.connect(instellingen["host"], instellingen["port"], keepalive=60)
    client.loop_start()
    _mqtt_client = client

    minuten = int(CFG.get("refresh_minutes") or 5)
    minuten = min(max(minuten, 1), 60)
    threading.Thread(
        target=_ververs_lus, args=(client, minuten * 60), daemon=True
    ).start()
    log.info(
        "MQTT: verbonden met %s:%s, codes worden elke %s minuten ververst",
        instellingen["host"],
        instellingen["port"],
        minuten,
    )


if __name__ == "__main__":
    # waitress in plaats van Flask's ingebouwde ontwikkelserver: die laatste
    # waarschuwt bij elke start dat hij niet voor productie bedoeld is en is
    # minder robuust bij gelijktijdige verzoeken.
    from waitress import serve

    if CFG.get("mqtt_enabled", True):
        try:
            start_mqtt()
        except Exception:
            # Geen reden om de hele brug te laten vallen: de HTTP-API en het
            # paneel werken prima zonder MQTT. Alleen de entiteiten ontbreken.
            log.exception(
                "MQTT: opzetten mislukt - de brug draait verder zonder entiteiten"
            )

    log.info("Tuya Lock Bridge start op poort 8099 (waitress)")
    serve(app, host="0.0.0.0", port=8099)
