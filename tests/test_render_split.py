"""Découplage du rendu MP3 / MP4 (2026-08-03).

Le rendu était monolithique : les MP3 (quelques minutes) et le MP4 (un
ré-encodage x264 de 15 à 20 min) partageaient le même job RQ, si bien que
l'album audio n'existait pour personne tant que la vidéo n'était pas finie.
La phase 1 (audio) livre désormais l'album, puis enfile la phase 2 (vidéo).

Ces tests verrouillent les trois points sur lesquels le découplage peut
silencieusement détruire du travail ou mentir à l'utilisateur :
la non-régression du MP4 existant, l'étanchéité des états des deux jobs,
et la réalité de la progression affichée.
"""
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from backend import renderqueue
from backend.pipeline import render

GEST = {"X-authentik-username": "g", "X-authentik-groups": "live2mp3-gestionnaire"}


# --- Le job audio ne doit jamais toucher au MP4 ----------------------------

def test_audio_only_render_preserves_existing_mp4(synth_project):
    """Piège central du découplage : `render.run` purge les orphelins de
    `build/video`. La phase 1 tourne avec `video=False` — si cette purge s'y
    déclenchait, chaque rendu audio détruirait le MP4 déjà encodé (jusqu'à
    20 min de calcul, sur un stockage partagé avec la production)."""
    render.run(synth_project, video=True)
    video_dir = synth_project / "build" / "video"
    mp4 = next(video_dir.glob("*.mp4"))
    mtime = mp4.stat().st_mtime

    render.run(synth_project, video=False, force=True)

    assert mp4.exists()
    assert mp4.stat().st_mtime == mtime


def test_video_only_render_leaves_audio_untouched(synth_project):
    """Phase 2 : le MP4 se rend sans retoucher aux MP3 déjà livrés."""
    render.run(synth_project, video=False)
    audio_dir = synth_project / "build" / "audio"
    before = {p.name: p.stat().st_mtime for p in audio_dir.glob("*.mp3")}
    assert len(before) == 4

    out = render.run(synth_project, video=True, audio=False)

    assert len(out["video"]) == 1
    assert out["audio"] == []
    after = {p.name: p.stat().st_mtime for p in audio_dir.glob("*.mp3")}
    assert after == before


def test_video_only_does_not_purge_audio_orphans(synth_project):
    """Un rendu vidéo lancé après une modification de setlist ne doit pas
    faire le ménage dans `build/audio` : ce n'est pas son travail, et il
    supprimerait des pistes que personne ne va ré-encoder derrière."""
    render.run(synth_project, video=False)
    from backend.manifest import Manifest
    m = Manifest.load(synth_project / "manifest.yaml")
    m.data["tracks"] = m.data["tracks"][:1]
    m.save()

    render.run(synth_project, video=True, audio=False)

    assert len(list((synth_project / "build" / "audio").glob("*.mp3"))) == 4


# --- Un rendu interrompu ne doit rien laisser sous le nom final -------------

def test_killed_render_leaves_no_file_under_final_name(synth_project, monkeypatch):
    """Le stage est idempotent par nom de fichier : un fichier partiel portant
    le nom définitif serait pris pour un rendu abouti et jamais réencodé.

    Cas réel du 2026-08-03 : un redéploiement a recréé le conteneur du worker
    en plein encodage, laissant un MP4 tronqué de 744 Mo sous son nom final.
    Une annulation propre nettoie son fichier, mais un SIGKILL n'en laisse pas
    l'occasion — d'où le rendu dans un `.part` promu seulement à la fin."""
    from backend.pipeline import render as R

    def _die(src, start, end, out, cancel=None, paused=None, **kw):
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_bytes(b"\x00" * 1024)      # encodage entamé...
        raise KeyboardInterrupt("worker tué")  # ...puis process tué

    monkeypatch.setattr(R, "render_video", _die)
    with pytest.raises(KeyboardInterrupt):
        R.run(synth_project, video=True, audio=False)

    video_dir = synth_project / "build" / "video"
    assert list(video_dir.glob("*.mp4")) == []
    assert list(video_dir.glob("*.part")) == []


def test_leftover_part_file_is_purged_by_next_render(synth_project):
    """Un `.part` résiduel (worker tué sans pouvoir nettoyer) ne doit pas
    s'accumuler : la purge des orphelins le balaie au rendu suivant."""
    video_dir = synth_project / "build" / "video"
    video_dir.mkdir(parents=True, exist_ok=True)
    (video_dir / "vieux-restant.mp4.part").write_bytes(b"\x00" * 512)

    render.run(synth_project, video=True, audio=False)

    assert list(video_dir.glob("*.part")) == []
    assert len(list(video_dir.glob("*.mp4"))) == 1


# --- Progression réelle du MP4 ---------------------------------------------

def test_video_render_reports_real_progress(synth_project):
    """L'ancien code n'émettait que « 0/1 » puis « 1/1 » : la barre restait
    figée à 0 % pendant tout l'encodage. On exige désormais une progression
    mesurée, croissante et bornée à 1."""
    seen: list[float] = []
    render.run(synth_project, video=True, audio=False, on_video=seen.append)

    assert seen, "aucune progression émise par ffmpeg"
    assert all(0.0 <= v <= 1.0 for v in seen)
    assert seen == sorted(seen)
    assert seen[-1] > 0.5


def test_video_progress_marks_done_when_file_already_rendered(synth_project):
    """Rendu idempotent (fichier déjà là) : la barre doit finir à 100 %,
    pas rester à zéro faute d'un ffmpeg à écouter."""
    render.run(synth_project, video=True, audio=False)
    seen: list[float] = []
    render.run(synth_project, video=True, audio=False, on_video=seen.append)
    assert seen == [1.0]


# --- Étanchéité des deux jobs dans Redis -----------------------------------

def test_job_state_keys_are_isolated_per_kind():
    """Les deux natures partagent le slug mais pas leur état : sans clés
    distinctes, le `finally` du job audio effacerait les métadonnées du job
    vidéo qu'il vient d'enfiler, et une annulation viserait le mauvais job."""
    slug = "album-2026-01-01"
    renderqueue.set_meta(slug, {"a": 1}, renderqueue.KIND_RENDER)
    renderqueue.set_meta(slug, {"b": 2}, renderqueue.KIND_VIDEO)
    renderqueue.request_cancel(slug, renderqueue.KIND_VIDEO)

    assert renderqueue.get_meta(slug, renderqueue.KIND_RENDER) == {"a": 1}
    assert renderqueue.get_meta(slug, renderqueue.KIND_VIDEO) == {"b": 2}
    assert renderqueue.cancel_requested(slug, renderqueue.KIND_VIDEO) is True
    assert renderqueue.cancel_requested(slug, renderqueue.KIND_RENDER) is False

    renderqueue.clear_meta(slug, renderqueue.KIND_RENDER)
    assert renderqueue.get_meta(slug, renderqueue.KIND_VIDEO) == {"b": 2}


def test_video_job_id_differs_from_audio_job_id():
    """Deux identifiants distincts : sinon RQ refuserait d'enfiler la vidéo
    tant que le job audio du même album occupe l'identifiant."""
    audio = renderqueue.job_id("x", renderqueue.KIND_RENDER)
    video = renderqueue.job_id("x", renderqueue.KIND_VIDEO)
    assert audio != video
    assert renderqueue._parse_jid(audio) == ("x", renderqueue.KIND_RENDER)
    assert renderqueue._parse_jid(video) == ("x", renderqueue.KIND_VIDEO)


def test_pct_is_scoped_per_kind():
    renderqueue.set_pct("s", 42.0, renderqueue.KIND_VIDEO)
    assert renderqueue.get_pct("s", renderqueue.KIND_VIDEO) == 42.0
    assert renderqueue.get_pct("s", renderqueue.KIND_RENDER) is None
    renderqueue.clear_pct("s", renderqueue.KIND_VIDEO)
    assert renderqueue.get_pct("s", renderqueue.KIND_VIDEO) is None


# --- Enchaînement phase 1 -> phase 2 ---------------------------------------

@pytest.fixture
def api(tmp_path, monkeypatch):
    from backend import catalogue, covers, db, main, manifest, social
    data = tmp_path / "data"
    monkeypatch.setattr(db, "DATA_DIR", data)
    monkeypatch.setattr(db, "DB_PATH", data / "live2mp3.db")
    monkeypatch.setattr(db, "AVATARS_DIR", data / "avatars")
    monkeypatch.setattr(social, "AVATARS_DIR", data / "avatars")
    for mod in (manifest, main, catalogue, social, covers):
        monkeypatch.setattr(mod, "PROJECTS_DIR", tmp_path)
    db.init_db()
    return TestClient(main.app), tmp_path


def _make_project(client, projects: Path, *, with_video: bool) -> str:
    import subprocess
    c = client
    payload = {
        "album": {"artist": "Split", "title": "Test", "date": "2026-01-01"},
        "tracks": [{"n": 1, "title": "A", "start": 0.0, "end": 3.0, "locked": True},
                   {"n": 2, "title": "B", "start": 3.0, "end": 6.0, "locked": True}],
        "target": "audio_cd",
        # `video` pilote `source.media`, dont `start_render` déduit s'il faut
        # une vidéo : un import audio-only ne doit jamais enfiler de MP4.
        "video": with_video,
    }
    slug = c.post("/api/jobs", json=payload, headers=GEST).json()["slug"]
    src = projects / slug / "source"
    src.mkdir(parents=True, exist_ok=True)
    subprocess.run([
        "ffmpeg", "-y", "-f", "lavfi", "-i", "sine=frequency=440:duration=6",
        "-ar", "44100", "-ac", "2", str(src / "master.wav"),
    ], check=True, capture_output=True)
    if with_video:
        subprocess.run([
            "ffmpeg", "-y",
            "-f", "lavfi", "-i", "testsrc=size=160x120:rate=10:duration=6",
            "-f", "lavfi", "-i", "sine=frequency=440:duration=6",
            "-c:v", "libx264", "-preset", "ultrafast", "-crf", "35",
            "-c:a", "aac", "-shortest", str(src / "master.mkv"),
        ], check=True, capture_output=True)
    return slug


def test_audio_job_enqueues_video_job(api, monkeypatch):
    """Fin de phase 1 = album audio livré **et** vidéo mise en file."""
    c, projects = api
    slug = _make_project(c, projects, with_video=True)

    queued: list[dict] = []
    monkeypatch.setattr(renderqueue, "enqueue_video",
                        lambda s, **kw: queued.append({"slug": s, **kw}))

    r = c.post(f"/api/jobs/{slug}/render", params={"media": "audio"}, headers=GEST)
    assert r.status_code == 200

    # Les MP3 sont là tout de suite, sans attendre le moindre encodage vidéo.
    assert len(list((projects / slug / "build" / "audio").glob("*.mp3"))) == 2
    assert [q["slug"] for q in queued] == [slug]


def test_no_video_job_without_video_source(api, monkeypatch):
    """Import audio-only : aucun job vidéo ne doit encombrer la file."""
    c, projects = api
    slug = _make_project(c, projects, with_video=False)

    queued: list[str] = []
    monkeypatch.setattr(renderqueue, "enqueue_video",
                        lambda s, **kw: queued.append(s))

    c.post(f"/api/jobs/{slug}/render", params={"media": "audio"}, headers=GEST)
    assert queued == []


def test_render_queue_labels_video_job(api, monkeypatch):
    """La file distingue les deux natures : un rendu vidéo n'annonce que le
    MP4, sans quoi l'interface laisserait croire que l'album audio manque
    encore alors qu'il est déjà écoutable.

    `listing()` est simulé : en test la file RQ est synchrone, un vrai job
    s'exécuterait dans l'`enqueue` et ne serait donc jamais observable
    « en cours »."""
    c, projects = api
    slug = _make_project(c, projects, with_video=True)
    monkeypatch.setattr(renderqueue, "listing", lambda: [
        {"slug": slug, "kind": renderqueue.KIND_VIDEO, "state": "running",
         "video": True, "pct": 37.0, "requested_by": "g"},
    ])

    items = c.get("/api/render-queue", headers=GEST).json()["items"]
    row = next(i for i in items if i["slug"] == slug)
    assert row["kind"] == "video"
    assert row["formats"] == ["mp4"]
    assert row["pct"] == 37.0
    assert row["artist"] == "Split"


def test_render_queue_audio_job_announces_both_formats(api, monkeypatch):
    """Phase 1 d'un import vidéo : l'album annonce mp3 **et** mp4, puisque
    le MP4 suivra automatiquement."""
    c, projects = api
    slug = _make_project(c, projects, with_video=True)
    monkeypatch.setattr(renderqueue, "listing", lambda: [
        {"slug": slug, "kind": renderqueue.KIND_RENDER, "state": "running",
         "video": True, "pct": None, "requested_by": "g"},
    ])
    row = c.get("/api/render-queue", headers=GEST).json()["items"][0]
    assert row["formats"] == ["mp3", "mp4"]


# --- Avancement par étape (barre de la page brouillons) ---------------------

def test_step_progress_is_monotonic_across_stages():
    """La barre ne doit jamais reculer d'une étape à la suivante, et l'étape
    est numérotée pour l'affichage « Étape 2/4 »."""
    from backend.jobs import _step_progress

    seq = [
        _step_progress("render", "running", {"pct": 0}),
        _step_progress("render", "running", {"pct": 50}),
        _step_progress("render", "done", {}),
        _step_progress("tags", "running", {}),
        _step_progress("tags", "done", {}),
        _step_progress("artwork", "done", {}),
        _step_progress("disc", "done", {}),
    ]
    pcts = [s["pct"] for s in seq]
    assert pcts == sorted(pcts)
    assert pcts[0] == 0 and pcts[-1] == 100
    assert seq[0]["index"] == 1 and seq[0]["total"] == 4
    assert seq[3]["index"] == 2
    assert seq[3]["stage"] == "tags"


def test_encoding_stage_dominates_the_bar():
    """La découpe/encodage occupe la quasi totalité du temps réel : à poids
    égaux la barre resterait sous 25 % pendant tout le rendu puis sauterait à
    100 %, donnant l'impression de blocage que cette barre doit dissiper."""
    from backend.jobs import _step_progress

    assert _step_progress("render", "running", {"pct": 50})["pct"] > 40
    assert _step_progress("render", "done", {})["pct"] > 85


def test_step_progress_ignores_unknown_stages():
    """Les étapes de préparation (téléchargement, analyse IA) ne font pas
    partie de ce job : elles ne doivent pas perturber la barre."""
    from backend.jobs import _step_progress

    assert _step_progress("download", "running", {"pct": 30}) is None
    assert _step_progress("ai_markers", "done", {}) is None


def test_render_queue_exposes_step(api, monkeypatch):
    c, projects = api
    slug = _make_project(c, projects, with_video=False)
    monkeypatch.setattr(renderqueue, "listing", lambda: [
        {"slug": slug, "kind": renderqueue.KIND_RENDER, "state": "running",
         "video": False, "pct": 44.0, "requested_by": "g",
         "step": {"stage": "render", "index": 1, "total": 4, "pct": 44.0}},
    ])
    row = c.get("/api/render-queue", headers=GEST).json()["items"][0]
    assert row["step"]["stage"] == "render"
    assert row["step"]["index"] == 1
    assert row["step"]["total"] == 4


# --- Notification dédiée ----------------------------------------------------

def test_video_done_notification_is_a_distinct_type(api):
    """Fin de vidéo ≠ fin de rendu : annoncer deux fois « votre album est
    prêt » pour un seul import était le risque de la réutilisation du type."""
    from backend import notifications

    c, projects = api
    slug = _make_project(c, projects, with_video=False)
    notifications.notify_render_done(slug, "g")
    notifications.notify_video_done(slug, "g")

    items = c.get("/api/social/notifications", headers=GEST).json()["items"]
    types = [i["type"] for i in items]
    assert "render_done" in types
    assert "video_done" in types
