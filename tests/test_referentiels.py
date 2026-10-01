"""Sous-étape V2.1 : référentiels (secteurs, tâches, zone, déclencheurs). Aucun réseau, 0 €."""
from __future__ import annotations

import shutil
from pathlib import Path

import pytest
import yaml

from app import referentiels as r

CONFIG = Path(r.CONFIG_DIR)


@pytest.fixture
def dossier(tmp_path) -> Path:
    """Copie jetable des 4 fichiers réels, à abîmer test par test."""
    for nom in ("secteurs_tpe.yaml", "taches.yaml", "zone.yaml", "declencheurs.yaml"):
        shutil.copy(CONFIG / nom, tmp_path / nom)
    return tmp_path


def _modifier(dossier: Path, nom: str, fonction) -> None:
    chemin = dossier / nom
    brut = yaml.safe_load(chemin.read_text(encoding="utf-8"))
    fonction(brut)
    chemin.write_text(yaml.safe_dump(brut, allow_unicode=True), encoding="utf-8")


# ----- fichiers réels --------------------------------------------------------------------

def test_les_quatre_fichiers_reels_se_chargent_et_sont_coherents():
    secteurs, taches, zone, decl = (
        r.charger_secteurs_tpe(), r.charger_taches(), r.charger_zone(), r.charger_declencheurs())
    r.verifier_coherence(secteurs, decl, taches)
    assert len(secteurs.secteurs) >= 150
    assert len(taches.taches) >= 40
    assert 20 <= len(decl.declencheurs) <= 30
    assert zone.centre.nom == "Bordeaux" and zone.rayon_km == 100


def test_secteurs_exclus_ont_motif_et_source():
    exclus = [s for s in r.charger_secteurs_tpe().secteurs if s.exclusion]
    assert exclus
    assert all(s.exclusion.source for s in exclus)
    assert {s.exclusion.motif for s in exclus} <= {"reglementaire_lourd", "materiel_industriel", "code_naf_agence"}


def test_zone_gironde_au_coeur_et_dans_nouvelle_aquitaine():
    zone = r.charger_zone()
    assert [d.code for d in zone.coeur] == ["33"]
    assert len(zone.nouvelle_aquitaine) == 12
    assert zone.departements_zone()[0] == "33"


def test_declencheurs_tous_sur_domaine_officiel_et_verifies():
    for d in r.charger_declencheurs().declencheurs:
        assert d.source_url.startswith("https://")
        assert d.verifie_le.year == 2026
    for a in r.charger_declencheurs().a_surveiller:
        assert a.source_url.startswith("https://")


def test_accesseurs_caches_renvoient_le_meme_objet():
    assert r.secteurs_tpe() is r.secteurs_tpe()
    assert r.declencheurs() is r.declencheurs()
    assert r.taches() is r.taches()
    assert r.zone() is r.zone()


# ----- validation stricte ------------------------------------------------------------------

def test_code_naf_mal_forme_refuse(dossier):
    _modifier(dossier, "secteurs_tpe.yaml", lambda b: b["secteurs"][0].update(code="4120A"))
    with pytest.raises(r.ReferentielInvalide, match="code NAF mal formé"):
        r.charger_secteurs_tpe(dossier)


def test_code_naf_en_double_refuse(dossier):
    _modifier(dossier, "secteurs_tpe.yaml", lambda b: b["secteurs"].append(dict(b["secteurs"][0])))
    with pytest.raises(r.ReferentielInvalide, match="en double"):
        r.charger_secteurs_tpe(dossier)


def test_champ_inconnu_refuse(dossier):
    _modifier(dossier, "secteurs_tpe.yaml", lambda b: b["secteurs"][0].update(truc="x"))
    with pytest.raises(r.ReferentielInvalide):
        r.charger_secteurs_tpe(dossier)


def test_exclusion_sans_source_connue_refusee(dossier):
    def abimer(b):
        for s in b["secteurs"]:
            if s.get("exclusion"):
                s["exclusion"]["source"] = "inconnue"
                return
    _modifier(dossier, "secteurs_tpe.yaml", abimer)
    with pytest.raises(r.ReferentielInvalide, match="source d'exclusion inconnue"):
        r.charger_secteurs_tpe(dossier)


def test_motif_d_exclusion_hors_liste_refuse(dossier):
    def abimer(b):
        for s in b["secteurs"]:
            if s.get("exclusion"):
                s["exclusion"]["motif"] = "parce_que"
                return
    _modifier(dossier, "secteurs_tpe.yaml", abimer)
    with pytest.raises(r.ReferentielInvalide):
        r.charger_secteurs_tpe(dossier)


def test_famille_de_secteur_inconnue_refusee(dossier):
    _modifier(dossier, "secteurs_tpe.yaml", lambda b: b["secteurs"][0].update(famille="autre"))
    with pytest.raises(r.ReferentielInvalide, match="famille inconnue"):
        r.charger_secteurs_tpe(dossier)


def test_toutes_les_erreurs_sont_listees_pas_seulement_la_premiere(dossier):
    def abimer(b):
        b["secteurs"][0]["famille"] = "x"
        b["secteurs"][1]["famille"] = "y"
    _modifier(dossier, "secteurs_tpe.yaml", abimer)
    with pytest.raises(r.ReferentielInvalide) as exc:
        r.charger_secteurs_tpe(dossier)
    assert "'x'" in str(exc.value) and "'y'" in str(exc.value)


def test_mot_cle_present_dans_deux_taches_refuse(dossier):
    def abimer(b):
        b["taches"][1]["mots_cles"].append(b["taches"][0]["mots_cles"][0])
    _modifier(dossier, "taches.yaml", abimer)
    with pytest.raises(r.ReferentielInvalide, match="présent dans"):
        r.charger_taches(dossier)


def test_tache_avec_trop_peu_de_mots_cles_refusee(dossier):
    _modifier(dossier, "taches.yaml", lambda b: b["taches"][0].update(mots_cles=["a", "b"]))
    with pytest.raises(r.ReferentielInvalide):
        r.charger_taches(dossier)


def test_identifiant_de_tache_mal_forme_refuse(dossier):
    _modifier(dossier, "taches.yaml", lambda b: b["taches"][0].update(id="Relance Clients"))
    with pytest.raises(r.ReferentielInvalide, match="identifiant mal formé"):
        r.charger_taches(dossier)


def test_zone_departement_hors_nouvelle_aquitaine_refuse(dossier):
    _modifier(dossier, "zone.yaml", lambda b: b["proximite"].append({"code": "31", "nom": "Haute-Garonne"}))
    with pytest.raises(r.ReferentielInvalide, match="absent de la Nouvelle-Aquitaine"):
        r.charger_zone(dossier)


def test_zone_rayon_nul_refuse(dossier):
    _modifier(dossier, "zone.yaml", lambda b: b.update(rayon_km=0))
    with pytest.raises(r.ReferentielInvalide):
        r.charger_zone(dossier)


def test_declencheur_sans_url_refuse(dossier):
    _modifier(dossier, "declencheurs.yaml", lambda b: b["declencheurs"][0].pop("source_url"))
    with pytest.raises(r.ReferentielInvalide, match="source_url"):
        r.charger_declencheurs(dossier)


def test_declencheur_sur_domaine_non_officiel_refuse(dossier):
    _modifier(dossier, "declencheurs.yaml",
              lambda b: b["declencheurs"][0].update(source_url="https://blog-compta.fr/facture"))
    with pytest.raises(r.ReferentielInvalide, match="domaine non officiel"):
        r.charger_declencheurs(dossier)


def test_declencheur_url_http_refusee(dossier):
    _modifier(dossier, "declencheurs.yaml",
              lambda b: b["declencheurs"][0].update(source_url="http://www.economie.gouv.fr/x"))
    with pytest.raises(r.ReferentielInvalide, match="URL https attendue"):
        r.charger_declencheurs(dossier)


def test_domaine_qui_finit_comme_un_officiel_sans_l_etre_refuse(dossier):
    _modifier(dossier, "declencheurs.yaml",
              lambda b: b["declencheurs"][0].update(source_url="https://faux-gouv.fr/page"))
    with pytest.raises(r.ReferentielInvalide, match="domaine non officiel"):
        r.charger_declencheurs(dossier)


def test_declencheur_date_invalide_refusee(dossier):
    _modifier(dossier, "declencheurs.yaml", lambda b: b["declencheurs"][0].update(date="1er septembre"))
    with pytest.raises(r.ReferentielInvalide):
        r.charger_declencheurs(dossier)


def test_declencheur_etoile_combinee_a_un_code_refusee(dossier):
    _modifier(dossier, "declencheurs.yaml",
              lambda b: b["declencheurs"][0].update(secteurs=["*", "43.21A"]))
    with pytest.raises(r.ReferentielInvalide, match="ne se combine"):
        r.charger_declencheurs(dossier)


def test_declencheur_verification_inconnue_refusee(dossier):
    _modifier(dossier, "declencheurs.yaml",
              lambda b: b["declencheurs"][0].update(verification="de_memoire"))
    with pytest.raises(r.ReferentielInvalide):
        r.charger_declencheurs(dossier)


def test_coherence_croisee_code_naf_inconnu_refuse(dossier):
    _modifier(dossier, "declencheurs.yaml",
              lambda b: b["declencheurs"][0].update(secteurs=["99.99Z"]))
    with pytest.raises(r.ReferentielInvalide, match="absent de secteurs_tpe.yaml"):
        r.verifier_coherence(r.charger_secteurs_tpe(dossier), r.charger_declencheurs(dossier))


def test_fichier_absent_message_clair(tmp_path):
    with pytest.raises(r.ReferentielInvalide, match="fichier absent"):
        r.charger_zone(tmp_path)


def test_yaml_illisible_message_clair(tmp_path):
    (tmp_path / "zone.yaml").write_text("centre: [oups", encoding="utf-8")
    with pytest.raises(r.ReferentielInvalide, match="YAML illisible"):
        r.charger_zone(tmp_path)


def test_racine_non_dictionnaire_refusee(tmp_path):
    (tmp_path / "zone.yaml").write_text("- a\n- b\n", encoding="utf-8")
    with pytest.raises(r.ReferentielInvalide, match="dictionnaire"):
        r.charger_zone(tmp_path)


# ----- décisions du 2026-10-01 : santé retenue, exclusion HDS portée par les tâches --------------

CODES_SANTE = ["86.21Z", "86.22C", "86.23Z", "86.90B", "86.90D", "86.90E", "75.00Z"]
TACHES_PATIENTS = ["prise_rendez_vous", "relance_clients", "suivi_dossiers", "facturation_clients", "reponse_avis"]


def test_sante_liberale_est_un_secteur_retenu():
    par_code = r.charger_secteurs_tpe().par_code()
    for code in CODES_SANTE:
        assert par_code[code].famille == "sante_liberale"
        assert par_code[code].exclusion is None, code


def test_seule_la_pharmacie_reste_exclue_en_sante():
    exclus = [s.code for s in r.charger_secteurs_tpe().secteurs
              if s.famille == "sante_liberale" and s.exclusion]
    assert exclus == ["47.73Z"]


def test_drapeau_donnees_sensibles_sur_les_cinq_taches_voulues_et_seulement_elles():
    drapeaux = {t.id for t in r.charger_taches().taches if t.donnees_sensibles_patients}
    assert drapeaux == set(TACHES_PATIENTS)


@pytest.mark.parametrize("code", ["86.21Z", "86.23Z", "86.90B", "86.90D", "86.90E"])
@pytest.mark.parametrize("tache", TACHES_PATIENTS)
def test_taches_patients_exclues_hds_en_secteur_de_sante(code, tache):
    s, t = r.charger_secteurs_tpe(), r.charger_taches()
    assert r.motif_exclusion_couple(code, tache, s, t) == "HDS"


@pytest.mark.parametrize("tache", ["reporting", "saisie_factures", "redaction_contenus", "tri_mails", "planning"])
def test_autres_taches_retenues_en_secteur_de_sante(tache):
    s, t = r.charger_secteurs_tpe(), r.charger_taches()
    assert r.motif_exclusion_couple("86.23Z", tache, s, t) is None


@pytest.mark.parametrize("code", ["75.00Z", "47.74Z", "47.78A"])
def test_veterinaires_et_hors_regle_ne_sont_pas_exclus_hds(code):
    s, t = r.charger_secteurs_tpe(), r.charger_taches()
    for tache in TACHES_PATIENTS:
        assert r.motif_exclusion_couple(code, tache, s, t) is None


def test_les_memes_taches_ne_sont_pas_exclues_hors_sante():
    s, t = r.charger_secteurs_tpe(), r.charger_taches()
    for tache in TACHES_PATIENTS:
        assert r.motif_exclusion_couple("69.20Z", tache, s, t) is None  # comptables
        assert r.motif_exclusion_couple("96.02A", tache, s, t) is None  # coiffure


def test_exclusion_du_secteur_prime_sur_la_regle_hds():
    s, t = r.charger_secteurs_tpe(), r.charger_taches()
    assert r.motif_exclusion_couple("47.73Z", "prise_rendez_vous", s, t) == "reglementaire_lourd"
    assert r.motif_exclusion_couple("43.12A", "reporting", s, t) == "materiel_industriel"


def test_couple_inconnu_refuse():
    s, t = r.charger_secteurs_tpe(), r.charger_taches()
    with pytest.raises(r.ReferentielInvalide, match="code NAF inconnu"):
        r.motif_exclusion_couple("99.99Z", "reporting", s, t)
    with pytest.raises(r.ReferentielInvalide, match="tâche inconnue"):
        r.motif_exclusion_couple("86.23Z", "inconnue", s, t)


def test_regle_hds_motif_hors_liste_refuse(dossier):
    _modifier(dossier, "taches.yaml",
              lambda b: b["regle_donnees_sensibles_patients"].update(motif="autre"))
    with pytest.raises(r.ReferentielInvalide):
        r.charger_taches(dossier)


def test_regle_hds_absente_refusee(dossier):
    _modifier(dossier, "taches.yaml", lambda b: b.pop("regle_donnees_sensibles_patients"))
    with pytest.raises(r.ReferentielInvalide, match="regle_donnees_sensibles_patients"):
        r.charger_taches(dossier)


def test_regle_hds_sans_aucune_tache_flaguee_refusee(dossier):
    def abimer(b):
        for t in b["taches"]:
            t.pop("donnees_sensibles_patients", None)
    _modifier(dossier, "taches.yaml", abimer)
    with pytest.raises(r.ReferentielInvalide, match="sans effet"):
        r.charger_taches(dossier)


def test_regle_hds_famille_inconnue_refusee_par_la_coherence_croisee(dossier):
    _modifier(dossier, "taches.yaml",
              lambda b: b["regle_donnees_sensibles_patients"].update(familles_secteurs=["sante_inconnue"]))
    with pytest.raises(r.ReferentielInvalide, match="famille 'sante_inconnue' absente"):
        r.verifier_coherence(r.charger_secteurs_tpe(dossier), r.charger_declencheurs(dossier),
                             r.charger_taches(dossier))


def test_regle_hds_code_hors_regle_inconnu_refuse_par_la_coherence_croisee(dossier):
    _modifier(dossier, "taches.yaml",
              lambda b: b["regle_donnees_sensibles_patients"].update(secteurs_hors_regle=["99.99Z"]))
    with pytest.raises(r.ReferentielInvalide, match="code NAF 99.99Z absent"):
        r.verifier_coherence(r.charger_secteurs_tpe(dossier), r.charger_declencheurs(dossier),
                             r.charger_taches(dossier))


def test_drapeau_non_booleen_refuse(dossier):
    _modifier(dossier, "taches.yaml", lambda b: b["taches"][0].update(donnees_sensibles_patients="peut-être"))
    with pytest.raises(r.ReferentielInvalide):
        r.charger_taches(dossier)


# ----- naf_version -----------------------------------------------------------------------

def test_naf_version_courante_est_la_revision_2():
    assert r.charger_secteurs_tpe().naf_version == "2"


def test_naf_version_inconnue_refusee(dossier):
    _modifier(dossier, "secteurs_tpe.yaml", lambda b: b.update(naf_version="3"))
    with pytest.raises(r.ReferentielInvalide, match="naf_version"):
        r.charger_secteurs_tpe(dossier)


def test_naf_version_absente_refusee(dossier):
    _modifier(dossier, "secteurs_tpe.yaml", lambda b: b.pop("naf_version"))
    with pytest.raises(r.ReferentielInvalide, match="naf_version"):
        r.charger_secteurs_tpe(dossier)


# ----- décision du 2026-10-01 : intérim et placement exclus (code NAF de l'agence, pas du métier proposé) ---------------

AGENCES = ["78.10Z", "78.20Z"]


@pytest.mark.parametrize("code", AGENCES)
def test_interim_et_placement_exclus_avec_le_motif_de_l_agence(code):
    secteur = r.charger_secteurs_tpe().par_code()[code]
    assert secteur.exclusion is not None and secteur.exclusion.motif == "code_naf_agence"
    source = r.charger_secteurs_tpe().sources_exclusion[secteur.exclusion.source]
    assert "code naf de l'agence, pas du métier proposé" in source.lower()


def test_78_30z_autre_mise_a_disposition_reste_retenu():
    assert r.charger_secteurs_tpe().par_code()["78.30Z"].exclusion is None  # non visé par la décision


def test_le_motif_agence_prime_pour_tous_les_couples_secteur_tache():
    s, t = r.charger_secteurs_tpe(), r.charger_taches()
    for code in AGENCES:
        for tache in t.taches:
            assert r.motif_exclusion_couple(code, tache.id, s, t) == "code_naf_agence"


def test_les_deux_codes_restent_au_referentiel_et_dans_les_declencheurs():
    codes = {s.code for s in r.charger_secteurs_tpe().secteurs}
    assert set(AGENCES) <= codes   # jamais supprimés
    r.verifier_coherence(r.charger_secteurs_tpe(), r.charger_declencheurs(), r.charger_taches())


def test_secteurs_retenus_apres_exclusion_des_agences():
    refs = r.charger_secteurs_tpe()
    assert len(refs.secteurs) == 168 and len(refs.non_exclus()) == 158
    assert not set(AGENCES) & {s.code for s in refs.non_exclus()}


def test_motif_inconnu_toujours_refuse(dossier):
    def abimer(b):
        for s in b["secteurs"]:
            if s.get("exclusion"):
                s["exclusion"]["motif"] = "agence"
                return
    _modifier(dossier, "secteurs_tpe.yaml", abimer)
    with pytest.raises(r.ReferentielInvalide):
        r.charger_secteurs_tpe(dossier)


def test_declencheur_tache_inconnue_refusee_par_la_coherence_croisee(dossier):
    _modifier(dossier, "declencheurs.yaml", lambda b: b["declencheurs"][0].update(taches=["tache_inventee"]))
    with pytest.raises(r.ReferentielInvalide, match="tâche tache_inventee absente de taches.yaml"):
        r.verifier_coherence(r.charger_secteurs_tpe(dossier), r.charger_declencheurs(dossier), r.charger_taches(dossier))


def test_declencheur_taches_mal_formees_ou_en_double_refusees(dossier):
    _modifier(dossier, "declencheurs.yaml", lambda b: b["declencheurs"][0].update(taches=["Mauvais Id"]))
    with pytest.raises(r.ReferentielInvalide, match="mal formé"):
        r.charger_declencheurs(dossier)
    _modifier(dossier, "declencheurs.yaml", lambda b: b["declencheurs"][0].update(taches=["reporting", "reporting"]))
    with pytest.raises(r.ReferentielInvalide, match="en double"):
        r.charger_declencheurs(dossier)


def test_rattachement_taches_obligatoire_et_statut_controle(dossier):
    _modifier(dossier, "declencheurs.yaml", lambda b: b.pop("rattachement_taches"))
    with pytest.raises(r.ReferentielInvalide, match="rattachement_taches"):
        r.charger_declencheurs(dossier)
    _modifier(dossier, "declencheurs.yaml", lambda b: b.update(rattachement_taches={"statut": "definitif", "decide_le": "2026-10-01", "a_relire_a": "V2.9", "note": "une note assez longue"}))
    with pytest.raises(r.ReferentielInvalide, match="statut"):
        r.charger_declencheurs(dossier)


def test_rattachement_taches_reel_provisoire():
    rt = r.charger_declencheurs().rattachement_taches
    assert rt.statut == "provisoire" and rt.a_relire_a == "V2.9"
