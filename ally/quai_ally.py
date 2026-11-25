#!/usr/bin/env python3
"""Quai — moteur côté ROG Ally.

Piloté depuis l'application Quai du Mac, par SSH (`ssh ally python3 -I ~/.local/share/quai/quai_ally.py …`).
Chaque événement sort sur stdout en une ligne JSON :
    {"t": "etape",    "msg": "…"}                 étape lisible par Jean
    {"t": "progres",  "pct": 42, "msg": "…"}      avancement chiffré
    {"t": "question", "type": "…", …}             le Mac doit demander quelque chose à Jean
    {"t": "fin",      …}                          résultat
    {"t": "erreur",   "msg": "…"}                 échec, déjà formulé pour Jean

Rangement : un dossier par jeu, <racine>/Jeux/<id>/ avec
    quai.json   la fiche (nom, type, lanceur, appid Steam…)
    jeu/        les fichiers du jeu
    pfx/        le préfixe Proton (jeux Windows)
    lancer.sh   ce que Steam exécute
<racine> = ~ (interne) ou la microSD montée.

Règles :
- les jeux Windows tournent par umu-run + le GE-Proton le plus récent, chacun dans son préfixe ;
- shortcuts.vdf (binaire) ne s'écrit que Steam fermé ; en mode jeu, la session relance Steam seule ;
- désinstaller met d'abord de côté les sauvegardes (~/Sauvegardes/Quai) ;
- les formats Mac/Android/paquets système sont refusés avec la raison.
"""
import binascii, json, os, re, shutil, struct, subprocess, sys, time, urllib.parse, urllib.request
from pathlib import Path

VERSION = "3"
HOME = Path.home()
STEAM = HOME / ".local/share/Steam"
CONF = HOME / ".config/quai"
SAUV = HOME / "Sauvegardes/Quai"
LAUNCHERS = HOME / "Emulation/tools/launchers"
RA_CORES = HOME / ".var/app/org.libretro.RetroArch/config/retroarch/cores"
ROMS = HOME / "Emulation/roms"

REFUS = {
    ".dmg": "un .dmg est une application Mac : elle ne peut pas tourner sur l'Ally (ni sous Linux, ni avec Proton).",
    ".pkg": "un .pkg est un installeur Mac : il ne peut pas tourner sur l'Ally.",
    ".app": "un .app est une application Mac : elle ne peut pas tourner sur l'Ally.",
    ".ipa": "un .ipa est une application iPhone : elle ne peut pas tourner sur l'Ally.",
    ".apk": "un .apk est une application Android : Bazzite ne fait pas tourner Android.",
    ".xapk": "un .xapk est une application Android : Bazzite ne fait pas tourner Android.",
    ".rpm": "un .rpm est un paquet système : Bazzite n'en installe pas (système en lecture seule). Cherche la version Flatpak ou AppImage.",
    ".deb": "un .deb est un paquet Debian/Ubuntu : Bazzite ne l'installe pas. Cherche la version Flatpak ou AppImage.",
}
ARCHIVES = (".zip", ".7z", ".rar", ".tar", ".gz", ".tgz", ".xz", ".bz2", ".zst")
# extension -> (dossier de ROMs, cœurs RetroArch par ordre de préférence) ; "eden" = Switch
ROM_MAP = {
    ".nes": ("nes", ["nestopia", "fceumm", "mesen"]),
    ".sfc": ("snes", ["snes9x", "bsnes"]), ".smc": ("snes", ["snes9x", "bsnes"]),
    ".md": ("megadrive", ["genesis_plus_gx", "picodrive", "blastem"]),
    ".gen": ("megadrive", ["genesis_plus_gx", "picodrive"]), ".smd": ("megadrive", ["genesis_plus_gx", "picodrive"]),
    ".sms": ("mastersystem", ["genesis_plus_gx", "gearsystem"]), ".gg": ("gamegear", ["genesis_plus_gx", "gearsystem"]),
    ".gb": ("gb", ["gambatte", "sameboy", "gearboy"]), ".gbc": ("gbc", ["gambatte", "sameboy", "gearboy"]),
    ".gba": ("gba", ["mgba", "vba_next", "gpsp"]),
    ".n64": ("n64", ["mupen64plus_next", "parallel_n64"]), ".z64": ("n64", ["mupen64plus_next", "parallel_n64"]),
    ".v64": ("n64", ["mupen64plus_next", "parallel_n64"]),
    ".pce": ("pcengine", ["mednafen_pce_fast", "mednafen_pce"]),
    ".nds": ("nds", ["melonds", "desmume"]),
    ".chd": ("psx", ["swanstation", "mednafen_psx_hw", "pcsx_rearmed"]),
    ".cue": ("psx", ["swanstation", "mednafen_psx_hw", "pcsx_rearmed"]),
    ".xci": ("switch", "eden"), ".nsp": ("switch", "eden"),
}
EXE_EXCLUS = re.compile(
    r"(unins|uninstall|setup|install|redist|vc_?redist|vcredist|dxsetup|directx|dotnet|ndp\d|"
    r"crashhandler|crashreport|crashpad|unitycrash|bugreport|easyanticheat|eac_|battleye|"
    r"launcherpatch|updater|update\.exe|helper|cefprocess|qtwebengine|ue4prereq|ueprereq|"
    r"physx|oalinst|touchup|dxwebsetup|steamworks|7za?\.exe|cmd\.exe|winecfg)", re.I)
# programmes de démonstration que Proton/Wine pose dans tout préfixe neuf : jamais le jeu
WINE_DOSSIERS = ("/internet explorer/", "/windows media player/", "/windows nt/", "/common files/",
                 "/windows photo viewer/", "/windows defender/", "/windowspowershell/", "/microsoft.net/")


# ── sortie ───────────────────────────────────────────────────────────────────
def emit(t, **kw):
    kw["t"] = t
    print(json.dumps(kw, ensure_ascii=False), flush=True)

def etape(msg): emit("etape", msg=msg)

def erreur(msg):
    emit("erreur", msg=msg)
    sys.exit(1)


# ── emplacements ─────────────────────────────────────────────────────────────
def carte_sd():
    """La microSD montée (racine), ou None."""
    base = Path("/run/media") / os.environ.get("USER", "bazzite")
    try:
        for p in sorted(base.iterdir()):
            if os.path.ismount(p) and os.access(p, os.W_OK):
                return p
    except OSError:
        pass
    return None

def racine(dest):
    if dest == "sd":
        sd = carte_sd()
        if not sd:
            erreur("La microSD n'est pas montée sur l'Ally. Vérifie qu'elle est bien insérée.")
        return sd
    return HOME

def place(p):
    st = os.statvfs(p)
    return {"libre": st.f_bavail * st.f_frsize, "total": st.f_blocks * st.f_frsize}

def slug(nom):
    s = re.sub(r"[^\w\- ]+", "", nom, flags=re.UNICODE).strip().replace(" ", "-")
    return re.sub(r"-+", "-", s)[:60] or "jeu"

def taille_dossier(p):
    try:
        out = subprocess.run(["du", "-sb", str(p)], capture_output=True, text=True).stdout
        return int(out.split()[0])
    except Exception:
        return 0

def fiche(d):
    return json.loads((Path(d) / "quai.json").read_text())

def ecrire_fiche(d, data):
    (Path(d) / "quai.json").write_text(json.dumps(data, ensure_ascii=False, indent=2))

def trouver_jeu(id_):
    for r in (HOME, carte_sd()):
        if r and (r / "Jeux" / id_ / "quai.json").exists():
            return r / "Jeux" / id_
    erreur(f"Jeu « {id_} » introuvable sur l'Ally.")


# ── Proton ───────────────────────────────────────────────────────────────────
def proton():
    """Chemin du GE-Proton le plus récent installé (sinon Proton Experimental de Steam)."""
    ge = sorted((STEAM / "compatibilitytools.d").glob("GE-Proton*"),
                key=lambda p: [int(x) for x in re.findall(r"\d+", p.name)] or [0])
    if ge:
        return ge[-1]
    exp = STEAM / "steamapps/common/Proton - Experimental"
    if exp.exists():
        return exp
    erreur("Aucun Proton trouvé sur l'Ally (ni GE-Proton, ni Proton Experimental).")

def env_proton(pfx):
    e = dict(os.environ)
    e.update(WINEPREFIX=str(pfx), GAMEID="umu-none", PROTONPATH=str(proton()),
             UMU_RUNTIME_UPDATE="0", DISPLAY=e.get("DISPLAY", ":0"),
             XDG_RUNTIME_DIR=e.get("XDG_RUNTIME_DIR", f"/run/user/{os.getuid()}"),
             WINEDEBUG="-all")
    return e

def chemin_windows(p):
    return "Z:" + str(p).replace("/", "\\")

# Sous-installeurs que les repacks (FitGirl surtout) lancent après la décompression : contrôle MD5,
# DirectX, Visual C++. En mode jeu, leur fenêtre est invisible et ils attendent un clic pour toujours
# (cas vécu le 24/11/2026). Proton fournit déjà DirectX et Visual C++ : on les ferme dès qu'ils
# ne travaillent plus (processeur immobile depuis INACTIF_MAX secondes), jamais pendant qu'ils calculent.
BLOQUEURS = re.compile(r"^(quicksfv|dxwebsetup|dxwsetup|dxsetup|vc_?redist[\w.\-]*|vcredist[\w.\-]*)\.exe$")
INACTIF_MAX = 30
# « Immobile » ne veut pas dire zéro : une fenêtre qui attend un clic consomme
# encore ~0,3 % (vu le 25/11/2026 sur QuickSFV, Trails : 3 tops en 10 s), ce qui
# remettait le compteur à zéro indéfiniment. Seuil : moins de 5 % d'un cœur sur
# la fenêtre d'observation. Un contrôle MD5 qui calcule tourne près de 100 %.
SEUIL_ACTIF = 0.05 * os.sysconf("SC_CLK_TCK")   # tops processeur par seconde

def _sous_installeurs(pfx):
    """{pid: (nom, temps processeur)} des bloqueurs connus qui tournent dans CE préfixe Wine."""
    trouves, cible = {}, f"WINEPREFIX={pfx}".encode()
    for e in os.listdir("/proc"):
        if not e.isdigit():
            continue
        try:
            argv0 = Path(f"/proc/{e}/cmdline").read_bytes().split(b"\0")[0].decode(errors="replace")
            nom = re.split(r"[\\/]", argv0)[-1].lower()
            if not BLOQUEURS.match(nom):
                continue
            if cible not in Path(f"/proc/{e}/environ").read_bytes().split(b"\0"):
                continue
            champs = Path(f"/proc/{e}/stat").read_text().rsplit(")", 1)[1].split()
            trouves[int(e)] = (nom, int(champs[11]) + int(champs[12]))
        except (OSError, IndexError, ValueError):
            continue
    return trouves

def _fermer(pid):
    for sig in (15, 9):
        try:
            os.kill(pid, sig)
        except ProcessLookupError:
            return
        time.sleep(1)

def lancer_proton(pfx, args, msg, timeout=10800):
    etape(msg)
    pfx.mkdir(parents=True, exist_ok=True)
    proc = subprocess.Popen(["umu-run", *args], env=env_proton(pfx),
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    debut = time.time()
    vus = {}   # pid -> (temps processeur, depuis quand il n'a pas bougé)
    while proc.poll() is None:
        time.sleep(5)
        ecoule = int(time.time() - debut)
        emit("progres", pct=None, msg=f"{msg} ({ecoule // 60} min {ecoule % 60:02d} s)")
        maintenant = time.time()
        actifs = _sous_installeurs(pfx)
        for pid, (nom, cpu) in actifs.items():
            avant = vus.get(pid)
            if avant is None:
                etape(f"{nom} lancé par l'installeur : je le surveille "
                      f"(fermé s'il reste {INACTIF_MAX} s sans rien faire).")
            if avant is None or (cpu - avant[0]) > SEUIL_ACTIF * (maintenant - avant[1]):
                vus[pid] = (cpu, maintenant)
            elif maintenant - avant[1] >= INACTIF_MAX:
                etape(f"{nom} attend un clic invisible en mode jeu : fermé, Proton s'en passe.")
                _fermer(pid)
                vus.pop(pid, None)
        for pid in set(vus) - set(actifs):
            vus.pop(pid)
        if ecoule > timeout:
            proc.kill()
            erreur(f"L'installation a dépassé {timeout // 3600} h : arrêtée. L'installeur attendait peut-être un clic.")
    return proc.returncode


# ── reconnaissance du format ─────────────────────────────────────────────────
def contient(fichier, motifs, limite=64 << 20):
    """Cherche des signatures binaires dans les `limite` premiers octets."""
    reste = b""
    with open(fichier, "rb") as f:
        lu = 0
        while lu < limite:
            bloc = f.read(1 << 20)
            if not bloc:
                break
            lu += len(bloc)
            zone = reste + bloc
            for nom, motif in motifs.items():
                if motif in zone:
                    return nom
            reste = bloc[-64:]
    return None

def type_exe(f):
    return {"inno": "inno", "nsis": "nsis"}.get(
        contient(f, {"inno": b"Inno Setup Setup Data", "nsis": b"Nullsoft Install System"}), "exe")

def nom_propre(fichier):
    """« setup_cairn_1.0.2_(71234).exe » -> « Cairn ». Suggestion seulement : Jean corrige dans le Mac."""
    n = Path(fichier).name
    for ext in (".tar.gz", ".tar.xz", ".tar.zst"):
        if n.lower().endswith(ext):
            n = n[: -len(ext)]
            break
    else:
        n = Path(n).stem
    n = re.sub(r"[\[\(\{].*?[\]\)\}]", " ", n)                    # [FitGirl], (71234), [0100…]
    n = n.replace("_", " ")
    n = re.sub(r"\bv?\d+(\.\d+)+[a-z]?\b", " ", n, flags=re.I)        # 1.0.2, v8.7.5
    n = n.replace(".", " ")
    n = re.sub(r"\b(setup|installer|install|x64|x86|win(64|32)?|windows|linux|gog|repack|shipping|"
               r"portable|multi\d*|build\s*\d+)\b", " ", n, flags=re.I)
    n = re.sub(r"(\s-+|-+\s)+", " - ", f" {n} ")                       # tirets isolés seulement
    n = re.sub(r"\s+", " ", n).strip(" -")
    if n and n.islower():
        n = n.replace("-", " ").title()
        n = re.sub(r"\b([IVX]{1,4})\b", lambda m: m.group(1).upper(), n, flags=re.I)
    return n or Path(fichier).stem

def lister_archive(f):
    out = subprocess.run(["7z", "l", "-slt", "-ba", str(f)], capture_output=True, text=True).stdout
    return [l[7:] for l in out.splitlines() if l.startswith("Path = ")]

def analyser(f):
    f = Path(f)
    if not f.exists():
        erreur(f"Fichier introuvable sur l'Ally : {f}")
    ext = f.suffix.lower()
    base = {"nom": nom_propre(f), "taille": f.stat().st_size, "fichier": str(f)}
    if f.is_dir():
        return {**base, "type": "dossier", "nom": f.name, "taille": taille_dossier(f),
                "note": "Dossier de jeu : je le range tel quel et te fais choisir l'exécutable."}
    if ext in REFUS:
        return {**base, "type": "refus", "note": REFUS[ext]}
    if ext == ".exe":
        t = type_exe(f)
        note = {"inno": "Installeur Windows (Inno Setup, GOG…) : installation automatique, sans clic.",
                "nsis": "Installeur Windows (NSIS) : installation automatique, sans clic.",
                "exe": "Programme Windows de type inconnu : est-ce un installeur ou le jeu lui-même ?"}[t]
        return {**base, "type": t, "note": note}
    if ext == ".msi":
        return {**base, "type": "msi", "note": "Installeur Windows (MSI) : installation automatique."}
    if ext == ".iso":
        return {**base, "type": "iso", "note": "Image disque : je l'ouvre et lance l'installeur qu'elle contient."}
    if ext == ".appimage":
        return {**base, "type": "appimage", "note": "Application Linux (AppImage) : tourne en natif, sans Proton."}
    if ext in (".flatpak", ".flatpakref"):
        return {**base, "type": "flatpak", "note": "Application Flatpak : installée par Flatpak."}
    if ext == ".sh":
        head = f.open("rb").read(16384)
        if b"makeself" in head.lower() or b"mojosetup" in head.lower() or b"gog" in head.lower():
            return {**base, "type": "gog_linux", "note": "Installeur Linux (GOG) : installation native, sans Proton."}
        return {**base, "type": "refus", "note": "Script shell inconnu : je ne l'exécute pas à l'aveugle."}
    if ext in ROM_MAP:
        sysdir, cores = ROM_MAP[ext]
        if cores == "eden":
            if not (LAUNCHERS / "eden.sh").exists():
                return {**base, "type": "refus", "note": "Jeu Switch, mais l'émulateur Eden n'est plus installé."}
            return {**base, "type": "rom", "systeme": sysdir, "note": "Jeu Switch : rangé pour l'émulateur Eden."}
        core = next((c for c in cores if (RA_CORES / f"{c}_libretro.so").exists()), None)
        if not core:
            return {**base, "type": "refus", "note": f"ROM « {sysdir} », mais aucun cœur RetroArch adapté n'est installé."}
        return {**base, "type": "rom", "systeme": sysdir, "note": f"ROM {sysdir} : rangée pour RetroArch."}
    if ext in ARCHIVES or f.name.lower().endswith((".tar.gz", ".tar.xz", ".tar.zst")):
        noms = lister_archive(f)
        if not noms:
            return {**base, "type": "refus", "note": "Archive illisible, ou protégée par un mot de passe."}
        bas = [n.lower().replace("\\", "/") for n in noms]
        if any(re.search(r"(^|/)(setup|install)[^/]*\.exe$", n) and n.count("/") <= 1 for n in bas):
            return {**base, "type": "archive", "note": "Archive avec un installeur : je l'ouvre et je l'installe."}
        if not any(n.endswith(".exe") for n in bas) and any(re.search(r"\.(x86_64|sh)$", n) for n in bas):
            return {**base, "type": "archive", "note": "Jeu Linux en archive : je le décompresse, il tournera en natif, sans Proton."}
        if any(n.endswith(".exe") for n in bas):
            return {**base, "type": "archive", "note": "Jeu Windows déjà installé, en archive : je le décompresse et je le range."}
        return {**base, "type": "archive", "note": "Archive : je la décompresse et cherche ce qu'il faut lancer."}
    return {**base, "type": "refus", "note": f"Format « {ext or 'sans extension'} » inconnu : je ne sais pas quoi en faire."}


# ── installation ─────────────────────────────────────────────────────────────
def extraire(src, dst):
    dst.mkdir(parents=True, exist_ok=True)
    etape("Décompression…")
    if re.search(r"\.(tar(\.(gz|xz|bz2|zst))?|tgz)$", src.name, re.I):
        p = subprocess.run(["tar", "-xf", str(src), "-C", str(dst)], capture_output=True, text=True)
        if p.returncode != 0:
            erreur("La décompression a échoué (archive abîmée ou incomplète).")
        return
    proc = subprocess.Popen(["7z", "x", "-y", "-bsp1", "-bso0", f"-o{dst}", str(src)],
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    dernier = -1
    buf = ""
    while True:
        c = proc.stdout.read(1)
        if not c:
            break
        buf += c
        if c in "\r\n\b":
            m = re.search(r"(\d+)%", buf)
            if m and int(m.group(1)) != dernier:
                dernier = int(m.group(1))
                emit("progres", pct=dernier, msg=f"Décompression… {dernier} %")
            buf = ""
    if proc.wait() != 0:
        erreur("La décompression a échoué (archive abîmée, incomplète ou protégée par un mot de passe).")

def installeur_dans(d):
    """Installeur Windows au premier ou deuxième niveau d'un dossier décompressé."""
    for prof in (1, 2):
        for p in sorted(d.glob("/".join(["*"] * prof))):
            if p.is_file() and re.match(r"(setup|install)[^/]*\.exe$", p.name, re.I):
                return p
    return None

LINUX_EXCLUS = re.compile(r"(crash|handler|unins|update|helper|launcher_|chrome-sandbox|xdg-|"
                          r"\.so(\.\d+)*$|\.py$|\.dll$)", re.I)

def est_linux(p):
    """Exécutable Linux : binaire ELF, ou script de lancement (#!)."""
    if p.suffix.lower() in (".exe", ".dll", ".txt", ".json", ".png", ".pak", ".dat", ".ini", ".cfg", ".so"):
        return False
    try:
        with open(p, "rb") as f:
            tete = f.read(4)
    except OSError:
        return False
    return tete == b"\x7fELF" or (tete[:2] == b"#!" and p.suffix.lower() in ("", ".sh", ".x86_64", ".run"))

def candidats_exe(*dossiers):
    vus, cands = set(), []
    for d in dossiers:
        if not d or not d.exists() or "drive_c" in str(d):
            continue
        for p in d.rglob("*"):
            if not p.is_file() or p in vus or LINUX_EXCLUS.search(p.name) or not est_linux(p):
                continue
            vus.add(p)
            t = p.stat().st_size
            prof = len(p.relative_to(d).parts)
            score = t / 1e6 - prof * 3 + (30 if re.search(r"\.(x86_64|x64)$", p.name, re.I) else 0) \
                + (20 if re.match(r"(start|run|launch|play)[^/]*\.sh$", p.name, re.I) else 0)
            cands.append({"chemin": str(p), "nom": p.name, "taille": t, "score": round(score, 1), "linux": True})
    for d in dossiers:
        if not d or not d.exists():
            continue
        for p in d.rglob("*.exe"):
            low = str(p).lower()
            if ("/pfx/drive_c/windows/" in low or any(w in low for w in WINE_DOSSIERS)
                    or EXE_EXCLUS.search(p.name)):
                continue
            if p in vus or not p.is_file():
                continue
            vus.add(p)
            try:
                t = p.stat().st_size
            except OSError:
                continue
            prof = len(p.relative_to(d).parts)
            score = t / 1e6 - prof * 3 + (25 if re.search(r"(shipping|win64|game)", p.name, re.I) else 0)
            cands.append({"chemin": str(p), "nom": p.name, "taille": t, "score": round(score, 1),
                          "_racine": str(d), "_prof": prof})
    # Jeux Unreal : le vrai binaire (…-Win64-Shipping.exe, dans <Projet>/Binaries/Win64/) est lourd, mais c'est
    # le petit lanceur posé à la racine qui doit démarrer le jeu (il passe les bons arguments).
    # S'il existe, il passe devant, nettement, pour que Quai le choisisse sans poser la question.
    for ship in [c for c in cands if re.search(r"-win64-shipping\.exe$", c["nom"], re.I)]:
        for c in cands:
            if c is not ship and c.get("_racine") == ship["_racine"] and c.get("_prof") == 1 \
                    and c["nom"].lower().endswith(".exe"):
                c["score"] = max(c["score"], ship["score"] + 50)
    for c in cands:
        c.pop("_racine", None); c.pop("_prof", None)
    return sorted(cands, key=lambda c: -c["score"])

# Repacks « HV » (hyperviseur) : la protection n'est pas retirée mais contournée par un pilote noyau
# Windows (VBS.cmd, Test Signing). Impossible sous Proton : le jeu ne démarrera jamais (vu le 24/11/2026,
# FitGirl HV Repack). On les reconnaît au nom du repack et à leurs fichiers, jamais au nom du jeu.
HV_NOM = re.compile(r"(^|[\s\[\(_.\-])(hv|hypervisor)([\s\]\)_.\-]|$)", re.I)
HV_FICHIERS = {"vbs.cmd", "hvg.bat", "hv source.7z"}
HV_REFUS = ("Repack « HV » (hyperviseur) : sa protection passe par un pilote Windows qui exige de "
            "désactiver les sécurités du système. Impossible sous Linux et Proton, le jeu ne démarrerait pas.")

def est_hv(nom, dossier=None, profondeur=2):
    if nom and HV_NOM.search(nom):
        return True
    if dossier and Path(dossier).is_dir():
        base = len(Path(dossier).parts)
        for racine_, sous, fichiers in os.walk(dossier):
            if len(Path(racine_).parts) - base >= profondeur:
                sous[:] = []
            if any(x.lower() in HV_FICHIERS or "hypervisor" in x.lower() for x in fichiers):
                return True
    return False

def empecher_veille(gardien, pourquoi):
    """Pas de veille pendant un envoi ou une installation (demande de Jean, 25/11/2026).

    systemd-inhibit et non « systemctl mask » : pas de droits administrateur, et
    l'interdiction tombe d'elle-même dès que le petit script « gardien » se termine.
    Chaque gardien a une fin garantie : un plantage ne laisse jamais l'Ally
    privée de veille.
    """
    # Par SSH, polkit refuse « sleep » sans mot de passe administrateur (session
    # non active, vérifié le 25/11/2026) mais accepte « idle ». On tente le
    # blocage complet, puis on se rabat sur l'inactivité. Une règle polkit
    # (/etc/polkit-1/rules.d/50-quai.rules, posée une fois avec sudo) rendrait
    # le premier essai valable.
    for quoi in ("sleep:idle", "idle"):
        try:
            p = subprocess.Popen(["systemd-inhibit", f"--what={quoi}", "--who=Quai",
                                  f"--why={pourquoi}", "--mode=block", "sh", "-c", gardien],
                                 stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                 stderr=subprocess.DEVNULL, start_new_session=True)
        except OSError:
            return  # sans systemd-inhibit, on continue quand même
        time.sleep(0.5)
        if p.poll() is None:
            return

# Tant que CE moteur vit.
def gardien_moteur():
    return f"while kill -0 {os.getpid()} 2>/dev/null; do sleep 5; done"

# Le temps de l'envoi depuis le Mac : on attend que rsync arrive (2 min au plus),
# puis on tient tant qu'il tourne. « [r]sync » pour ne pas se reconnaître soi-même.
GARDIEN_ENVOI = ('t=0; while [ $t -lt 120 ] && ! pgrep -u "$(id -u)" -f "[r]sync --server" >/dev/null; '
                 'do sleep 2; t=$((t+2)); done; '
                 'while pgrep -u "$(id -u)" -f "[r]sync --server" >/dev/null; do sleep 5; done')

def installer(args):
    empecher_veille(gardien_moteur(), "Installation d'un jeu")
    f = Path(args["fichier"])
    info = analyser(f)
    t = info["type"]
    if t == "refus":
        erreur(info["note"])
    if est_hv(args.get("nom") or f.name, f if f.is_dir() else None):
        erreur(HV_REFUS)
    nom = args.get("nom") or info["nom"]
    dest = args.get("dest", "interne")
    r = racine(dest)
    id_ = slug(nom)
    jeux = r / "Jeux"
    d = jeux / id_
    n = 2
    while d.exists():
        d = jeux / f"{id_}-{n}"; n += 1
    id_ = d.name
    d.mkdir(parents=True)
    jeu, pfx = d / "jeu", d / "pfx"
    meta = {"id": id_, "nom": nom, "type": t, "dest": dest, "source": f.name,
            "cree": time.strftime("%Y-%m-%d %H:%M"), "etat": "installation"}
    ecrire_fiche(d, meta)
    poser_verrou(d)
    etape(f"Rangement dans {d}")
    mode = args.get("mode")  # pour un .exe inconnu : "installeur" ou "jeu"

    if t == "rom":
        sysdir = info["systeme"]
        cible = ROMS / sysdir
        cible.mkdir(parents=True, exist_ok=True)
        rom = cible / f.name
        shutil.move(str(f), rom)
        meta.update(rom=str(rom), systeme=sysdir)
        ecrire_fiche(d, meta)
        return finaliser({"id": id_, "exe": str(rom), "_d": d})

    if t == "flatpak":
        etape("Installation Flatpak…")
        avant = set(subprocess.run(["flatpak", "list", "--app", "--columns=application"],
                                   capture_output=True, text=True).stdout.split())
        opt = "--from" if f.suffix.lower() == ".flatpakref" else "--bundle"
        p = subprocess.run(["flatpak", "install", "--user", "-y", "--noninteractive", opt, str(f)],
                           capture_output=True, text=True)
        apres = set(subprocess.run(["flatpak", "list", "--app", "--columns=application"],
                                   capture_output=True, text=True).stdout.split())
        nouveaux = sorted(apres - avant)
        if p.returncode != 0 or not nouveaux:
            erreur("Flatpak a refusé l'installation : " + (p.stderr.strip().splitlines() or ["raison inconnue"])[-1])
        meta.update(flatpak=nouveaux[0]); ecrire_fiche(d, meta)
        f.unlink(missing_ok=True)
        return finaliser({"id": id_, "exe": f"flatpak:{nouveaux[0]}", "_d": d})

    if t == "appimage":
        jeu.mkdir()
        cible = jeu / f.name
        shutil.move(str(f), cible); cible.chmod(0o755)
        return finaliser({"id": id_, "exe": str(cible), "_d": d})

    if t == "gog_linux":
        etape("Installation Linux (GOG)…")
        jeu.mkdir()
        p = subprocess.run(["sh", str(f), "--", "--i-agree-to-all-licenses", "--noreadme", "--nooptions",
                            "--noprompt", "--destination", str(jeu)], capture_output=True, text=True)
        start = jeu / "start.sh"
        if p.returncode != 0 or not start.exists():
            erreur("L'installeur Linux GOG a échoué.")
        f.unlink(missing_ok=True)
        return finaliser({"id": id_, "exe": str(start), "_d": d})

    # ── tout ce qui suit est Windows ──
    installeur = None
    if t in ("archive", "iso", "dossier"):
        media = d / "media"
        if t == "dossier":
            shutil.move(str(f), media)
        else:
            extraire(f, media)
            f.unlink(missing_ok=True)
        installeur = installeur_dans(media)
        if not installeur:
            if t == "iso":
                erreur("L'image disque ne contient pas d'installeur reconnaissable (setup.exe).")
            media.rename(jeu)
            meta["type"] = "portable"; ecrire_fiche(d, meta)
            return choisir_ou_finaliser(d, meta, [jeu])
        t = type_exe(installeur)
    elif t in ("inno", "nsis", "exe", "msi"):
        installeur = d / ("installeur" + f.suffix.lower())
        shutil.move(str(f), installeur)
        # les .bin d'un installeur en plusieurs morceaux restent à côté de lui
        for voisin in f.parent.glob(f.stem + "*"):
            if voisin.suffix.lower() == ".bin":
                shutil.move(str(voisin), d / voisin.name)
        if t == "exe" and mode == "jeu":
            jeu.mkdir()
            cible = jeu / f.name
            shutil.move(str(installeur), cible)
            meta["type"] = "portable"; ecrire_fiche(d, meta)
            return finaliser({"id": id_, "exe": str(cible), "_d": d})

    meta.update(installeur=str(installeur), installeur_type=t); ecrire_fiche(d, meta)
    return lancer_installeur(d, meta, installeur, t, args.get("interactif"))

def lancer_installeur(d, meta, installeur, t, a_clics=False):
    jeu, pfx = d / "jeu", d / "pfx"
    win = chemin_windows(jeu)
    if a_clics:
        return interactif(d, meta)
    if t == "inno":
        code = lancer_proton(pfx, [str(installeur), "/VERYSILENT", "/SUPPRESSMSGBOXES", "/NORESTART",
                                   "/SP-", "/NOICONS", f"/DIR={win}"], "Installation automatique (Inno Setup)…")
    elif t == "nsis":
        code = lancer_proton(pfx, [str(installeur), "/S", f"/D={win}"], "Installation automatique (NSIS)…")
    elif t == "msi":
        code = lancer_proton(pfx, ["msiexec", "/i", str(installeur), "/qn", f"TARGETDIR={win}",
                                   f"INSTALLDIR={win}"], "Installation automatique (MSI)…")
    else:
        return interactif(d, meta)
    if code not in (0, None):
        etape(f"L'installeur s'est terminé avec le code {code} : je vérifie ce qu'il a laissé.")
    if est_hv(None, jeu):
        meta["etat"] = "refus HV"; ecrire_fiche(d, meta)
        erreur(HV_REFUS + " Clique sur « Désinstaller » pour récupérer la place.")
    return choisir_ou_finaliser(d, meta, [jeu, pfx / "drive_c"])

# ── installation en cours / relance (25/11/2026) ─────────────────────────────
# La liste affiche « inachevée » tant que quai.json n'est pas « pret » : c'est
# aussi le cas PENDANT une installation. Avant de proposer « Relancer », il
# faut donc savoir si un moteur travaille encore sur ce jeu : verrou en_cours.pid
# (PID du moteur), plus tout processus Wine resté dans le préfixe du jeu.
def poser_verrou(d):
    (d / "en_cours.pid").write_text(str(os.getpid()))

def _vivant(pid):
    try:
        return b"quai_ally.py" in Path(f"/proc/{pid}/cmdline").read_bytes()
    except OSError:
        return False

def processus_du_prefixe(pfx):
    cible, pids = f"WINEPREFIX={pfx}".encode(), []
    for e in os.listdir("/proc"):
        if e.isdigit():
            try:
                if cible in Path(f"/proc/{e}/environ").read_bytes().split(b"\0"):
                    pids.append(int(e))
            except OSError:
                pass
    return pids

def en_cours(d):
    try:
        pid = int((d / "en_cours.pid").read_text())
        if pid != os.getpid() and _vivant(pid):
            return True
    except (OSError, ValueError):
        pass
    return bool(processus_du_prefixe(d / "pfx"))

def relancable(d, m):
    """Installeur automatique encore présent. Le type vient de la fiche : relire
    l'installeur (jusqu'à 64 Mo) pour chaque jeu ralentirait toute la liste."""
    i = m.get("installeur")
    return (m.get("etat") == "installation" and bool(i) and Path(i).exists()
            and m.get("installeur_type") in ("inno", "nsis", "msi"))

def relancer(args):
    """Nettoie les restes d'une installation inachevée et relance l'installeur déjà sur l'Ally."""
    d = trouver_jeu(args["id"]); meta = fiche(d)
    if not relancable(d, meta):
        erreur("Ce jeu ne peut pas être relancé : l'installeur n'est plus là ou l'installation n'était pas automatique. "
               "Désinstalle-le et redépose le fichier.")
    if en_cours(d):
        erreur("Une installation tourne encore pour ce jeu. Attends qu'elle se termine ou qu'elle échoue.")
    poser_verrou(d)
    empecher_veille(gardien_moteur(), "Installation d'un jeu")
    etape("Nettoyage des restes de l'installation précédente…")
    for pid in processus_du_prefixe(d / "pfx"):
        _fermer(pid)
    for reste in ("jeu", "pfx"):
        shutil.rmtree(d / reste, ignore_errors=True)
    meta.pop("candidats", None); meta["etat"] = "installation"; ecrire_fiche(d, meta)
    return lancer_installeur(d, meta, Path(meta["installeur"]), meta["installeur_type"])

def interactif(d, meta):
    """Installeur à clics : tuile temporaire « Installer — Nom » dans Steam, Jean la lance sur l'Ally."""
    script = d / "installer.sh"
    script.write_text(script_proton(d / "pfx", Path(meta["installeur"])))
    script.chmod(0o755)
    nom = f"Installer — {meta['nom']}"
    appid = ajouter_raccourci(nom, script, d)
    meta.update(etat="interactif", raccourci_installeur=appid); ecrire_fiche(d, meta)
    emit("question", type="interactif", id=meta["id"], nom=meta["nom"],
         msg=(f"Cet installeur a besoin de toi. Sur l'Ally, lance la tuile « {nom} » dans ta bibliothèque Steam "
              "(collection « Hors Steam »), suis l'installation en gardant le dossier proposé, puis reviens "
              "ici et clique sur « J'ai terminé »."))

def choisir_ou_finaliser(d, meta, dossiers):
    cands = candidats_exe(*dossiers)
    if not cands:
        meta["etat"] = "echec"; ecrire_fiche(d, meta)
        erreur("Aucun exécutable de jeu trouvé après l'installation. L'installeur attendait sans doute un clic : "
               "réessaie, et choisis « c'est un installeur à clics » si on te le propose.")
    meta["candidats"] = cands[:12]; ecrire_fiche(d, meta)
    net = len(cands) == 1 or cands[0]["score"] - cands[1]["score"] > 40
    if net:
        return finaliser({"id": meta["id"], "exe": cands[0]["chemin"], "_d": d})
    emit("question", type="choisir_exe", id=meta["id"], candidats=cands[:12],
         msg="Plusieurs programmes possibles : lequel lance le jeu ?")

def script_proton(pfx, exe, args=""):
    p = proton()
    return f"""#!/bin/bash
# Écrit par Quai. Lance {exe.name} avec Proton (umu), préfixe propre au jeu.
export WINEPREFIX="{pfx}"
export GAMEID=umu-none
export PROTONPATH="{p}"
export UMU_RUNTIME_UPDATE=0
cd "{exe.parent}"
exec umu-run "{exe}" {args} "$@"
"""

def finaliser(args):
    d = args.get("_d") or trouver_jeu(args["id"])
    meta = fiche(d)
    exe = args["exe"]
    etape("Préparation du lancement…")
    if meta.get("type") == "rom":
        rom = Path(exe)
        if meta["systeme"] == "switch":
            cible, opts = LAUNCHERS / "eden.sh", f'-f -g "{rom}"'
        else:
            core = next(c for c in ROM_MAP[rom.suffix.lower()][1] if (RA_CORES / f"{c}_libretro.so").exists())
            cible, opts = LAUNCHERS / "retroarch.sh", f'-L "{RA_CORES / (core + "_libretro.so")}" "{rom}"'
        lanceur, start, options = cible, LAUNCHERS, opts
    elif exe.startswith("flatpak:"):
        lanceur, start, options = Path("/usr/bin/flatpak"), HOME, f"run {exe.split(':', 1)[1]}"
    elif exe.lower().endswith(".exe"):
        lanceur = d / "lancer.sh"
        lanceur.write_text(script_proton(d / "pfx", Path(exe)))
        lanceur.chmod(0o755)
        start, options = d, ""
    else:
        Path(exe).chmod(Path(exe).stat().st_mode | 0o755)
        lanceur, start, options = Path(exe), Path(exe).parent, ""
    # tuile d'installation temporaire éventuelle
    retirer = [meta["raccourci_installeur"]] if meta.get("raccourci_installeur") else []
    appid = calcul_appid(meta["nom"], lanceur)
    jaquettes(meta, d, appid)
    appid = ajouter_raccourci(meta["nom"], lanceur, start, options, retirer=retirer, icone=meta.get("icone", ""))
    meta.update(exe=exe, lanceur=str(lanceur), appid=appid, etat="pret")
    meta.pop("raccourci_installeur", None); meta.pop("candidats", None)
    # ménage des fichiers d'installation
    for x in ("installeur.exe", "installeur.msi", "installer.sh", "media"):
        p = d / x
        if p.is_dir():
            shutil.rmtree(p, ignore_errors=True)
        elif p.exists():
            p.unlink()
    for b in d.glob("*.bin"):
        b.unlink()
    meta.pop("installeur", None)
    meta["taille"] = taille_dossier(d) + (Path(exe).stat().st_size if meta.get("type") == "rom" else 0)
    ecrire_fiche(d, meta)
    emit("fin", id=meta["id"], nom=meta["nom"], taille=meta["taille"], dest=meta["dest"],
         msg=f"« {meta['nom']} » est installé et ajouté à Steam.")


# ── Steam : raccourcis hors Steam (shortcuts.vdf binaire) ────────────────────
def dossier_utilisateur():
    cands = [p for p in (STEAM / "userdata").iterdir() if p.name.isdigit() and p.name != "0"]
    if not cands:
        erreur("Aucun compte Steam trouvé sur l'Ally.")
    return max(cands, key=lambda p: (p / "config/localconfig.vdf").stat().st_mtime
               if (p / "config/localconfig.vdf").exists() else 0)

class VDF:
    def __init__(self, data):
        self.d, self.i = data, 0
    def cstr(self):
        j = self.d.index(b"\0", self.i); s = self.d[self.i:j]; self.i = j + 1; return s
    def parse(self):
        out = []
        while True:
            t = self.d[self.i]; self.i += 1
            if t == 8:
                return out
            k = self.cstr()
            if t == 0:
                out.append([k, 0, self.parse()])
            elif t == 1:
                out.append([k, 1, self.cstr()])
            elif t == 2:
                out.append([k, 2, self.d[self.i:self.i + 4]]); self.i += 4
            else:
                raise ValueError(f"type VDF inconnu {t}")
    @staticmethod
    def dump(lst):
        b = b""
        for k, t, v in lst:
            b += bytes([t]) + k + b"\0"
            b += VDF.dump(v) + b"\x08" if t == 0 else (v + b"\0" if t == 1 else v)
        return b

def lire_raccourcis(f):
    if not f.exists() or f.stat().st_size < 12:
        return [[b"shortcuts", 0, []]]
    return VDF(f.read_bytes()).parse()

def champ(e, cle):
    for k, t, v in e:
        if k.lower() == cle.lower():
            return v
    return None

def session_jeu():
    return "gamescope" in subprocess.run(["pgrep", "-a", "gamescope"], capture_output=True, text=True).stdout

def fermer_steam():
    if subprocess.run(["pgrep", "-x", "steam"], capture_output=True).returncode != 0:
        return False
    etape("Fermeture de Steam le temps de mettre à jour la bibliothèque…")
    subprocess.Popen(["steam", "-shutdown"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    for _ in range(60):
        time.sleep(1)
        if subprocess.run(["pgrep", "-x", "steam"], capture_output=True).returncode != 0:
            return True
    erreur("Steam ne s'est pas fermé : le jeu n'a pas pu être ajouté. Réessaie dans un instant.")

def rouvrir_steam(etait_ouvert):
    if not etait_ouvert:
        return
    if session_jeu():
        etape("Steam se relance…")   # la session de jeu le relance d'elle-même
        for _ in range(60):
            if subprocess.run(["pgrep", "-x", "steam"], capture_output=True).returncode == 0:
                return
            time.sleep(1)
        return
    # mode Bureau : relancer Steam dans la session graphique
    env = dict(os.environ)
    for proc in ("plasmashell", "kwin_wayland"):
        pid = subprocess.run(["pgrep", "-x", proc], capture_output=True, text=True).stdout.split()
        if pid:
            for ligne in Path(f"/proc/{pid[0]}/environ").read_bytes().split(b"\0"):
                k, _, v = ligne.decode(errors="ignore").partition("=")
                if k in ("DISPLAY", "WAYLAND_DISPLAY", "XDG_RUNTIME_DIR", "DBUS_SESSION_BUS_ADDRESS", "XAUTHORITY"):
                    env[k] = v
            break
    subprocess.Popen(["setsid", "-f", "steam"], env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    etape("Steam se relance…")

def calcul_appid(nom, lanceur):
    return (binascii.crc32((f'"{lanceur}"' + nom).encode()) & 0xFFFFFFFF) | 0x80000000

def ajouter_raccourci(nom, lanceur, start, options="", retirer=(), icone=""):
    u = dossier_utilisateur()
    f = u / "config/shortcuts.vdf"
    exe = f'"{lanceur}"'
    appid = calcul_appid(nom, lanceur)
    ouvert = fermer_steam()
    racine_vdf = lire_raccourcis(f)
    items = [e for _, _, e in racine_vdf[0][2]
             if struct.unpack("<I", champ(e, b"appid") or b"\0\0\0\0")[0] not in (appid, *retirer)]
    entree = [
        [b"appid", 2, struct.pack("<I", appid)],
        [b"AppName", 1, nom.encode()],
        [b"Exe", 1, exe.encode()],
        [b"StartDir", 1, f'"{start}"'.encode()],
        [b"icon", 1, icone.encode()],
        [b"ShortcutPath", 1, b""],
        [b"LaunchOptions", 1, options.encode()],
        [b"IsHidden", 2, b"\0\0\0\0"],
        [b"AllowDesktopConfig", 2, b"\1\0\0\0"],
        [b"AllowOverlay", 2, b"\1\0\0\0"],
        [b"OpenVR", 2, b"\0\0\0\0"],
        [b"Devkit", 2, b"\0\0\0\0"],
        [b"DevkitGameID", 1, b""],
        [b"DevkitOverrideAppID", 2, b"\0\0\0\0"],
        [b"LastPlayTime", 2, b"\0\0\0\0"],
        [b"FlatpakAppID", 1, b""],
        [b"tags", 0, [[b"0", 1, b"Hors Steam"]]],
    ]
    items.append(entree)
    racine_vdf[0][2] = [[str(n).encode(), 0, e] for n, e in enumerate(items)]
    f.parent.mkdir(parents=True, exist_ok=True)
    if f.exists():
        shutil.copy2(f, f.with_suffix(".vdf.quai-avant"))
    tmp = f.with_suffix(".tmp")
    tmp.write_bytes(VDF.dump(racine_vdf) + b"\x08")
    tmp.replace(f)
    rouvrir_steam(ouvert)
    return appid

def retirer_raccourci(appid):
    u = dossier_utilisateur()
    f = u / "config/shortcuts.vdf"
    if not f.exists():
        return
    ouvert = fermer_steam()
    racine_vdf = lire_raccourcis(f)
    items = [e for _, _, e in racine_vdf[0][2]
             if struct.unpack("<I", champ(e, b"appid") or b"\0\0\0\0")[0] != appid]
    racine_vdf[0][2] = [[str(n).encode(), 0, e] for n, e in enumerate(items)]
    shutil.copy2(f, f.with_suffix(".vdf.quai-avant"))
    f.write_bytes(VDF.dump(racine_vdf) + b"\x08")
    for g in (u / "config/grid").glob(f"{appid}*"):
        g.unlink()
    rouvrir_steam(ouvert)


# ── jaquettes SteamGridDB ────────────────────────────────────────────────────
def sgdb(chemin, cle):
    req = urllib.request.Request("https://www.steamgriddb.com/api/v2" + chemin,
                                 headers={"Authorization": f"Bearer {cle}", "User-Agent": "Quai"})
    with urllib.request.urlopen(req, timeout=20) as r:
        return json.loads(r.read())

def jaquettes(meta, d, appid):
    cle_f = CONF / "sgdb.key"
    if not cle_f.exists():
        etape("Pas de clé SteamGridDB : le jeu apparaîtra sans jaquette.")
        return
    cle = cle_f.read_text().strip()
    etape("Recherche des jaquettes…")
    try:
        res = sgdb("/search/autocomplete/" + urllib.parse.quote(meta["nom"]), cle).get("data") or []
        if not res:
            etape("Aucune jaquette trouvée pour ce nom.")
            return
        gid = res[0]["id"]
        grid = dossier_utilisateur() / "config/grid"
        grid.mkdir(parents=True, exist_ok=True)
        voulu = [("grids", "dimensions=600x900", f"{appid}p"),
                 ("grids", "dimensions=920x430,460x215", f"{appid}"),
                 ("heroes", "", f"{appid}_hero"),
                 ("logos", "", f"{appid}_logo"),
                 ("icons", "", "icone")]
        for kind, filtre, nomf in voulu:
            q = "&".join(x for x in (filtre, "mimes=image/png,image/jpeg") if x)
            data = sgdb(f"/{kind}/game/{gid}?{q}", cle).get("data") or []
            if not data:
                continue
            url = data[0]["url"]
            ext = ".jpg" if url.lower().endswith((".jpg", ".jpeg")) else ".png"
            cible = (d / (nomf + ext)) if kind == "icons" else (grid / (nomf + ext))
            urllib.request.urlretrieve(url, cible)
            if kind == "icons":
                meta["icone"] = str(cible)
        meta["sgdb"] = {"id": gid, "nom": res[0].get("name")}
        etape(f"Jaquettes trouvées (SteamGridDB : « {res[0].get('name')} »).")
    except Exception as e:
        etape(f"Jaquettes non récupérées ({e.__class__.__name__}) : le jeu reste sans visuel.")

# ── liste, désinstallation, état ─────────────────────────────────────────────
def liste():
    jeux = []
    for r, dest in ((HOME, "interne"), (carte_sd(), "sd")):
        if not r or not (r / "Jeux").exists():
            continue
        for q in sorted((r / "Jeux").glob("*/quai.json")):
            try:
                m = json.loads(q.read_text())
            except Exception:
                continue
            jeux.append({"id": m["id"], "nom": m["nom"], "type": m.get("type"), "dest": dest,
                         "etat": m.get("etat"), "taille": m.get("taille") or taille_dossier(q.parent),
                         "cree": m.get("cree"), "en_cours": en_cours(q.parent),
                         "relancable": relancable(q.parent, m)})
    emit("fin", jeux=jeux)

def desinstaller(args):
    d = trouver_jeu(args["id"])
    meta = fiche(d)
    if en_cours(d):
        erreur("Une installation tourne encore pour ce jeu : attends qu'elle se termine avant de le désinstaller.")
    SAUV.mkdir(parents=True, exist_ok=True)
    users = d / "pfx/drive_c/users/steamuser"
    garder = [p for p in (users / "AppData", users / "Documents", users / "Saved Games") if p.exists()]
    if garder:
        etape("Mise de côté des sauvegardes…")
        arch = SAUV / f"{meta['id']}-{time.strftime('%Y%m%d-%H%M')}.tar.gz"
        subprocess.run(["tar", "-czf", str(arch), "-C", str(users)] + [p.name for p in garder],
                       capture_output=True)
        etape(f"Sauvegardes mises de côté : {arch}")
    if meta.get("appid"):
        etape("Retrait de Steam…")
        retirer_raccourci(meta["appid"])
    if meta.get("flatpak"):
        subprocess.run(["flatpak", "uninstall", "--user", "-y", "--noninteractive", meta["flatpak"]],
                       capture_output=True)
    if meta.get("rom"):
        Path(meta["rom"]).unlink(missing_ok=True)
    etape("Suppression des fichiers…")
    taille = taille_dossier(d)
    shutil.rmtree(d, ignore_errors=True)
    emit("fin", id=meta["id"], libere=taille, msg=f"« {meta['nom']} » est désinstallé.")

def etat():
    sd = carte_sd()
    emit("fin",
         interne=place(HOME), sd=({"chemin": str(sd), **place(sd)} if sd else None),
         steam=subprocess.run(["pgrep", "-x", "steam"], capture_output=True).returncode == 0,
         session="jeu" if session_jeu() else "bureau",
         proton=proton().name, sgdb=(CONF / "sgdb.key").exists())

def depot(dest):
    p = racine(dest) / "Jeux/.depot"
    p.mkdir(parents=True, exist_ok=True)
    empecher_veille(GARDIEN_ENVOI, "Réception d'un jeu depuis le Mac")
    emit("fin", chemin=str(p))


def main():
    if len(sys.argv) < 2:
        erreur("commande manquante")
    cmd, args = sys.argv[1], json.loads(sys.argv[2]) if len(sys.argv) > 2 else {}
    try:
        if cmd == "etat": etat()
        elif cmd == "depot": depot(args.get("dest", "interne"))
        elif cmd == "analyser": emit("fin", **analyser(args["fichier"]))
        elif cmd == "installer": installer(args)
        elif cmd == "finaliser": finaliser(args)
        elif cmd == "reprendre":
            d = trouver_jeu(args["id"]); m = fiche(d)
            choisir_ou_finaliser(d, m, [d / "jeu", d / "pfx/drive_c"])
        elif cmd == "version": emit("fin", version=VERSION)
        elif cmd == "effacer_depot":
            f = Path(args["fichier"]).resolve()
            if f.parent.name != ".depot" or f.parent.parent.name != "Jeux":
                erreur("Refus : ce fichier n'est pas dans un dossier de dépôt de Quai.")
            shutil.rmtree(f, ignore_errors=True) if f.is_dir() else f.unlink(missing_ok=True)
            emit("fin", msg="Fichier envoyé effacé.")
        elif cmd == "liste": liste()
        elif cmd == "desinstaller": desinstaller(args)
        elif cmd == "relancer": relancer(args)
        elif cmd == "cle":
            CONF.mkdir(parents=True, exist_ok=True)
            (CONF / "sgdb.key").write_text(args["cle"].strip()); (CONF / "sgdb.key").chmod(0o600)
            emit("fin", msg="Clé SteamGridDB enregistrée.")
        else:
            erreur(f"commande inconnue : {cmd}")
    except SystemExit:
        raise
    except Exception as e:
        erreur(f"Erreur inattendue sur l'Ally : {e.__class__.__name__} — {e}")

if __name__ == "__main__":
    main()
