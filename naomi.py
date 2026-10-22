"""Application web de suivi des heures et des salaires des ouvriers de chantier.

Un seul fichier, uniquement Python + Streamlit (aucune autre bibliothèque).
Lancer :  streamlit run app.py
Données : base SQLite (chantier.db) créée et remplie automatiquement au premier lancement.
Comptes de démonstration : pdg / pdg123  et  chef / chef123
"""
import hashlib
import hmac
import json
import os
import random
import sqlite3
from collections import defaultdict
from contextlib import contextmanager
from datetime import date, datetime, time, timedelta

import pandas as pd
import streamlit as st

st.set_page_config(page_title="Suivi de chantier", page_icon="🏗️", layout="centered")


# =============================================================================
# 1. Calculs : heures, paie, mots de passe (testables)
# =============================================================================
def vers_time(valeur):
    """Convertit 'HH:MM', 'HH:MM:SS', time ou datetime en objet time."""
    if isinstance(valeur, time):
        return valeur
    if isinstance(valeur, datetime):
        return valeur.time()
    texte = str(valeur).strip()
    for fmt in ("%H:%M:%S", "%H:%M"):
        try:
            return datetime.strptime(texte, fmt).time()
        except ValueError:
            continue
    raise ValueError(f"Heure invalide : {valeur!r}")


def heures_jour(arrivee, depart, pause_min=0):
    """Heures travaillées dans la journée (en heures décimales)."""
    a, d = vers_time(arrivee), vers_time(depart)
    if d <= a:
        raise ValueError("L'heure de départ doit être postérieure à l'heure d'arrivée.")
    if pause_min is None or pause_min < 0:
        raise ValueError("La pause ne peut pas être négative.")
    minutes = (d.hour * 60 + d.minute) - (a.hour * 60 + a.minute) - pause_min
    if minutes < 0:
        raise ValueError("La pause est plus longue que la présence.")
    return round(minutes / 60, 2)


def lundi_de(jour: date) -> date:
    """Lundi de la semaine contenant `jour`."""
    return jour - timedelta(days=jour.weekday())


def calcul_paie(heures_des_jours, taux_horaire, seuil, majoration):
    """Paie hebdomadaire d'un ouvrier à partir de ses heures journalières."""
    if taux_horaire < 0 or seuil < 0 or majoration < 1:
        raise ValueError("Paramètres de paie invalides.")
    total = round(sum(heures_des_jours), 2)
    h_normales = min(total, seuil)
    h_sup = max(total - seuil, 0.0)
    salaire = h_normales * taux_horaire + h_sup * taux_horaire * majoration
    return {
        "total_heures": total,
        "h_normales": round(h_normales, 2),
        "h_sup": round(h_sup, 2),
        "salaire": round(salaire),
    }


# --- Mots de passe (PBKDF2, bibliothèque standard) ---------------------------
def hash_mdp(mdp: str, sel: bytes = None) -> str:
    sel = sel or os.urandom(16)
    h = hashlib.pbkdf2_hmac("sha256", mdp.encode(), sel, 120_000)
    return sel.hex() + "$" + h.hex()


def verifier_mdp(mdp: str, stocke: str) -> bool:
    try:
        sel_hex, h_hex = stocke.split("$")
        h = hashlib.pbkdf2_hmac("sha256", mdp.encode(), bytes.fromhex(sel_hex), 120_000)
        return hmac.compare_digest(h.hex(), h_hex)
    except (ValueError, AttributeError):
        return False

# =============================================================================
# 2. Génération de PDF en Python pur (fiche de paie)
# =============================================================================
class MiniPDF:
    MM = 72 / 25.4  # points PDF par millimètre
    HAUTEUR = 841.89  # A4, en points

    def __init__(self):
        self.flux = []

    def _y(self, y_mm):
        return self.HAUTEUR - y_mm * self.MM

    def texte(self, x, y, txt, taille=11, gras=False):
        octets = (str(txt).encode("cp1252", "replace")
                  .replace(b"\\", b"\\\\").replace(b"(", b"\\(").replace(b")", b"\\)"))
        police = b"/F2" if gras else b"/F1"
        self.flux.append(b"BT %s %.1f Tf %.2f %.2f Td (" % (
            police, taille, x * self.MM, self._y(y)) + octets + b") Tj ET")

    def rect(self, x, y, largeur, hauteur):
        """Rectangle (y = bord haut)."""
        self.flux.append(b"%.2f %.2f %.2f %.2f re S" % (
            x * self.MM, self._y(y + hauteur), largeur * self.MM, hauteur * self.MM))

    def sortie(self) -> bytes:
        contenu = b"\n".join(self.flux)
        objets = [
            b"<< /Type /Catalog /Pages 2 0 R >>",
            b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
            b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 595.28 841.89] /Contents 4 0 R "
            b"/Resources << /Font << /F1 5 0 R /F2 6 0 R >> >> >>",
            b"<< /Length %d >>\nstream\n" % len(contenu) + contenu + b"\nendstream",
            b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica /Encoding /WinAnsiEncoding >>",
            b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica-Bold /Encoding /WinAnsiEncoding >>",
        ]
        doc = bytearray(b"%PDF-1.4\n")
        positions = []
        for i, obj in enumerate(objets, 1):
            positions.append(len(doc))
            doc += b"%d 0 obj\n" % i + obj + b"\nendobj\n"
        debut_xref = len(doc)
        doc += b"xref\n0 %d\n0000000000 65535 f \n" % (len(objets) + 1)
        for pos in positions:
            doc += b"%010d 00000 n \n" % pos
        doc += b"trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n" % (
            len(objets) + 1, debut_xref)
        return bytes(doc)

# =============================================================================
# 3. Base de données SQLite et journal des modifications
# =============================================================================
DB_PATH = os.environ.get("DB_PATH", "chantier.db")

SCHEMA = """
CREATE TABLE IF NOT EXISTS chantiers (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    nom TEXT NOT NULL, lieu TEXT, date_debut TEXT, date_fin TEXT,
    budget_main_oeuvre REAL NOT NULL DEFAULT 0 CHECK (budget_main_oeuvre >= 0));
CREATE TABLE IF NOT EXISTS utilisateurs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    nom TEXT NOT NULL, role TEXT NOT NULL CHECK (role IN ('pdg', 'chef')),
    identifiant TEXT NOT NULL UNIQUE, mot_de_passe TEXT NOT NULL,
    chantier_id INTEGER REFERENCES chantiers(id));
CREATE TABLE IF NOT EXISTS ouvriers (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    matricule TEXT NOT NULL UNIQUE, nom TEXT NOT NULL, poste TEXT,
    taux_horaire REAL NOT NULL CHECK (taux_horaire > 0),
    chantier_id INTEGER NOT NULL REFERENCES chantiers(id),
    actif INTEGER NOT NULL DEFAULT 1);
CREATE TABLE IF NOT EXISTS pointages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ouvrier_id INTEGER NOT NULL REFERENCES ouvriers(id),
    date TEXT NOT NULL, heure_arrivee TEXT NOT NULL, heure_depart TEXT NOT NULL,
    pause_min INTEGER NOT NULL DEFAULT 60 CHECK (pause_min >= 0),
    saisi_par TEXT,
    UNIQUE (ouvrier_id, date),
    CHECK (heure_depart > heure_arrivee));
CREATE TABLE IF NOT EXISTS parametres (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    seuil_heures_normales REAL NOT NULL DEFAULT 40,
    majoration_sup REAL NOT NULL DEFAULT 1.2,
    heures_max_jour REAL NOT NULL DEFAULT 12,
    pause_defaut_min INTEGER NOT NULL DEFAULT 60);
CREATE TABLE IF NOT EXISTS semaines_validees (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    chantier_id INTEGER NOT NULL REFERENCES chantiers(id),
    semaine TEXT NOT NULL, validee_par TEXT,
    date_validation TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE (chantier_id, semaine));
CREATE TABLE IF NOT EXISTS avancements (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    chantier_id INTEGER NOT NULL REFERENCES chantiers(id),
    date TEXT NOT NULL DEFAULT (date('now')),
    pourcentage REAL NOT NULL CHECK (pourcentage BETWEEN 0 AND 100),
    saisi_par TEXT);
CREATE TABLE IF NOT EXISTS journal (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    utilisateur TEXT, date_heure TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    table_nom TEXT, ancienne_valeur TEXT, nouvelle_valeur TEXT);
"""

TABLES = ("utilisateurs", "chantiers", "ouvriers", "pointages", "parametres",
          "semaines_validees", "avancements", "journal")


@contextmanager
def connexion():
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA foreign_keys = ON")
    try:
        yield con
        con.commit()
    except Exception:
        con.rollback()
        raise
    finally:
        con.close()


def lire(table: str) -> pd.DataFrame:
    """Lit une table entière (petites tables) sous forme de DataFrame."""
    if table not in TABLES:
        raise ValueError("Table inconnue.")
    with connexion() as con:
        return pd.read_sql_query(f"SELECT * FROM {table}", con)


def _egal(a, b) -> bool:
    try:
        return float(a) == float(b)
    except (TypeError, ValueError):
        return str(a) == str(b)


def _journaliser(con, utilisateur, table, ancienne, nouvelle):
    def enc(v):
        return None if v is None else json.dumps(v, default=str, ensure_ascii=False)

    con.execute("INSERT INTO journal (utilisateur, table_nom, ancienne_valeur, nouvelle_valeur) "
                "VALUES (?, ?, ?, ?)", (utilisateur, table, enc(ancienne), enc(nouvelle)))


def _insert(con, table, valeurs) -> int:
    cols = ", ".join(valeurs)
    marques = ", ".join("?" for _ in valeurs)
    cur = con.execute(f"INSERT INTO {table} ({cols}) VALUES ({marques})", list(valeurs.values()))
    return cur.lastrowid


def inserer(table: str, valeurs: dict, utilisateur: str) -> dict:
    with connexion() as con:
        new_id = _insert(con, table, valeurs)
        _journaliser(con, utilisateur, table, None, valeurs)
    return {"id": new_id, **valeurs}


def modifier(table: str, id_: int, valeurs: dict, utilisateur: str) -> None:
    with connexion() as con:
        ancien = con.execute(f"SELECT * FROM {table} WHERE id = ?", (id_,)).fetchone()
        if ancien is None:
            raise ValueError("Enregistrement introuvable.")
        ancien = dict(ancien)
        changes = {k: v for k, v in valeurs.items() if not _egal(ancien.get(k), v)}
        if not changes:
            return
        sets = ", ".join(f"{k} = ?" for k in changes)
        con.execute(f"UPDATE {table} SET {sets} WHERE id = ?", [*changes.values(), id_])
        _journaliser(con, utilisateur, table, {k: ancien.get(k) for k in changes}, changes)


def supprimer(table: str, id_: int, utilisateur: str) -> None:
    with connexion() as con:
        ancien = con.execute(f"SELECT * FROM {table} WHERE id = ?", (id_,)).fetchone()
        if ancien is None:
            return
        con.execute(f"DELETE FROM {table} WHERE id = ?", (id_,))
        _journaliser(con, utilisateur, table, dict(ancien), None)


def enregistrer_pointage(valeurs: dict, utilisateur: str) -> str:
    """Crée ou met à jour le pointage (ouvrier, date). Retourne 'créé', 'modifié' ou 'inchangé'."""
    with connexion() as con:
        avant = con.execute("SELECT * FROM pointages WHERE ouvrier_id = ? AND date = ?",
                            (valeurs["ouvrier_id"], valeurs["date"])).fetchone()
    if avant is None:
        inserer("pointages", valeurs, utilisateur)
        return "créé"
    avant = dict(avant)
    if all(_egal(avant[k], valeurs[k]) for k in ("heure_arrivee", "heure_depart", "pause_min")):
        return "inchangé"
    modifier("pointages", avant["id"], valeurs, utilisateur)
    return "modifié"


# =============================================================================
# Initialisation et données d'exemple
# =============================================================================
EQUIPE = [
    ("Mbarga Paul", "Chef d'équipe", 3000), ("Ngono Marie", "Manœuvre", 1500),
    ("Tchamba Jean", "Maçon", 2200), ("Fotso Alain", "Maçon", 2200),
    ("Essomba Luc", "Coffreur", 2400), ("Kamga Serge", "Coffreur", 2400),
    ("Biyong Henri", "Ferrailleur", 2300), ("Nkoulou Eric", "Ferrailleur", 2300),
    ("Abena Rose", "Manœuvre", 1500), ("Tagne Michel", "Électricien", 2800),
    ("Owona Pierre", "Plombier", 2700), ("Ndzana Joseph", "Grutier", 3000),
    ("Mvondo Samuel", "Manœuvre", 1500), ("Fouda Daniel", "Maçon", 2200),
    ("Atangana Boris", "Manœuvre", 1500),
]


def charger_exemple(con) -> None:
    """Chantier à Douala, 15 ouvriers, 2 semaines de pointages, 2 comptes de démonstration."""
    rnd = random.Random(42)
    # Valeurs par défaut, à confirmer avec le Code du travail camerounais
    _insert(con, "parametres", {"id": 1, "seuil_heures_normales": 40, "majoration_sup": 1.2,
                                "heures_max_jour": 12, "pause_defaut_min": 60})
    ch_id = _insert(con, "chantiers", {
        "nom": "Immeuble R+4 Bonamoussadi", "lieu": "Douala",
        "date_debut": str(date.today() - timedelta(days=60)),
        "date_fin": str(date.today() + timedelta(days=120)),
        "budget_main_oeuvre": 18_000_000})
    _insert(con, "utilisateurs", {"nom": "Directeur Général", "role": "pdg", "identifiant": "pdg",
                                  "mot_de_passe": hash_mdp("pdg123"), "chantier_id": None})
    _insert(con, "utilisateurs", {"nom": "Chef de chantier Douala", "role": "chef",
                                  "identifiant": "chef", "mot_de_passe": hash_mdp("chef123"),
                                  "chantier_id": ch_id})
    ids = [_insert(con, "ouvriers", {"matricule": f"OUV-{i + 1:03d}", "nom": n, "poste": p,
                                     "taux_horaire": t, "chantier_id": ch_id, "actif": 1})
           for i, (n, p, t) in enumerate(EQUIPE)]

    lundi = lundi_de(date.today())
    semaines = [lundi - timedelta(days=14), lundi - timedelta(days=7)]
    for s_idx, sem in enumerate(semaines):
        for o_idx, oid in enumerate(ids):
            for j in range(6):  # lundi à samedi
                # Absences répétées volontaires : ouvrier n°3, mercredi à vendredi, 2e semaine
                if s_idx == 1 and o_idx == 2 and j in (2, 3, 4):
                    continue
                if rnd.random() < 0.07:  # absence aléatoire
                    continue
                depart = rnd.choice(["16:00:00", "16:30:00", "17:00:00"])
                if s_idx == 1 and o_idx == 5 and j == 1:  # anomalie : journée trop longue
                    depart = "22:00:00"
                _insert(con, "pointages", {
                    "ouvrier_id": oid, "date": str(sem + timedelta(days=j)),
                    "heure_arrivee": "07:30:00", "heure_depart": depart,
                    "pause_min": 60, "saisi_par": "chef"})
    _insert(con, "semaines_validees", {"chantier_id": ch_id, "semaine": str(semaines[0]),
                                       "validee_par": "pdg"})
    _insert(con, "avancements", {"chantier_id": ch_id, "pourcentage": 12, "saisi_par": "chef"})


_initialise = False


def init_db() -> None:
    """Crée les tables si besoin et charge les données d'exemple si la base est vide."""
    global _initialise
    if _initialise:
        return
    with connexion() as con:
        con.executescript(SCHEMA)
        if con.execute("SELECT COUNT(*) FROM utilisateurs").fetchone()[0] == 0:
            charger_exemple(con)
    _initialise = True

# =============================================================================
# 4. Interface Streamlit
# =============================================================================
JOURS = ["Lun", "Mar", "Mer", "Jeu", "Ven", "Sam", "Dim"]
COLS_PC = ["id", "ouvrier_id", "date", "heure_arrivee", "heure_depart", "pause_min",
           "saisi_par", "matricule", "nom", "poste", "taux_horaire", "chantier_id",
           "actif", "heures"]


# =============================================================================
# Utilitaires
# =============================================================================
def fcfa(montant) -> str:
    return f"{montant:,.0f}".replace(",", " ") + " FCFA"


def utilisateur() -> dict:
    return st.session_state["user"]


def est_pdg() -> bool:
    return utilisateur()["role"] == "pdg"


def chantier_de_l_utilisateur():
    val = utilisateur().get("chantier_id")
    return None if pd.isna(val) else int(val)


def params() -> dict:
    """Paramètres modifiables (valeurs par défaut si la table est vide)."""
    d = {"seuil": 40.0, "maj": 1.2, "max_jour": 12.0, "pause": 60}
    p = lire("parametres")
    if not p.empty:
        r = p.iloc[0]
        d.update(seuil=float(r["seuil_heures_normales"]), maj=float(r["majoration_sup"]),
                 max_jour=float(r["heures_max_jour"]), pause=int(r["pause_defaut_min"]))
    return d


def choisir_chantier(cle: str) -> int:
    ch = lire("chantiers")
    if not est_pdg():  # un chef ne voit que son chantier
        ch = ch[ch["id"] == chantier_de_l_utilisateur()]
    if ch.empty:
        st.info("Aucun chantier disponible. Le PDG doit d'abord en créer un.")
        st.stop()
    noms = dict(zip(ch["id"].astype(int), ch["nom"]))
    return int(st.selectbox("Chantier", list(noms), format_func=noms.get, key=cle))


def choisir_semaine(cle: str) -> date:
    d = st.date_input("Semaine contenant le", date.today(), key=cle, format="DD/MM/YYYY")
    lundi = lundi_de(d)
    st.caption(f"Semaine du {lundi:%d/%m/%Y} au {lundi + timedelta(days=6):%d/%m/%Y}")
    return lundi


def semaine_validee(ch_id: int, lundi: date) -> bool:
    sv = lire("semaines_validees")
    if sv.empty:
        return False
    return bool(((sv["chantier_id"] == ch_id) & (sv["semaine"].astype(str) == str(lundi))).any())


def pointages_calcules(ch_id=None, debut=None, fin=None) -> pd.DataFrame:
    """Pointages + infos ouvrier + heures calculées (0 si données incorrectes)."""
    p = lire("pointages")
    o = lire("ouvriers").rename(columns={"id": "ouvrier_id"})
    if p.empty or o.empty:
        return pd.DataFrame(columns=COLS_PC)
    df = p.merge(o, on="ouvrier_id", how="inner")
    df["date"] = pd.to_datetime(df["date"]).dt.date
    if ch_id is not None:
        df = df[df["chantier_id"] == ch_id]
    if debut is not None:
        df = df[df["date"] >= debut]
    if fin is not None:
        df = df[df["date"] <= fin]
    df = df.copy()

    def h(r):
        try:
            pause = 0 if pd.isna(r.pause_min) else r.pause_min
            return heures_jour(r.heure_arrivee, r.heure_depart, pause)
        except (ValueError, TypeError):
            return 0.0

    df["heures"] = [h(r) for r in df.itertuples()]
    return df


def paie_df(df: pd.DataFrame, prm: dict) -> pd.DataFrame:
    """Une ligne par (semaine, ouvrier) avec heures normales, sup et salaire."""
    cols = ["semaine", "ouvrier_id", "matricule", "nom", "taux_horaire",
            "total_heures", "h_normales", "h_sup", "salaire"]
    if df.empty:
        return pd.DataFrame(columns=cols)
    df = df.assign(semaine=df["date"].map(lundi_de))
    lignes = []
    for (sem, oid), g in df.groupby(["semaine", "ouvrier_id"]):
        taux = float(g["taux_horaire"].iloc[0])
        r = calcul_paie(g["heures"].tolist(), taux, prm["seuil"], prm["maj"])
        lignes.append({"semaine": sem, "ouvrier_id": int(oid), "matricule": g["matricule"].iloc[0],
                       "nom": g["nom"].iloc[0], "taux_horaire": taux, **r})
    return pd.DataFrame(lignes, columns=cols)


def fiche_pdf(ligne, jours_df: pd.DataFrame, lundi: date, chantier: str) -> bytes:
    """Fiche de paie hebdomadaire d'un ouvrier (PDF généré en Python pur)."""
    pdf = MiniPDF()
    pdf.texte(15, 20, "Fiche de paie hebdomadaire", 16, gras=True)
    pdf.texte(15, 30, f"Chantier : {chantier}")
    pdf.texte(15, 37, f"Ouvrier : {ligne['nom']} ({ligne['matricule']})")
    pdf.texte(15, 44, f"Semaine du {lundi:%d/%m/%Y} au {lundi + timedelta(days=6):%d/%m/%Y}")
    colonnes = (("Date", 40), ("Arrivée", 30), ("Départ", 30), ("Pause (min)", 30), ("Heures", 30))
    y = 52
    x = 15
    for titre, larg in colonnes:  # en-tête du tableau
        pdf.rect(x, y, larg, 8)
        pdf.texte(x + 2, y + 5.5, titre, 10, gras=True)
        x += larg
    for r in jours_df.sort_values("date").itertuples():
        y += 8
        valeurs = (f"{JOURS[r.date.weekday()]} {r.date:%d/%m/%Y}", str(r.heure_arrivee)[:5],
                   str(r.heure_depart)[:5], str(int(r.pause_min)), f"{r.heures:.2f}")
        x = 15
        for (_, larg), v in zip(colonnes, valeurs):
            pdf.rect(x, y, larg, 8)
            pdf.texte(x + 2, y + 5.5, v, 10)
            x += larg
    y += 18
    pdf.texte(15, y, f"Taux horaire : {fcfa(ligne['taux_horaire'])}", 11, gras=True)
    pdf.texte(15, y + 7, f"Heures normales : {ligne['h_normales']:.2f} h", 11, gras=True)
    pdf.texte(15, y + 14, f"Heures supplémentaires : {ligne['h_sup']:.2f} h", 11, gras=True)
    pdf.texte(15, y + 26, f"SALAIRE DE LA SEMAINE : {fcfa(ligne['salaire'])}", 13, gras=True)
    return pdf.sortie()


# =============================================================================
# Connexion
# =============================================================================
def page_connexion():
    st.title("🏗️ Suivi des heures et salaires")
    st.caption("Application de gestion des ouvriers de chantier")
    with st.form("connexion"):
        ident = st.text_input("Identifiant")
        mdp = st.text_input("Mot de passe", type="password")
        ok = st.form_submit_button("Se connecter", type="primary")
    if ok:
        if not ident.strip() or not mdp:
            st.error("Veuillez saisir l'identifiant et le mot de passe.")
            return
        users = lire("utilisateurs")
        ligne = users[users["identifiant"] == ident.strip()]
        if ligne.empty or not verifier_mdp(mdp, ligne.iloc[0]["mot_de_passe"]):
            st.error("Identifiant ou mot de passe incorrect.")
            return
        st.session_state["user"] = ligne.iloc[0].to_dict()
        st.rerun()
    st.info("Comptes de démonstration : **pdg / pdg123** (administrateur) "
            "et **chef / chef123** (chef de chantier).")


# =============================================================================
# Tableau de bord (PDG)
# =============================================================================
def page_dashboard():
    st.header("Tableau de bord")
    prm = params()
    ch_id = choisir_chantier("db_ch")
    lundi = choisir_semaine("db_sem")
    jours = [lundi + timedelta(days=i) for i in range(7)]

    ouv = lire("ouvriers")
    ouv = ouv[(ouv["chantier_id"] == ch_id) & (ouv["actif"] == True)]  # noqa: E712
    df = pointages_calcules(ch_id, lundi, lundi + timedelta(days=6))
    paie = paie_df(df, prm)

    c1, c2, c3 = st.columns(3)
    c1.metric("Heures", f"{df['heures'].sum():.1f} h")
    c2.metric("Coût salarial", fcfa(paie["salaire"].sum()))
    c3.metric("Ouvriers présents", df["ouvrier_id"].nunique())
    if semaine_validee(ch_id, lundi):
        st.success("✅ Semaine validée")

    st.subheader("Qui a travaillé quel jour ?")
    if ouv.empty:
        st.info("Aucun ouvrier actif sur ce chantier.")
    else:
        lignes = {}
        for o in ouv.itertuples():
            ligne = {}
            for j in jours:
                h = df[(df["ouvrier_id"] == o.id) & (df["date"] == j)]["heures"]
                etiquette = f"{JOURS[j.weekday()]} {j:%d/%m}"
                if len(h):
                    ligne[etiquette] = f"{h.iloc[0]:.1f} h"
                else:
                    ligne[etiquette] = "—" if j.weekday() == 6 else "Absent"
            lignes[o.nom] = ligne
        st.dataframe(pd.DataFrame(lignes).T, use_container_width=True)

    st.subheader("Coût salarial par semaine")
    toutes = paie_df(pointages_calcules(ch_id), prm)
    if toutes.empty:
        st.info("Pas encore de données.")
    else:
        st.bar_chart(toutes.groupby("semaine")["salaire"].sum())
        st.subheader("Heures par semaine")
        st.line_chart(toutes.groupby("semaine")["total_heures"].sum())


# =============================================================================
# Pointage (chef de chantier)
# =============================================================================
def page_pointage():
    st.header("Saisie des pointages")
    prm = params()
    ch_id = choisir_chantier("pt_ch")
    jour = st.date_input("Date", date.today(), max_value=date.today(), format="DD/MM/YYYY")
    lundi = lundi_de(jour)

    validee = semaine_validee(ch_id, lundi)
    if validee and not est_pdg():
        st.warning("🔒 Cette semaine est validée par le PDG : modification impossible.")
        return
    if validee:
        st.warning("Semaine validée : vos modifications seront tracées dans le journal.")

    ouv = lire("ouvriers")
    ouv = ouv[(ouv["chantier_id"] == ch_id) & (ouv["actif"] == True)]  # noqa: E712
    if ouv.empty:
        st.info("Aucun ouvrier actif sur ce chantier.")
        return

    p = lire("pointages")
    existants = {}
    if not p.empty:
        p = p.assign(date=pd.to_datetime(p["date"]).dt.date)
        existants = {int(r.ouvrier_id): r for r in p[p["date"] == jour].itertuples()}

    lignes = []
    for o in ouv.itertuples():
        e = existants.get(int(o.id))
        lignes.append({
            "id": int(o.id),
            "Ouvrier": f"{o.matricule} - {o.nom}",
            "Présent": e is not None,
            "Arrivée": vers_time(e.heure_arrivee) if e else time(7, 30),
            "Départ": vers_time(e.heure_depart) if e else time(16, 30),
            "Pause (min)": int(e.pause_min) if e else prm["pause"],
        })
    st.caption("Cochez « Présent » puis ajustez les heures si besoin.")
    edite = st.data_editor(
        pd.DataFrame(lignes), hide_index=True, use_container_width=True, key=f"ed_{ch_id}_{jour}",
        column_config={
            "id": None,
            "Ouvrier": st.column_config.TextColumn(disabled=True),
            "Présent": st.column_config.CheckboxColumn(),
            "Arrivée": st.column_config.TimeColumn(format="HH:mm", step=60),
            "Départ": st.column_config.TimeColumn(format="HH:mm", step=60),
            "Pause (min)": st.column_config.NumberColumn(min_value=0, max_value=240, step=5),
        })

    if not st.button("💾 Enregistrer les pointages", type="primary"):
        return

    erreurs, a_enregistrer, a_supprimer, longues = [], [], [], []
    for _, r in edite.iterrows():
        oid = int(r["id"])
        if r["Présent"]:
            try:
                if pd.isna(r["Arrivée"]) or pd.isna(r["Départ"]):
                    raise ValueError("heure manquante.")
                pause = 0 if pd.isna(r["Pause (min)"]) else int(r["Pause (min)"])
                h = heures_jour(r["Arrivée"], r["Départ"], pause)
                if h > prm["max_jour"]:
                    longues.append(f"{r['Ouvrier']} ({h:.1f} h)")
                a_enregistrer.append({
                    "ouvrier_id": oid, "date": str(jour),
                    "heure_arrivee": vers_time(r["Arrivée"]).strftime("%H:%M:%S"),
                    "heure_depart": vers_time(r["Départ"]).strftime("%H:%M:%S"),
                    "pause_min": pause, "saisi_par": utilisateur()["identifiant"]})
            except ValueError as e:
                erreurs.append(f"{r['Ouvrier']} : {e}")
        elif oid in existants:
            a_supprimer.append(int(existants[oid].id))

    if erreurs:
        for e in erreurs:
            st.error(e)
        st.stop()

    cree = modifie = 0
    for v in a_enregistrer:
        etat = enregistrer_pointage(v, utilisateur()["identifiant"])
        cree += etat == "créé"
        modifie += etat == "modifié"
    for pid in a_supprimer:
        supprimer("pointages", pid, utilisateur()["identifiant"])
    st.success(f"Enregistré : {cree} créé(s), {modifie} modifié(s), {len(a_supprimer)} supprimé(s).")
    if longues:
        st.warning("Journée très longue (à vérifier) : " + ", ".join(longues))


# =============================================================================
# Paie de la semaine, fiches PDF, exports, validation
# =============================================================================
def page_paie():
    st.header("Paie de la semaine")
    prm = params()
    ch_id = choisir_chantier("pa_ch")
    lundi = choisir_semaine("pa_sem")
    df = pointages_calcules(ch_id, lundi, lundi + timedelta(days=6))
    paie = paie_df(df, prm)
    if paie.empty:
        st.info("Aucun pointage pour cette semaine.")
        return

    aff = paie[["matricule", "nom", "taux_horaire", "total_heures", "h_normales", "h_sup", "salaire"]]
    aff = aff.rename(columns={"matricule": "Matricule", "nom": "Nom", "taux_horaire": "Taux (FCFA/h)",
                              "total_heures": "Total h", "h_normales": "H. normales",
                              "h_sup": "H. sup", "salaire": "Salaire (FCFA)"})
    st.dataframe(aff, hide_index=True, use_container_width=True)
    st.metric("Coût salarial de la semaine", fcfa(paie["salaire"].sum()))
    st.caption(f"Seuil : {prm['seuil']:g} h/semaine · majoration heures sup : ×{prm['maj']:g}")

    st.download_button("⬇️ Export CSV (s'ouvre dans Excel)", aff.to_csv(index=False).encode("utf-8-sig"),
                       file_name=f"paie_{lundi}.csv", mime="text/csv")

    st.subheader("Fiche de paie individuelle (PDF)")
    noms = dict(zip(paie["ouvrier_id"], paie["nom"]))
    oid = st.selectbox("Ouvrier", list(noms), format_func=noms.get)
    ligne = paie[paie["ouvrier_id"] == oid].iloc[0]
    nom_chantier = lire("chantiers").set_index("id").loc[ch_id, "nom"]
    pdf = fiche_pdf(ligne, df[df["ouvrier_id"] == oid], lundi, nom_chantier)
    st.download_button("📄 Télécharger la fiche de paie", pdf,
                       file_name=f"fiche_{ligne['matricule']}_{lundi}.pdf", mime="application/pdf")

    if est_pdg():
        st.subheader("Validation")
        if semaine_validee(ch_id, lundi):
            st.success("✅ Semaine déjà validée.")
        elif st.button("Valider la semaine (verrouille les pointages)"):
            inserer("semaines_validees", {"chantier_id": ch_id, "semaine": str(lundi),
                                             "validee_par": utilisateur()["identifiant"]},
                       utilisateur()["identifiant"])
            st.rerun()


# =============================================================================
# Alertes
# =============================================================================
def calcul_alertes(ch_id: int, debut: date, fin: date, prm: dict) -> pd.DataFrame:
    df = pointages_calcules(ch_id, debut, fin)
    ouv = lire("ouvriers")
    ouv = ouv[(ouv["chantier_id"] == ch_id) & (ouv["actif"] == True)]  # noqa: E712
    alertes = []
    if df.empty:
        return pd.DataFrame(columns=["Type", "Ouvrier", "Date", "Détail"])

    for r in df[df["heures"] > prm["max_jour"]].itertuples():
        alertes.append(("Journée trop longue", r.nom, r.date, f"{r.heures:.1f} h (max {prm['max_jour']:g} h)"))

    # Pointage manquant : jour (hors dimanche) où le chantier a travaillé mais pas cet ouvrier
    manques = defaultdict(list)
    for j in sorted(d for d in df["date"].unique() if d.weekday() != 6):
        presents = set(df[df["date"] == j]["ouvrier_id"])
        for o in ouv.itertuples():
            if o.id not in presents:
                manques[(o.nom, lundi_de(j))].append(j)
                alertes.append(("Pointage manquant", o.nom, j, "Aucun pointage ce jour"))
    for (nom, sem), js in manques.items():
        if len(js) >= 3:
            alertes.append(("Absences répétées", nom, sem, f"{len(js)} jours sans pointage dans la semaine"))
    return pd.DataFrame(alertes, columns=["Type", "Ouvrier", "Date", "Détail"]).sort_values(
        "Date", ascending=False)


def comparaison_avancement(ch_id: int, prm: dict):
    """Retourne (coût cumulé, budget, % budget consommé, % avancement) ou None."""
    ch = lire("chantiers")
    budget = float(ch[ch["id"] == ch_id]["budget_main_oeuvre"].iloc[0])
    av = lire("avancements")
    av = av[av["chantier_id"] == ch_id]
    if av.empty or budget <= 0:
        return None
    avancement = float(av.sort_values(["date", "id"]).iloc[-1]["pourcentage"])
    cout = float(paie_df(pointages_calcules(ch_id), prm)["salaire"].sum())
    return cout, budget, cout / budget * 100, avancement


def page_alertes():
    st.header("Alertes")
    prm = params()
    ch_id = choisir_chantier("al_ch")
    col1, col2 = st.columns(2)
    debut = col1.date_input("Du", date.today() - timedelta(days=14), format="DD/MM/YYYY")
    fin = col2.date_input("Au", date.today(), format="DD/MM/YYYY")
    if debut > fin:
        st.error("La date de début doit précéder la date de fin.")
        return
    al = calcul_alertes(ch_id, debut, fin, prm)
    if al.empty:
        st.success("Aucune anomalie détectée sur la période.")
    else:
        st.warning(f"{len(al)} alerte(s) détectée(s).")
        st.dataframe(al, hide_index=True, use_container_width=True)

    comp = comparaison_avancement(ch_id, prm)
    if comp and comp[2] > comp[3] + 10:
        st.error(f"⚠️ Dépassement : {comp[2]:.0f} % du budget de main-d'œuvre consommé "
                 f"pour seulement {comp[3]:.0f} % d'avancement.")


# =============================================================================
# Avancement (coût vs avancement)
# =============================================================================
def page_avancement():
    st.header("Avancement du chantier")
    prm = params()
    ch_id = choisir_chantier("av_ch")
    with st.form("avancement"):
        pct = st.number_input("Avancement actuel (%)", 0.0, 100.0, step=1.0)
        ok = st.form_submit_button("Enregistrer l'avancement")
    if ok:
        inserer("avancements", {"chantier_id": ch_id, "pourcentage": pct,
                                   "saisi_par": utilisateur()["identifiant"]},
                   utilisateur()["identifiant"])
        st.success("Avancement enregistré.")
    comp = comparaison_avancement(ch_id, prm)
    if not comp:
        st.info("Renseignez un budget (page Chantiers) et un avancement pour voir la comparaison.")
        return
    cout, budget, pct_budget, avancement = comp
    st.metric("Coût de main-d'œuvre cumulé", fcfa(cout), f"{pct_budget:.0f} % du budget de {fcfa(budget)}")
    st.write(f"Budget consommé : {pct_budget:.0f} %")
    st.progress(min(pct_budget / 100, 1.0))
    st.write(f"Avancement : {avancement:.0f} %")
    st.progress(min(avancement / 100, 1.0))
    if pct_budget > avancement + 10:
        st.error("⚠️ Le coût de main-d'œuvre avance plus vite que le chantier.")
    else:
        st.success("Coût cohérent avec l'avancement.")


# =============================================================================
# Ouvriers et chantiers
# =============================================================================
def page_ouvriers():
    st.header("Ouvriers")
    ch_id = choisir_chantier("ou_ch")
    ouv = lire("ouvriers")
    ouv = ouv[ouv["chantier_id"] == ch_id]
    if ouv.empty:
        st.info("Aucun ouvrier enregistré sur ce chantier.")
    else:
        st.dataframe(ouv[["matricule", "nom", "poste", "taux_horaire", "actif"]],
                     hide_index=True, use_container_width=True)
    if not est_pdg():
        return

    with st.expander("➕ Ajouter un ouvrier"):
        with st.form("ajout_ouvrier", clear_on_submit=True):
            matricule = st.text_input("Matricule")
            nom = st.text_input("Nom complet")
            poste = st.text_input("Poste")
            taux = st.number_input("Taux horaire (FCFA)", min_value=0.0, step=100.0)
            ok = st.form_submit_button("Ajouter")
        if ok:
            if not matricule.strip() or not nom.strip():
                st.error("Le matricule et le nom sont obligatoires.")
            elif taux <= 0:
                st.error("Le taux horaire doit être supérieur à 0.")
            elif matricule.strip() in set(lire("ouvriers")["matricule"]):
                st.error("Ce matricule existe déjà.")
            else:
                inserer("ouvriers", {"matricule": matricule.strip(), "nom": nom.strip(),
                                        "poste": poste.strip(), "taux_horaire": taux,
                                        "chantier_id": ch_id, "actif": True},
                           utilisateur()["identifiant"])
                st.success("Ouvrier ajouté.")
                st.rerun()

    if not ouv.empty:
        with st.expander("✏️ Modifier / désactiver un ouvrier"):
            noms = dict(zip(ouv["id"].astype(int), ouv["matricule"] + " - " + ouv["nom"]))
            oid = int(st.selectbox("Ouvrier", list(noms), format_func=noms.get))
            o = ouv[ouv["id"] == oid].iloc[0]
            with st.form(f"modif_{oid}"):
                nom = st.text_input("Nom", o["nom"])
                poste = st.text_input("Poste", o["poste"] or "")
                taux = st.number_input("Taux horaire (FCFA)", min_value=0.0, value=float(o["taux_horaire"]), step=100.0)
                actif = st.checkbox("Actif", bool(o["actif"]))
                ok = st.form_submit_button("Enregistrer")
            if ok:
                if not nom.strip() or taux <= 0:
                    st.error("Nom obligatoire et taux supérieur à 0.")
                else:
                    modifier("ouvriers", oid, {"nom": nom.strip(), "poste": poste.strip(),
                                                  "taux_horaire": taux, "actif": actif},
                                utilisateur()["identifiant"])
                    st.success("Modifications enregistrées.")
                    st.rerun()


def page_chantiers():
    st.header("Chantiers")
    ch = lire("chantiers")
    if not ch.empty:
        st.dataframe(ch.drop(columns=["id"]), hide_index=True, use_container_width=True)
    with st.expander("➕ Ajouter un chantier"):
        with st.form("ajout_chantier", clear_on_submit=True):
            nom = st.text_input("Nom")
            lieu = st.text_input("Lieu")
            d1 = st.date_input("Début", date.today(), format="DD/MM/YYYY")
            d2 = st.date_input("Fin prévue", date.today() + timedelta(days=90), format="DD/MM/YYYY")
            budget = st.number_input("Budget main-d'œuvre (FCFA)", min_value=0.0, step=100000.0)
            ok = st.form_submit_button("Ajouter")
        if ok:
            if not nom.strip():
                st.error("Le nom est obligatoire.")
            elif d2 < d1:
                st.error("La date de fin doit suivre la date de début.")
            else:
                inserer("chantiers", {"nom": nom.strip(), "lieu": lieu.strip(),
                                         "date_debut": str(d1), "date_fin": str(d2),
                                         "budget_main_oeuvre": budget},
                           utilisateur()["identifiant"])
                st.success("Chantier ajouté.")
                st.rerun()


# =============================================================================
# Paramètres et journal (PDG)
# =============================================================================
def page_parametres():
    st.header("Paramètres")
    st.caption("Valeurs à confirmer selon le Code du travail camerounais ou la politique de l'entreprise.")
    prm = params()
    with st.form("parametres"):
        seuil = st.number_input("Seuil d'heures normales par semaine", 1.0, 100.0, prm["seuil"], step=1.0)
        maj = st.number_input("Majoration des heures supplémentaires (×)", 1.0, 3.0, prm["maj"], step=0.05)
        mx = st.number_input("Heures maximum par jour (alerte)", 1.0, 24.0, prm["max_jour"], step=1.0)
        pause = st.number_input("Pause par défaut (minutes)", 0, 240, prm["pause"], step=5)
        ok = st.form_submit_button("Enregistrer", type="primary")
    if ok:
        valeurs = {"seuil_heures_normales": seuil, "majoration_sup": maj,
                   "heures_max_jour": mx, "pause_defaut_min": int(pause)}
        if lire("parametres").empty:
            inserer("parametres", {"id": 1, **valeurs}, utilisateur()["identifiant"])
        else:
            modifier("parametres", 1, valeurs, utilisateur()["identifiant"])
        st.success("Paramètres enregistrés.")


def page_journal():
    st.header("Journal des modifications")
    j = lire("journal")
    if j.empty:
        st.info("Aucune modification enregistrée.")
        return
    j = j.sort_values("date_heure", ascending=False).head(500)
    st.dataframe(j.drop(columns=["id"]), hide_index=True, use_container_width=True)


# =============================================================================
# Navigation
# =============================================================================
PAGES_PDG = {"📊 Tableau de bord": page_dashboard, "✍️ Pointage": page_pointage,
             "💰 Paie": page_paie, "🚨 Alertes": page_alertes, "📈 Avancement": page_avancement,
             "👷 Ouvriers": page_ouvriers, "🏗️ Chantiers": page_chantiers,
             "⚙️ Paramètres": page_parametres, "📜 Journal": page_journal}
PAGES_CHEF = {"✍️ Pointage": page_pointage, "🚨 Alertes": page_alertes,
              "📈 Avancement": page_avancement, "👷 Ouvriers": page_ouvriers}


def main():
    init_db()
    if "user" not in st.session_state:
        page_connexion()
        return
    u = utilisateur()
    pages = PAGES_PDG if est_pdg() else PAGES_CHEF
    with st.sidebar:
        st.write(f"👤 **{u['nom']}**")
        st.caption("Administrateur" if est_pdg() else "Chef de chantier")
        choix = st.radio("Menu", list(pages), label_visibility="collapsed")
        if st.button("Se déconnecter"):
            st.session_state.clear()
            st.rerun()
    try:
        pages[choix]()
    except Exception as e:  # aucun plantage : message clair à la place
        st.error(f"Une erreur est survenue : {e}")


main()
