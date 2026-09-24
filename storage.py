"""
storage.py
==========
Profil formlarını ve üretim işi (run) kayıtlarını diskte JSON olarak tutar.

NEDEN VAR?
    Streamlit'in session_state'i tarayıcı oturumuna bağlı: sayfa yenilenince ya
    da uygulama yeniden başlatılınca her şey sıfırlanıyordu. Kalıcı olması
    gereken her şey artık diskte:

        data/profiles/<kullanici>/profile.json  → form alanları, son master görsel bilgisi
        data/profiles/<kullanici>/master.<ext>  → son yüklenen referans görsel
        data/runs/<run_id>.json                 → her işin durumu (varyasyon bazında)

    Görseller her iş için AYRI klasöre yazılır:
        <çıktı klasörü>/<kullanici>/<run_id>/001_kirmizi-arka-plan.png
    Böylece iki kişi aynı anda üretse de dosyalar birbirini ezmez ve hangi
    dosyanın hangi varyasyon olduğu adından okunur.

THREAD GÜVENLİĞİ:
    Arka plan işleri, batch takipçisi ve arayüz aynı süreçte çalışır. Yazmalar
    tek bir kilitle sıraya girer; her yazma geçici dosya + os.replace ile
    atomiktir (okuyan taraf yarım dosya görmez).
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import shutil
import tempfile
import threading
import time
import unicodedata
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

from app_config import DATA_DIR, PROJECT_ROOT

PROFILES_DIR = DATA_DIR / "profiles"
RUNS_DIR = DATA_DIR / "runs"

# Durum kümeleri (arayüz ve iş yöneticisi ortak kullanır)
ACTIVE_STATUSES = frozenset({"queued", "running", "submitting", "waiting", "downloading"})
FINISHED_STATUSES = frozenset({"done", "partial", "failed", "cancelled", "interrupted"})

ALLOWED_MASTER_SUFFIXES = {".png", ".jpg", ".jpeg", ".webp"}

_PROFILE_SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,39}$")
_RUN_ID_RE = re.compile(r"^[0-9]{8}-[0-9]{6}-[0-9a-f]{4}$")

_EXT_BY_MIME = {
    "image/png": ".png",
    "image/jpeg": ".jpg",
    "image/jpg": ".jpg",
    "image/webp": ".webp",
    "image/gif": ".gif",
}

_TR_ASCII = str.maketrans(
    {"ı": "i", "İ": "i", "ş": "s", "Ş": "s", "ğ": "g", "Ğ": "g",
     "ü": "u", "Ü": "u", "ö": "o", "Ö": "o", "ç": "c", "Ç": "c"}
)

# Aynı süreçteki tüm yazmaları sıraya sokar.
_WRITE_LOCK = threading.RLock()

# Okuma önbelleği: path -> (mtime_ns, size, veri). Arayüz birkaç saniyede bir
# iş listesini okuduğu için değişmeyen dosyalar tekrar parse edilmez.
_READ_CACHE: dict[str, tuple[int, int, Any]] = {}


# ===========================================================================
#                          DÜŞÜK SEVİYE DOSYA İŞLEMLERİ
# ===========================================================================
def _replace_with_retry(src: str, dst: Path) -> None:
    """Windows/OneDrive'da hedef dosya o an okunuyorsa os.replace kısa süreli
    PermissionError verebilir; birkaç kez tekrar dener."""
    for attempt in range(40):
        try:
            os.replace(src, dst)
            return
        except PermissionError:
            if attempt == 39:
                raise
            time.sleep(0.05)


def _atomic_write_bytes(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=".tmp_", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
        _replace_with_retry(tmp_name, path)
    finally:
        if os.path.exists(tmp_name):
            try:
                os.remove(tmp_name)
            except OSError:
                pass


def _atomic_write_json(path: Path, data: Any) -> None:
    text = json.dumps(data, ensure_ascii=False, indent=2)
    _atomic_write_bytes(path, text.encode("utf-8"))
    # Aynı süreçteki okuyucular yeni hali hemen görsün (mtime çözünürlüğüne
    # güvenmeden önbelleği doğrudan güncelle).
    try:
        stat = path.stat()
        _READ_CACHE[str(path)] = (stat.st_mtime_ns, stat.st_size, json.loads(text))
    except OSError:
        _READ_CACHE.pop(str(path), None)


def _read_json(path: Path, *, fresh: bool = False) -> Any | None:
    """
    JSON dosyasını okur. Yoksa ya da bozuksa None döner.

    fresh=False iken önbellekteki nesne döner: çağıran onu DEĞİŞTİRMEMELİ.
    Değiştirilecekse fresh=True ile yeni kopya al.
    """
    key = str(path)
    for attempt in range(20):
        try:
            stat = path.stat()
            cached = _READ_CACHE.get(key)
            if (
                not fresh
                and cached is not None
                and cached[0] == stat.st_mtime_ns
                and cached[1] == stat.st_size
            ):
                return cached[2]
            data = json.loads(path.read_text(encoding="utf-8"))
            _READ_CACHE[key] = (stat.st_mtime_ns, stat.st_size, data)
            return json.loads(json.dumps(data)) if fresh else data
        except FileNotFoundError:
            _READ_CACHE.pop(key, None)
            return None
        except PermissionError:
            # os.replace ile çakıştı; kısa bekleyip tekrar dene.
            time.sleep(0.05)
        except (json.JSONDecodeError, UnicodeDecodeError):
            return None
    return None


# ===========================================================================
#                                  YARDIMCILAR
# ===========================================================================
def slugify(text: str, max_len: int = 40) -> str:
    """'Kırmızı arka plan!' -> 'kirmizi-arka-plan' (dosya/klasör adı için)."""
    text = (text or "").translate(_TR_ASCII)
    text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode("ascii")
    text = re.sub(r"[^a-zA-Z0-9]+", "-", text).strip("-").lower()
    return text[:max_len].strip("-")


def image_filename(index: int, variation: str, mime_type: str) -> str:
    """001_kirmizi-arka-plan.png — sıra numarası + varyasyonun okunur hali."""
    ext = _EXT_BY_MIME.get((mime_type or "").lower(), ".png")
    slug = slugify(variation, 40)
    return f"{index:03d}_{slug}{ext}" if slug else f"{index:03d}{ext}"


def run_label(run: dict, max_len: int = 60) -> str:
    """İşin listede görünecek adı: master prompt'un ilk virgüle kadarki kısmı."""
    prompt = " ".join((run.get("master_prompt") or "").split())
    name = prompt.split(",")[0].strip()
    if len(name) > max_len:
        name = name[:max_len].rstrip() + "…"
    if name:
        return name
    return "Hesaptan içe aktarılan iş" if run.get("imported") else "(isimsiz üretim)"


def write_image(folder: Path, filename: str, data: bytes) -> Path:
    path = folder / filename
    _atomic_write_bytes(path, data)
    return path


def resolve_output_base(output_base: str) -> Path:
    """Göreli çıktı klasörü proje köküne göre çözülür (çalışma dizininden bağımsız)."""
    base = Path((output_base or "").strip() or "outputs").expanduser()
    return base if base.is_absolute() else PROJECT_ROOT / base


# ===========================================================================
#                                   PROFİLLER
# ===========================================================================
def is_valid_profile_slug(slug: str | None) -> bool:
    return bool(slug) and bool(_PROFILE_SLUG_RE.match(slug))


def list_profiles() -> list[dict]:
    if not PROFILES_DIR.exists():
        return []
    profiles = []
    for entry in PROFILES_DIR.iterdir():
        if entry.is_dir():
            data = _read_json(entry / "profile.json")
            if data:
                profiles.append(data)
    return sorted(profiles, key=lambda p: p.get("name", "").lower())


def get_profile(slug: str) -> dict | None:
    if not is_valid_profile_slug(slug):
        return None
    return _read_json(PROFILES_DIR / slug / "profile.json", fresh=True)


def create_profile(name: str) -> dict:
    """Profil oluşturur; aynı isim zaten varsa mevcut profili döndürür."""
    name = " ".join((name or "").split())
    slug = slugify(name, 40)
    if not slug:
        raise ValueError("İsim en az bir harf veya rakam içermeli.")
    with _WRITE_LOCK:
        existing = get_profile(slug)
        if existing:
            return existing
        profile = {
            "slug": slug,
            "name": name,
            "created_at": time.time(),
            "form": {},
            "master": None,
        }
        _atomic_write_json(PROFILES_DIR / slug / "profile.json", profile)
        return profile


def update_profile(slug: str, mutator: Callable[[dict], None]) -> dict | None:
    with _WRITE_LOCK:
        profile = get_profile(slug)
        if profile is None:
            return None
        mutator(profile)
        _atomic_write_json(PROFILES_DIR / slug / "profile.json", profile)
        return profile


def save_profile_master(slug: str, data: bytes, original_name: str) -> dict | None:
    """Referans görseli profile kaydeder (yenileme sonrası da kullanılabilsin)."""
    suffix = Path(original_name).suffix.lower()
    if suffix not in ALLOWED_MASTER_SUFFIXES:
        suffix = ".png"
    folder = PROFILES_DIR / slug
    file_name = f"master{suffix}"
    with _WRITE_LOCK:
        _atomic_write_bytes(folder / file_name, data)
        for old in folder.glob("master.*"):
            if old.name != file_name:
                old.unlink(missing_ok=True)
        master_info = {
            "file": file_name,
            "name": original_name,
            "sha1": hashlib.sha1(data).hexdigest(),
        }
        return update_profile(slug, lambda p: p.update(master=master_info))


def profile_master_path(profile: dict) -> Path | None:
    master = profile.get("master")
    if not master:
        return None
    path = PROFILES_DIR / profile["slug"] / master["file"]
    return path if path.exists() else None


def clear_profile_master(slug: str) -> None:
    with _WRITE_LOCK:
        for old in (PROFILES_DIR / slug).glob("master.*"):
            old.unlink(missing_ok=True)
        update_profile(slug, lambda p: p.update(master=None))


# ===========================================================================
#                                  İŞ KAYITLARI
# ===========================================================================
def new_run_id() -> str:
    return f"{time.strftime('%Y%m%d-%H%M%S')}-{secrets.token_hex(2)}"


def _run_path(run_id: str) -> Path:
    if not _RUN_ID_RE.match(run_id or ""):
        raise ValueError(f"Geçersiz iş kimliği: {run_id!r}")
    return RUNS_DIR / f"{run_id}.json"


def save_run(run: dict) -> None:
    with _WRITE_LOCK:
        run["updated_at"] = time.time()
        _atomic_write_json(_run_path(run["id"]), run)


def load_run(run_id: str) -> dict | None:
    """Değiştirilebilir, taze bir kopya döndürür."""
    return _read_json(_run_path(run_id), fresh=True)


def update_run(run_id: str, mutator: Callable[[dict], None]) -> dict | None:
    """Kaydı kilit altında okur, mutator ile değiştirir ve yazar."""
    with _WRITE_LOCK:
        run = load_run(run_id)
        if run is None:
            return None
        mutator(run)
        save_run(run)
        return run


def list_runs(owner: str | None = None) -> list[dict]:
    """
    İşleri yeniden eskiye döndürür. Dönen nesneler önbellekten gelir:
    DEĞİŞTİRME (değişiklik için update_run kullan).
    """
    if not RUNS_DIR.exists():
        return []
    runs = []
    for path in RUNS_DIR.glob("*.json"):
        data = _read_json(path)
        if not data:
            continue
        if owner is not None and data.get("owner") != owner:
            continue
        runs.append(data)
    runs.sort(key=lambda r: r.get("created_at", 0), reverse=True)
    return runs


def delete_run(run_id: str) -> bool:
    """
    İş kaydını ve o işe ait çıktı klasörünü KALICI olarak siler.

    Güvenlik: yalnızca adı run_id olan klasör silinir. Kullanıcı çıktı klasörünü
    değiştirmiş ya da kayıt bozulmuş olsa bile paylaşılan bir klasör silinemez.
    """
    with _WRITE_LOCK:
        run = load_run(run_id)
        if run is not None:
            folder = Path(run.get("output_dir") or "")
            if folder.name == run_id and folder.is_dir():
                shutil.rmtree(folder, ignore_errors=True)
        path = _run_path(run_id)
        existed = path.exists()
        path.unlink(missing_ok=True)
        _READ_CACHE.pop(str(path), None)
        return existed


def run_folder_size(run: dict) -> int:
    """İşin klasöründeki dosyaların toplam boyutu (bayt). Klasör yoksa 0."""
    folder = Path(run.get("output_dir") or "")
    if not folder.is_dir():
        return 0
    total = 0
    with os.scandir(folder) as entries:
        for entry in entries:
            if entry.is_file():
                try:
                    total += entry.stat().st_size
                except OSError:
                    pass
    return total


def create_run(
    *,
    owner: str,
    mode: str,
    master_prompt: str,
    variations: list[str],
    use_auto_prefix: bool,
    max_workers: int,
    output_base: str,
    master_bytes: bytes,
    master_name: str,
    key_fp: str,
    fingerprint: str,
    host: str,
    process_token: str,
) -> dict:
    """
    Yeni iş kaydı + iş klasörü oluşturur. Master görselin bir kopyası iş
    klasörüne konur: profildeki görsel sonradan değişse de iş etkilenmez.
    """
    run_id = new_run_id()
    out_dir = resolve_output_base(output_base) / owner / run_id
    suffix = Path(master_name).suffix.lower()
    if suffix not in ALLOWED_MASTER_SUFFIXES:
        suffix = ".png"
    master_file = f"_referans{suffix}"
    _atomic_write_bytes(out_dir / master_file, master_bytes)

    now = time.time()
    run = {
        "id": run_id,
        "owner": owner,
        "mode": mode,  # "standard" | "batch"
        "status": "queued",
        "created_at": now,
        "started_at": None,
        "finished_at": None,
        "master_prompt": master_prompt,
        "use_auto_prefix": use_auto_prefix,
        "max_workers": max_workers,
        "output_dir": str(out_dir),
        "master_file": master_file,
        "key_fp": key_fp,
        "fingerprint": fingerprint,
        "host": host,
        "process_token": process_token,
        "cancel_requested": False,
        "error": None,
        "hidden": False,
        "imported": False,
        "items": [
            {
                "index": i,
                "key": f"req-{i:03d}",
                "variation": v,
                "status": "pending",  # pending | ok | failed | cancelled
                "file": None,
                "error": None,
            }
            for i, v in enumerate(variations, start=1)
        ],
        "batch": None,
    }
    save_run(run)
    return run


def create_imported_run(
    *,
    owner: str,
    output_base: str,
    job_name: str,
    state: str,
    job_created_at: float | None,
    key_fp: str,
) -> dict:
    """
    Google hesabında bulunan ama bu uygulamanın takip etmediği bir batch job
    için kayıt açar. Varyasyon listesi sonuç indirilince doldurulur.
    """
    run_id = new_run_id()
    now = time.time()
    run = {
        "id": run_id,
        "owner": owner,
        "mode": "batch",
        "status": "waiting",
        "created_at": job_created_at or now,
        "started_at": job_created_at,
        "finished_at": None,
        "master_prompt": "",
        "use_auto_prefix": True,
        "max_workers": 0,
        "output_dir": str(resolve_output_base(output_base) / owner / run_id),
        "master_file": None,
        "key_fp": key_fp,
        "fingerprint": None,
        "host": None,
        "process_token": None,
        "cancel_requested": False,
        "error": None,
        "hidden": False,
        "imported": True,
        "items": [],
        "batch": {
            "job_name": job_name,
            "state": state,
            "submitted_at": job_created_at,
            "last_checked_at": None,
            "last_error": None,
            "src_file": None,
            "master_file_name": None,
        },
    }
    save_run(run)
    return run
# ===========================================================================
#                    İŞ KLASÖRÜNE YAZILAN BİLGİ DOSYASI
# ===========================================================================
# Görseller haftalar sonra Dosya Gezgini'nden açıldığında "bu hangi prompt'tu?"
# sorusunun cevabı klasörün içinde dursun diye.
_RUN_STATUS_TR = {
    "done": "Tamamlandı",
    "partial": "Kısmen tamamlandı",
    "failed": "Başarısız",
    "cancelled": "İptal edildi",
    "interrupted": "Yarıda kaldı",
    "queued": "Sırada",
    "running": "Üretiliyor",
    "submitting": "Gönderiliyor",
    "waiting": "Google'da işleniyor",
    "downloading": "İndiriliyor",
}
_ITEM_STATUS_TR = {
    "ok": "üretildi",
    "failed": "başarısız",
    "cancelled": "iptal",
    "pending": "beklemede",
}
RUN_INFO_FILENAME = "_bilgi.txt"


def run_info_text(run: dict) -> str:
    """İşin prompt'unu, ayarlarını ve varyasyon-dosya eşleşmesini metne döker."""
    items = run.get("items") or []
    ok_count = sum(1 for item in items if item["status"] == "ok")
    created = run.get("created_at")
    created_str = datetime.fromtimestamp(created).strftime("%d.%m.%Y %H:%M") if created else "?"
    mode = "Batch" if run.get("mode") == "batch" else "Standart"
    status = _RUN_STATUS_TR.get(run.get("status"), run.get("status", "?"))

    lines = [
        "Gemini 2.5 Flash Image Batcher — üretim bilgisi",
        "=" * 50,
        f"Kullanıcı      : {run.get('owner', '?')}",
        f"Tarih          : {created_str}",
        f"Mod            : {mode}",
        f"Durum          : {status} ({ok_count}/{len(items)} görsel)",
        f"Otomatik önek  : {'açık' if run.get('use_auto_prefix') else 'kapalı'}",
        f"İş kimliği     : {run.get('id', '?')}",
        "",
        "MASTER PROMPT",
        "-" * 50,
        (run.get("master_prompt") or "(bu iş için prompt kaydı yok)").strip(),
        "",
        f"VARYASYONLAR ({len(items)})",
        "-" * 50,
    ]

    if not items:
        lines.append("(varyasyon kaydı yok — hesaptan içe aktarılmış iş)")
    for item in items:
        durum = _ITEM_STATUS_TR.get(item["status"], item["status"])
        lines.append(f"{item['index']:03d} [{durum}] {item['variation'] or '(varyasyon metni yok)'}")
        if item.get("file"):
            lines.append(f"     -> {item['file']}")
        elif item.get("error"):
            lines.append(f"     -> {item['error']}")

    if run.get("error"):
        lines += ["", "NOT", "-" * 50, run["error"]]

    return "\n".join(lines) + "\n"


def write_run_info(run: dict) -> Path | None:
    """Bilgi dosyasını işin çıktı klasörüne yazar. Klasör yoksa atlar."""
    output_dir = run.get("output_dir")
    if not output_dir:
        return None
    folder = Path(output_dir)
    if not folder.exists():
        return None
    info_path = folder / RUN_INFO_FILENAME
    _atomic_write_bytes(info_path, run_info_text(run).encode("utf-8-sig"))
    return info_path
