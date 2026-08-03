"""Portée d'environnement : un album créé en preprod reste invisible en prod.

Prod et preprod partagent physiquement le même stockage d'albums (bind-mount
commun, cf. `workspace/infra/stockage.md`). Sans filtre, tout essai fait en
preprod apparaîtrait aussitôt dans le catalogue public. La portée est portée par
un simple champ `origin_env` du manifest — champ absent = album de production,
pour que les albums existants restent visibles sans migration.
"""
from backend import catalogue
from backend.manifest import Manifest
from backend.pipeline import render


def _set_origin(project_dir, value):
    m = Manifest.load(project_dir / "manifest.yaml")
    if value is None:
        m.data.pop("origin_env", None)
    else:
        m.data["origin_env"] = value
    m.save()


# --- hidden_by_env : la règle nue -----------------------------------------

def test_champ_absent_visible_partout(monkeypatch):
    """Les albums historiques n'ont pas le champ : ils restent visibles."""
    monkeypatch.setattr(catalogue, "APP_ENV", "prod")
    assert catalogue.hidden_by_env({}) is False


def test_album_preprod_masque_en_prod(monkeypatch):
    monkeypatch.setattr(catalogue, "APP_ENV", "prod")
    assert catalogue.hidden_by_env({"origin_env": "preprod"}) is True


def test_album_preprod_visible_en_preprod(monkeypatch):
    """La preprod voit tout — c'est son rôle."""
    monkeypatch.setattr(catalogue, "APP_ENV", "preprod")
    assert catalogue.hidden_by_env({"origin_env": "preprod"}) is False
    assert catalogue.hidden_by_env({}) is False


def test_origin_prod_explicite_visible(monkeypatch):
    """Un album promu (`origin_env` retiré) ou marqué "prod" reste visible."""
    monkeypatch.setattr(catalogue, "APP_ENV", "prod")
    assert catalogue.hidden_by_env({"origin_env": "prod"}) is False


# --- Effet réel sur le catalogue ------------------------------------------

def test_catalogue_prod_ignore_album_preprod(synth_audio_only, monkeypatch):
    monkeypatch.setattr(catalogue, "PROJECTS_DIR", synth_audio_only.parent)
    render.run(synth_audio_only, video=False)

    monkeypatch.setattr(catalogue, "APP_ENV", "preprod")
    _set_origin(synth_audio_only, "preprod")
    assert len(catalogue.list_albums()) == 1, "la preprod doit voir son album"

    monkeypatch.setattr(catalogue, "APP_ENV", "prod")
    assert catalogue.list_albums() == [], "la prod ne doit pas le voir"


def test_promotion_rend_visible_en_prod(synth_audio_only, monkeypatch):
    """Retirer l'étiquette = publier en prod, sans déplacer un seul fichier."""
    monkeypatch.setattr(catalogue, "PROJECTS_DIR", synth_audio_only.parent)
    monkeypatch.setattr(catalogue, "APP_ENV", "prod")
    render.run(synth_audio_only, video=False)

    _set_origin(synth_audio_only, "preprod")
    assert catalogue.list_albums() == []

    _set_origin(synth_audio_only, None)          # <- la promotion
    assert len(catalogue.list_albums()) == 1


def test_brouillon_preprod_absent_des_brouillons_prod(synth_audio_only, monkeypatch):
    """Un import de test en preprod ne doit pas polluer /app/drafts en prod."""
    monkeypatch.setattr(catalogue, "PROJECTS_DIR", synth_audio_only.parent)
    _set_origin(synth_audio_only, "preprod")     # pas de rendu => brouillon

    monkeypatch.setattr(catalogue, "APP_ENV", "preprod")
    assert len(catalogue.list_drafts()) == 1

    monkeypatch.setattr(catalogue, "APP_ENV", "prod")
    assert catalogue.list_drafts() == []
