"""Ablage: Gedächtnis und Chat-Protokoll (home/) zusätzlich außerhalb des eigenen Servers, alles optional.

Drei Arten von Menschen:
1. Mit Cloud-Ordner: Google Drive (über die bestehende Google-Anmeldung), Dropbox (PKCE, Code zum Einfügen) oder
   OneDrive (Gerätecode). Jede Nacht spiegelt rclone home/ in einen Ordner mit dem Namen des Assistenten.
2. Alles auf dem Handy: home/ als ZIP in den Chat, auf Knopfdruck oder am Ersten jedes Monats.
3. Nichts davon: alles bleibt auf dem Server, dort sichert ihn jede Nacht ein Git-Stand.

Zugänge holt dieses Modul selbst frisch (Refresh-Token in secrets.env) und gibt rclone nur ein kurzlebiges
Access-Token, damit rclone nie selbst erneuern muss. Ein Weg für alle drei Anbieter.
"""
import base64
import contextlib
import datetime
import glob
import hashlib
import json
import os
import secrets
import shutil
import subprocess
import tempfile
import threading
import time
import urllib.parse
import zipfile

import requests

import kit

# Öffentliche Kennungen der Apps, die die Person bei Dropbox und Microsoft freigibt. Beide ohne Secret (PKCE und
# Gerätecode), deshalb dürfen sie im Code stehen. Leer: der Anbieter wird auf der Seite nicht angeboten.
DROPBOX_APP = os.environ.get("ASSISTENT_DROPBOX_APP") or ""
ONEDRIVE_APP = os.environ.get("ASSISTENT_ONEDRIVE_APP") or ""

ZIELE = {"drive": "Google Drive", "dropbox": "Dropbox", "onedrive": "OneDrive"}
AUSSEN_VOR = ("eingang/", "ausgang/", ".git/", "whatsapp/.sweep/")   # Durchgangsordner, wie in .gitignore
ZIP_MAX = 49 * 1024 * 1024   # Bots dürfen Dateien bis 50 MB schicken
STAND = os.path.join(kit.STATE_DIR, "ablage.json")
DROPBOX_TOKEN = "https://api.dropboxapi.com/oauth2/token"
MS = "https://login.microsoftonline.com/common/oauth2/v2.0"
MS_RECHTE = "Files.ReadWrite offline_access User.Read"
GOOGLE_TOKEN = "https://oauth2.googleapis.com/token"
_lock = threading.Lock()
bei_verbunden = None   # setzt assistent.py: nach dem Verbinden einmal spiegeln und Bescheid geben


class AblageFehler(Exception):
    """Ein Satz für die Person. technik: Grund von rclone (nur fürs Protokoll), zugang_weg: der Anbieter lässt
    nicht mehr hinein (401, invalid_grant), die Verbindung muss neu."""

    def __init__(self, text, technik="", zugang_weg=False):
        super().__init__(text)
        self.technik = technik
        self.zugang_weg = zugang_weg


def json_lesen(pfad):
    try:
        with open(pfad, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def json_setzen(pfad, **kw):
    """Werte in einer Zustandsdatei setzen (None entfernt den Schlüssel), Rechte 600. Rückgabe: neuer Zustand."""
    with _lock:
        st = json_lesen(pfad)
        for k, v in kw.items():
            if v is None:
                st.pop(k, None)
            else:
                st[k] = v
        os.makedirs(os.path.dirname(pfad), exist_ok=True)
        tmp = pfad + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(st, f, ensure_ascii=False, indent=1)
        os.chmod(tmp, 0o600)
        os.replace(tmp, pfad)
        return st


def lies():
    return json_lesen(STAND)


def setze(**kw):
    return json_setzen(STAND, **kw)


def angebot(drive_moeglich):
    """Welche Ziele die Seite anbietet. drive_moeglich: Google ist mit Drive verbunden."""
    return [k for k, an in (("drive", drive_moeglich), ("dropbox", bool(DROPBOX_APP)), ("onedrive", bool(ONEDRIVE_APP)))
            if an]


def zustand(drive_moeglich):
    st = lies()
    warte = st.get("onedrive_warte") or {}
    return {"ziel": st.get("ziel") or "", "angebot": angebot(drive_moeglich), "zuletzt": st.get("zuletzt") or 0,
            "fehler": st.get("fehler") or "", "monatlich": bool(st.get("monatlich")), "rclone": bool(shutil.which("rclone")),
            "geraet": {"code": warte.get("code"), "url": warte.get("url")} if warte.get("bis", 0) > time.time() else None}


def _post(url, daten, was):
    try:
        r = requests.post(url, data=daten, timeout=30)
        d = r.json()
    except (requests.RequestException, ValueError):
        raise AblageFehler(f"{was} antwortet gerade nicht. Versuch es gleich noch einmal.")
    return r, d


# --- Verbinden ---------------------------------------------------------------------------------------------------

def verbinden(ziel, drive_moeglich):
    """Erster Schritt für ein Ziel. Rückgabe für die Seite: {url} (Dropbox), {code, url} (OneDrive) oder {} (Drive)."""
    if ziel not in angebot(drive_moeglich):
        raise AblageFehler("Diese Ablage gibt es auf deinem Server nicht.")
    if not shutil.which("rclone"):
        raise AblageFehler("Auf deinem Server fehlt noch ein Programm dafür. Es kommt mit dem nächsten Update von selbst.")
    if ziel == "drive":
        _google_zugang()   # prüft, ob Drive freigegeben ist
        _fertig("drive")
        return {}
    if ziel == "dropbox":
        pruef = secrets.token_urlsafe(64)
        setze(pkce=pruef, pkce_bis=time.time() + 900)
        challenge = base64.urlsafe_b64encode(hashlib.sha256(pruef.encode()).digest()).rstrip(b"=").decode()
        # Ohne redirect_uri zeigt Dropbox nach der Freigabe einen Code zum Kopieren
        return {"url": "https://www.dropbox.com/oauth2/authorize?" + urllib.parse.urlencode({
            "client_id": DROPBOX_APP, "response_type": "code", "code_challenge": challenge,
            "code_challenge_method": "S256", "token_access_type": "offline"})}
    r, d = _post(f"{MS}/devicecode", {"client_id": ONEDRIVE_APP, "scope": MS_RECHTE}, "Microsoft")
    if not r.ok or not d.get("device_code"):
        raise AblageFehler("Microsoft hat die Anmeldung gerade abgelehnt. Versuch es gleich noch einmal.")
    bis = time.time() + int(d.get("expires_in") or 900)
    setze(onedrive_warte={"code": d["user_code"], "url": d.get("verification_uri") or "https://microsoft.com/devicelogin",
                          "bis": bis})
    threading.Thread(target=_onedrive_warten, args=(d["device_code"], int(d.get("interval") or 5), bis),
                     daemon=True).start()
    return {"code": d["user_code"], "url": d.get("verification_uri") or "https://microsoft.com/devicelogin"}


def dropbox_code(code):
    st = lies()
    code = (code or "").strip()
    if not code or len(code) > 200:
        raise AblageFehler("Das sieht nicht nach dem Code von Dropbox aus.")
    if not st.get("pkce") or st.get("pkce_bis", 0) < time.time():
        raise AblageFehler("Das hat zu lange gedauert. Tipp noch einmal auf Dropbox.")
    r, d = _post(DROPBOX_TOKEN, {"code": code, "grant_type": "authorization_code", "code_verifier": st["pkce"],
                                 "client_id": DROPBOX_APP}, "Dropbox")
    if not r.ok or not d.get("refresh_token"):
        raise AblageFehler("Dropbox kennt diesen Code nicht. Kopier ihn bitte noch einmal ganz.")
    kit.setze_secret("ABLAGE_REFRESH", d["refresh_token"])
    setze(pkce=None, pkce_bis=None)
    _fertig("dropbox")


def _onedrive_warten(device_code, abstand, bis):
    while time.time() < bis:
        time.sleep(abstand)
        if (lies().get("onedrive_warte") or {}).get("bis") != bis:
            return   # neu gestartet oder abgebrochen
        try:
            r, d = _post(f"{MS}/token", {"grant_type": "urn:ietf:params:oauth:grant-type:device_code",
                                         "client_id": ONEDRIVE_APP, "device_code": device_code}, "Microsoft")
        except AblageFehler:
            continue
        if d.get("error") == "authorization_pending":
            continue
        if d.get("error") == "slow_down":
            abstand += 5
            continue
        if not r.ok or not d.get("refresh_token"):
            setze(onedrive_warte=None, fehler="Microsoft hat die Anmeldung nicht bestätigt. Versuch es noch einmal.")
            return
        try:
            lw = requests.get("https://graph.microsoft.com/v1.0/me/drive",
                              headers={"authorization": f"Bearer {d['access_token']}"}, timeout=30).json()
        except (requests.RequestException, ValueError):
            lw = {}
        if not lw.get("id"):
            setze(onedrive_warte=None, fehler="Zu diesem Microsoft-Konto gibt es kein OneDrive.")
            return
        kit.setze_secret("ABLAGE_REFRESH", d["refresh_token"])
        setze(onedrive_warte=None, drive_id=lw["id"], drive_type=lw.get("driveType") or "personal")
        _fertig("onedrive")
        return
    setze(onedrive_warte=None)


def _fertig(ziel):
    setze(ziel=ziel, fehler=None, zuletzt=None)
    if bei_verbunden:
        threading.Thread(target=bei_verbunden, args=(ziel,), daemon=True).start()


def trennen():
    setze(ziel=None, fehler=None, zuletzt=None, onedrive_warte=None, drive_id=None, drive_type=None)
    kit.setze_secret("ABLAGE_REFRESH", "")


# --- Zugang und Spiegel ------------------------------------------------------------------------------------------

def _google_anmeldung():
    """Anmeldung des Google-Dienstes (seine Datei, nur lesen): (daten, client_id, client_secret), ohne Datei None.
    Fehlen Client-ID oder Schlüssel in der Datei, gelten die der Google-App, mit der der Dienst läuft: auf dem eigenen
    Server die der Person, sonst die der Box (einrichten.google_client)."""
    pfade = sorted(glob.glob(os.path.join(kit.DATEN, "google", "zugang", "*.json")), key=os.path.getmtime)
    if not pfade:
        return None
    try:
        with open(pfade[-1], encoding="utf-8") as f:
            z = json.load(f)
    except (OSError, ValueError):
        z = {}
    if not isinstance(z, dict):
        z = {}
    cid, cgeheim = z.get("client_id"), z.get("client_secret")
    if not cid:
        import einrichten   # erst hier: einrichten zieht beim Anlegen brain.py und damit dieses Modul nach
        c = json_lesen(einrichten.google_client())
        c = (c.get("installed") or c.get("web") or {}) if isinstance(c, dict) else {}
        cid, cgeheim = c.get("client_id"), c.get("client_secret")
    return z, cid, cgeheim


def google_anmeldung_gilt(warten=10):
    """Gilt die Google-Anmeldung noch? Ein Refresh bei Google. True: ja, False: Google lehnt ab (invalid_grant, die
    Person muss neu verbinden), None: unklar (keine Anmeldung, Google nicht erreicht)."""
    a = _google_anmeldung()
    if not a or not a[1] or not a[0].get("refresh_token"):
        return None
    z, cid, cgeheim = a
    try:
        r = requests.post(z.get("token_uri") or GOOGLE_TOKEN, timeout=warten,
                          data={"client_id": cid, "client_secret": cgeheim or "", "refresh_token": z["refresh_token"],
                                "grant_type": "refresh_token"})
        d = r.json()
    except (requests.RequestException, ValueError):
        return None
    if r.ok and isinstance(d, dict) and d.get("access_token"):
        return True
    return False if isinstance(d, dict) and d.get("error") in ("invalid_grant", "unauthorized_client") else None


def _google_zugang():
    """Drive über die Google-Anmeldung des Google-Dienstes (Datei des Dienstes, nur lesen)."""
    a = _google_anmeldung()
    if not a:
        raise AblageFehler("Verbinde zuerst Google unter Kalender und Mail.")
    z, cid, cgeheim = a
    if not z:
        raise AblageFehler("Die Google-Anmeldung ist nicht lesbar. Verbinde Google bitte neu.")
    rechte = " ".join(z.get("scopes") or [])
    if rechte and "auth/drive" not in rechte:
        raise AblageFehler("Google Drive ist nicht freigegeben. Schalte unter Kalender und Mail die Mail ein, "
                           "darin steckt Drive, und verbinde Google neu.")
    if not (cid and z.get("refresh_token")):
        raise AblageFehler("Die Google-Anmeldung ist unvollständig. Verbinde Google bitte neu.")
    r, d = _post(z.get("token_uri") or GOOGLE_TOKEN, {"client_id": cid, "client_secret": cgeheim or "",
                                                      "refresh_token": z["refresh_token"], "grant_type": "refresh_token"},
                 "Google")
    if not r.ok or not d.get("access_token"):
        raise AblageFehler("Google lässt mich nicht mehr in dein Drive. Verbinde Google bitte neu.")
    return d["access_token"], int(d.get("expires_in") or 3600)


def zugang(ziel):
    """Frisches Access-Token: (token, sekunden)."""
    if ziel == "drive":
        return _google_zugang()
    refresh = kit.lies_secrets().get("ABLAGE_REFRESH") or ""
    if not refresh:
        raise AblageFehler(f"Die Verbindung zu {ZIELE[ziel]} fehlt. Verbinde sie bitte neu.")
    if ziel == "dropbox":
        r, d = _post(DROPBOX_TOKEN, {"grant_type": "refresh_token", "refresh_token": refresh, "client_id": DROPBOX_APP},
                     "Dropbox")
    else:
        r, d = _post(f"{MS}/token", {"grant_type": "refresh_token", "refresh_token": refresh, "client_id": ONEDRIVE_APP,
                                     "scope": MS_RECHTE}, "Microsoft")
    if not r.ok or not d.get("access_token"):
        raise AblageFehler(f"{ZIELE[ziel]} lässt mich nicht mehr hinein. Verbinde die Ablage bitte neu.")
    if d.get("refresh_token") and d["refresh_token"] != refresh:
        kit.setze_secret("ABLAGE_REFRESH", d["refresh_token"])   # Microsoft gibt jedes Mal ein neues aus
    return d["access_token"], int(d.get("expires_in") or 3600)


def ordnername(name):
    sauber = "".join(c for c in (name or "") if c.isalnum() or c in " -_.").strip(" .")
    return sauber or "Assistent"


@contextlib.contextmanager
def verbindung(ziel, st=None):
    """rclone mit frischem Zugang für einen Lauf. Die Konfiguration mit dem Access-Token liegt nur so lange in einem
    Temp-Ordner, wie der Block läuft. Liefert lauf(*argumente, timeout=3600) -> CompletedProcess, das Ziel heißt
    darin `ziel:`. st: Zustand mit drive_id und drive_type (OneDrive)."""
    st = st or {}
    rclone = shutil.which("rclone")
    if not rclone:
        raise AblageFehler("Auf deinem Server fehlt noch ein Programm dafür. Es kommt mit dem nächsten Update von selbst.")
    token, sek = zugang(ziel)
    ablauf = datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(seconds=max(sek - 120, 300))
    tok = json.dumps({"access_token": token, "token_type": "Bearer", "expiry": ablauf.isoformat()})
    zeilen = ["[ziel]", f"type = {ziel}", f"token = {tok}"]
    if ziel == "drive":
        zeilen.append("scope = drive")
    if ziel == "onedrive":
        zeilen += [f"drive_id = {st.get('drive_id') or ''}", f"drive_type = {st.get('drive_type') or 'personal'}"]
    with tempfile.TemporaryDirectory(dir=kit.STATE_DIR if os.path.isdir(kit.STATE_DIR) else None) as tmp:
        conf = os.path.join(tmp, "rclone.conf")
        with open(os.open(conf, os.O_WRONLY | os.O_CREAT, 0o600), "w", encoding="utf-8") as f:
            f.write("\n".join(zeilen) + "\n")

        def lauf(*argumente, timeout=3600):
            try:
                return subprocess.run([rclone, "--config", conf, *argumente], capture_output=True, text=True,
                                      timeout=timeout)
            except subprocess.TimeoutExpired:
                raise AblageFehler(f"Das Spiegeln zu {ZIELE[ziel]} hat zu lange gedauert. Ich versuche es morgen wieder.")
        yield lauf


def fehler_aus(ziel, r):
    """AblageFehler aus einem gescheiterten rclone-Lauf."""
    grund = (r.stderr or r.stdout).strip()[-300:]
    if "401" in grund or "invalid_grant" in grund or "unauthorized" in grund.lower():
        return AblageFehler(f"{ZIELE[ziel]} lässt mich nicht mehr hinein. Verbinde die Ablage bitte neu.", grund, True)
    return AblageFehler(f"Das Spiegeln zu {ZIELE[ziel]} hat nicht geklappt. Ich versuche es morgen wieder.", grund)


def sync(lauf, ziel, quelle, ordner, ausschluesse=()):
    """Kern des Spiegelns: quelle (Ordner auf dem Server) per rclone sync nach ziel:<ordner>, das Ziel gleicht danach
    der Quelle. ausschluesse: Ordner relativ zur Quelle, die draußen bleiben. Wirft AblageFehler."""
    befehl = ["sync", quelle, f"ziel:{ordner}", "--transfers", "4", "--log-level", "ERROR"]
    for o in ausschluesse:
        befehl += ["--exclude", "/" + o + "**"]   # mit / nur ab der Quelle, nicht in jeder Tiefe
    r = lauf(*befehl)
    if r.returncode:
        raise fehler_aus(ziel, r)


def spiegeln(home, name):
    """home/ in den Ordner des Assistenten spiegeln. Wirft AblageFehler mit einem Satz für die Person."""
    st = lies()
    ziel = st.get("ziel")
    if not ziel:
        return
    try:
        with verbindung(ziel, st) as lauf:
            sync(lauf, ziel, home, ordnername(name), AUSSEN_VOR)
    except AblageFehler as e:
        if e.technik:
            setze(fehler_technik=e.technik)
        raise
    setze(zuletzt=time.time(), fehler=None, fehler_technik=None)


# --- ZIP ---------------------------------------------------------------------------------------------------------

def zip_bauen(home, name):
    """home/ als ZIP in einer temporären Datei. Rückgabe: Pfad (der Aufrufer löscht ihn)."""
    fd, pfad = tempfile.mkstemp(suffix=".zip", prefix=f"{ordnername(name).replace(' ', '-').lower()}-")
    os.close(fd)
    wurzel = ordnername(name)
    with zipfile.ZipFile(pfad, "w", zipfile.ZIP_DEFLATED) as z:
        for ordner, unter, dateien in os.walk(home):
            rel = os.path.relpath(ordner, home)
            rel = "" if rel == "." else rel + "/"
            unter[:] = sorted(u for u in unter if not (rel + u + "/").startswith(AUSSEN_VOR))
            for d in sorted(dateien):
                quelle = os.path.join(ordner, d)
                if os.path.isfile(quelle) and not os.path.islink(quelle):
                    z.write(quelle, f"{wurzel}/{rel}{d}")
    if os.path.getsize(pfad) > ZIP_MAX:
        os.unlink(pfad)
        raise AblageFehler("Deine Daten sind größer als 50 MB, so viel nimmt Telegram nicht an. "
                           "Verbinde dafür eine Ablage wie Google Drive, Dropbox oder OneDrive.")
    return pfad
