"""Brain: die Themen der Person geordnet in home/brain/, auf Wunsch jede Nacht in einen Drive-Ordner gespiegelt, den sie
selbst mit anderen teilt.

home/brain/ entsteht aus vorlage/brain/ (Regeln, Cockpit, Index, Eingang, Vorlage für den Stand eines Themas). Was
dort liegt, können andere lesen, sobald Teilen an ist. Profil, Akten, Gedächtnis, WhatsApp und Logbuch gehören nie
hinein, das regelt brain/REGELN.md für die KI.

Teilen geht nur in eine Richtung: rclone sync von home/brain/ in den Drive-Ordner, über die Google-Anmeldung des
Kits (ablage.zugang("drive"), also Kalender und Mail mit dem Schalter Mail). Was andere im Drive-Ordner ändern, ist
nach dem nächsten Lauf wieder weg. Freigeben tut die Person selbst in Drive. Der Eingang (_eingang/) bleibt auf dem
Server, er hält noch nicht Geprüftes.

sync löscht im Ziel alles, was nicht aus home/brain/ kommt. Deshalb spiegelt kein Lauf in einen fremden Ordner: Ein
Ordner gilt als unserer, wenn er fehlt, leer ist oder REGELN.md enthält. Liegt in Drive schon ein anderer „Brain“
(etwa aus dem Brain-Kit oder nach einem Wechsel des Google-Kontos), nimmt der Lauf „Brain 2“, „Brain 3“ und merkt
sich den Namen.

Zustand: STATE_DIR/brain.json. config.toml fasst dieses Modul nicht an, [brain] status gehört assistent.py.
"""
import datetime
import os
import re
import time

import ablage
import kit

VORLAGE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "vorlage", "brain")
STAND = os.path.join(kit.STATE_DIR, "brain.json")
ORDNER = "Brain"          # Drive-Ordner, bei einem fremden Ordner gleichen Namens „Brain 2“ usw.
KENNUNG = "REGELN.md"     # liegt in jedem Ordner, den dieses Modul spiegelt
NICHT_TEILEN = ("_eingang/",)   # Ungeprüftes bleibt auf dem Server
PLATZHALTER = re.compile(r"\{\{(PERSON|ASSISTENT|HEUTE)\}\}")


class BrainFehler(ablage.AblageFehler):
    pass


def lies():
    return ablage.json_lesen(STAND)


def setze(**kw):
    return ablage.json_setzen(STAND, **kw)


# --- Anlegen -----------------------------------------------------------------------------------------------------

def anlegen(home_dir, werte):
    """vorlage/brain/ nach home/brain/, vorhandene Dateien bleiben unberührt. Ersetzt {{PERSON}}, {{ASSISTENT}} und
    {{HEUTE}} aus werte (HEUTE fehlt: heute). Rückgabe: neu angelegte Pfade relativ zu home_dir."""
    w = {"PERSON": werte.get("PERSON") or "die Person", "ASSISTENT": werte.get("ASSISTENT") or "der Assistent",
         "HEUTE": werte.get("HEUTE") or datetime.date.today().isoformat()}
    neu = []
    for wurzel, _, dateien in os.walk(VORLAGE):
        ziel_ordner = os.path.normpath(os.path.join(home_dir, "brain", os.path.relpath(wurzel, VORLAGE)))
        os.makedirs(ziel_ordner, exist_ok=True)
        for d in sorted(dateien):
            ziel = os.path.join(ziel_ordner, d)
            if os.path.exists(ziel):
                continue
            with open(os.path.join(wurzel, d), encoding="utf-8") as f:
                text = PLATZHALTER.sub(lambda m: w[m.group(1)], f.read())
            tmp = ziel + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                f.write(text)
            os.chmod(tmp, 0o600)
            os.replace(tmp, ziel)
            neu.append(os.path.relpath(ziel, home_dir))
    return sorted(neu)


# --- Teilen ------------------------------------------------------------------------------------------------------

def zustand(drive_moeglich):
    """Für die Karte Brain. moeglich: Google ist mit Drive verbunden (Kalender und Mail, Schalter Mail)."""
    st = lies()
    return {"teilen": bool(st.get("teilen")), "moeglich": bool(drive_moeglich), "ordner": st.get("ordner") or ORDNER,
            "zuletzt": float(st.get("zuletzt") or 0), "fehler": st.get("fehler") or ""}


def teilen(an, drive_moeglich):
    """Teilen an oder aus. An: sofort einmal spiegeln. Klappt das nicht, bleibt Teilen aus und BrainFehler sagt warum.
    Aus: kein Lauf mehr, der Ordner in Drive bleibt, wie er ist."""
    if not an:
        setze(teilen=False, fehler=None, fehler_technik=None)
        return
    if not drive_moeglich:
        raise BrainFehler("Zum Teilen braucht dein Assistent Google Drive. Verbinde Google unter Kalender und Mail "
                          "und schalte dort die Mail ein, darin steckt Drive.")
    if not os.path.isdir(os.path.join(kit.HOME_DIR, "brain")):
        raise BrainFehler("Schalte zuerst Brain ein, dann gibt es etwas zu teilen.")
    setze(teilen=True)
    try:
        spiegeln(kit.HOME_DIR)
    except BrainFehler as e:
        setze(teilen=False, fehler=str(e))
        raise


def _fehler(e):
    """AblageFehler als Satz für die Karte Brain: Drive hängt hier an der Google-Anmeldung, nicht an der Ablage."""
    if isinstance(e, BrainFehler):
        return e
    if e.zugang_weg:
        return BrainFehler("Google lässt mich nicht mehr in dein Drive. Verbinde Google unter Kalender und Mail bitte "
                           "neu.", e.technik, True)
    if e.technik:
        return BrainFehler("Das Teilen in Google Drive hat nicht geklappt. Ich versuche es morgen wieder.", e.technik)
    return BrainFehler(str(e))


def _unser_ordner(lauf, name):
    """Darf der Lauf nach ziel:<name> spiegeln? Ja, wenn der Ordner fehlt, leer ist oder REGELN.md enthält."""
    r = lauf("lsf", f"ziel:{name}", "--max-depth", "1", timeout=300)
    if r.returncode == 3 or "directory not found" in (r.stderr or "").lower():
        return True
    if r.returncode:
        raise ablage.fehler_aus("drive", r)
    inhalt = {z.strip() for z in r.stdout.splitlines() if z.strip()}
    return not inhalt or KENNUNG in inhalt


def _ordner(lauf, st):
    """Der gemerkte Ordner, sonst „Brain“, solange dort nichts Fremdes liegt, sonst der nächste freie Name."""
    namen = [st.get("ordner") or ORDNER, ORDNER] + [f"{ORDNER} {n}" for n in range(2, 21)]
    for name in dict.fromkeys(namen):
        if _unser_ordner(lauf, name):
            return name
    raise BrainFehler("In deinem Google Drive liegen schon zu viele Ordner namens Brain. Benenne einen davon um, "
                      "dann versuche ich es wieder.")


def spiegeln(home_dir):
    """home/brain/ in den Drive-Ordner spiegeln. Ohne Teilen passiert nichts. Wirft BrainFehler mit einem Satz für
    die Person und merkt ihn sich als fehler."""
    st = lies()
    quelle = os.path.join(home_dir, "brain")
    if not st.get("teilen") or not os.path.isdir(quelle):
        return
    try:
        with ablage.verbindung("drive") as lauf:
            ordner = _ordner(lauf, st)
            ablage.sync(lauf, "drive", quelle, ordner, NICHT_TEILEN)
    except ablage.AblageFehler as e:
        f = _fehler(e)
        setze(fehler=str(f), fehler_technik=f.technik or None)
        raise f
    setze(ordner=ordner, zuletzt=time.time(), fehler=None, fehler_technik=None)
