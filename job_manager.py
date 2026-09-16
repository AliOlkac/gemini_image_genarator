"""
job_manager.py
==============
Üretim işlerini tarayıcı oturumundan BAĞIMSIZ olarak arka planda yürütür.

NEDEN VAR?
    Eskiden üretim, butona basan sayfa çalıştırmasının İÇİNDE dönüyordu:
    - Sayfa yenilenince, sekme kapanınca ya da başka bir şeye basılınca iş
      yarıda kalıyordu. Batch job Google'da bitip faturalansa da sonuçları
      indirecek kod artık çalışmıyordu ("20-30 dk tepki yok, para gitti").
    - Standart modda kesinti anında kuyruktaki istekler yine gönderiliyor,
      sonuçları çöpe gidiyordu.

    Artık:
    - Standart işler süreç içindeki bir thread havuzunda çalışır; her görsel
      biter bitmez diske yazılır ve iş kaydı (storage) güncellenir.
    - Batch job'ın adı, gönderildiği AN diske kaydedilir. Tek bir takipçi
      thread bekleyen tüm job'ları periyodik sorgular ve bitenleri OTOMATİK
      indirir — tarayıcı açık olmasa bile.
    - Uygulama kapanıp açılırsa bekleyen batch job'ların takibi kaldığı yerden
      sürer; eski (bu sistemden önceki) job'lar da hesaptan içe aktarılabilir.

Streamlit'e bağımlı değildir; arayüz sadece kayıtları okur ve bu sınıfa
"başlat / iptal et" der.
"""

from __future__ import annotations

import base64
import logging
import os
import re
import secrets
import socket
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from google import genai
from google.genai import types

import storage
from app_config import HTTP_TIMEOUT_MS, get_api_key, key_fingerprint
from async_saver import ImagePayload
from batch_handler import BatchItemResult, GeminiBatchHandler
from standard_handler import GeminiStandardHandler, StandardProgress

_LOGGER = logging.getLogger(__name__)

# Bekleyen batch job'lar kaç saniyede bir sorgulansın.
BATCH_POLL_INTERVAL_SECONDS = 20
POLLER_THREAD_NAME = "gig-batch-poller"

# Aynı anda en fazla kaç iş (run) yürütülür; fazlası "queued" bekler.
MAX_PARALLEL_RUNS = 4

# Bu Python sürecinin kimliği. Modül yeniden yüklense de değişmesin diye ortam
# değişkeninde tutulur. "running" görünen bir işin gerçekten bu süreçte mi
# çalıştığını yoksa uygulama kapanınca yarım mı kaldığını ayırt etmeye yarar.
PROCESS_TOKEN = os.environ.setdefault("GIG_PROCESS_TOKEN", secrets.token_hex(8))
HOSTNAME = socket.gethostname()

_BATCH_END_STATES = {"JOB_STATE_FAILED", "JOB_STATE_CANCELLED", "JOB_STATE_EXPIRED"}
_BATCH_IMPORTABLE_STATES = {
    "JOB_STATE_QUEUED",
    "JOB_STATE_PENDING",
    "JOB_STATE_RUNNING",
    "JOB_STATE_SUCCEEDED",
}
_KEY_INDEX_RE = re.compile(r"(\d+)$")


# ===========================================================================
#                        İŞ KAYDI ÜZERİNDE KÜÇÜK YARDIMCILAR
# ===========================================================================
def _mark_pending_items(run: dict, status: str, error: str | None) -> None:
    for item in run["items"]:
        if item["status"] == "pending":
            item["status"] = status
            item["error"] = error


def _finalize_status(run: dict) -> None:
    """Varyasyon sonuçlarına göre işin son durumunu belirler."""
    items = run["items"]
    ok_count = sum(1 for item in items if item["status"] == "ok")
    if items and ok_count == len(items):
        run["status"] = "done"
    elif run.get("cancel_requested") or any(i["status"] == "cancelled" for i in items):
        run["status"] = "cancelled"
    elif ok_count:
        run["status"] = "partial"
    else:
        run["status"] = "failed"
    run["finished_at"] = time.time()


def _save_payload(run: dict, item: dict, payload: ImagePayload) -> str:
    """Görseli işin klasörüne yazar, dosya adını döndürür."""
    filename = storage.image_filename(item["index"], item["variation"], payload.mime_type)
    storage.write_image(Path(run["output_dir"]), filename, base64.b64decode(payload.base64_data))
    return filename


def _is_not_found(exc: Exception) -> bool:
    return getattr(exc, "code", None) == 404 or "NOT_FOUND" in str(exc)


def _batch_handler_for(run: dict, api_key: str) -> GeminiBatchHandler:
    """Kayıtlı bir batch işine bağlanmış handler (yeniden gönderim yapmaz)."""
    handler = GeminiBatchHandler(api_key=api_key)
    batch = run.get("batch") or {}
    handler.batch_job_name = batch.get("job_name")
    handler.jsonl_file_name = batch.get("src_file")
    handler.master_file_name = batch.get("master_file_name")
    return handler


# ===========================================================================
#                                  ANA SINIF
# ===========================================================================
class JobManager:
    """Süreç başına tek örnek (get_manager). Tüm kullanıcıların işlerini yürütür."""

    def __init__(self) -> None:
        self._executor = ThreadPoolExecutor(
            max_workers=MAX_PARALLEL_RUNS, thread_name_prefix="gig-run"
        )
        self._cancel_events: dict[str, threading.Event] = {}
        self._lock = threading.Lock()
        self._wake_poller = threading.Event()
        self._recover_after_restart()
        self._ensure_poller()

    # -----------------------------------------------------------------------
    # Başlatma / iptal (arayüzden çağrılır)
    # -----------------------------------------------------------------------
    def start_run(self, run: dict, api_key: str) -> None:
        if run["mode"] == "standard":
            event = threading.Event()
            with self._lock:
                self._cancel_events[run["id"]] = event
            self._executor.submit(self._run_standard, run["id"], api_key, event)
        else:
            self._executor.submit(self._submit_batch, run["id"], api_key)

    def cancel_run(self, run_id: str) -> None:
        """
        Standart: başlamamış istekler iptal edilir, işlenmekte olanlar kaydedilir.
        Batch: job Google'da iptal edilir; takipçi son durumu işler.
        """
        run = storage.update_run(run_id, lambda r: r.update(cancel_requested=True))
        if run is None or run["status"] not in storage.ACTIVE_STATUSES:
            return

        if run["mode"] == "standard":
            with self._lock:
                event = self._cancel_events.get(run_id)
            if event is not None:
                event.set()
            else:
                # Bu süreçte çalışan bir karşılığı yok (yarım kalmış kayıt).
                def cancel_orphan(r: dict) -> None:
                    _mark_pending_items(r, "cancelled", None)
                    _finalize_status(r)

                storage.update_run(run_id, cancel_orphan)
            return

        # Batch: "queued"/"submitting" ise gönderim adımı cancel_requested'ı görüp
        # iptal eder. "waiting" ise job'ı şimdi iptal et.
        if run["status"] == "waiting" and (run.get("batch") or {}).get("job_name"):
            api_key = get_api_key()
            if not api_key or key_fingerprint(api_key) != run.get("key_fp"):
                raise RuntimeError(
                    "Bu işi iptal etmek için başlatıldığı API anahtarı kayıtlı olmalı."
                )
            _batch_handler_for(run, api_key).cancel()
            self._wake_poller.set()

    # -----------------------------------------------------------------------
    # STANDART İŞ
    # -----------------------------------------------------------------------
    def _run_standard(self, run_id: str, api_key: str, cancel_event: threading.Event) -> None:
        try:
            def mark_running(r: dict) -> None:
                r.update(
                    status="running",
                    started_at=time.time(),
                    process_token=PROCESS_TOKEN,
                    host=HOSTNAME,
                )

            run = storage.update_run(run_id, mark_running)
            if run is None:
                return
            if run.get("cancel_requested") or cancel_event.is_set():
                # Sırası gelmeden iptal edildi: hiç istek gönderme.
                def cancel_before_start(r: dict) -> None:
                    _mark_pending_items(r, "cancelled", None)
                    _finalize_status(r)

                storage.update_run(run_id, cancel_before_start)
                return

            handler = GeminiStandardHandler(api_key=api_key)
            handler.upload_master_image(Path(run["output_dir"]) / run["master_file"])
            items_by_index = {item["index"]: item for item in run["items"]}

            for prog in handler.generate_all_streaming(
                master_prompt=run["master_prompt"],
                variations=[item["variation"] for item in run["items"]],
                max_workers=run["max_workers"],
                use_auto_prefix=run["use_auto_prefix"],
                cancel_event=cancel_event,
            ):
                filename = None
                if prog.payload is not None:
                    # Görsel HEMEN diske: sonrasında ne olursa olsun kaybolmaz.
                    filename = _save_payload(run, items_by_index[prog.index], prog.payload)
                storage.update_run(
                    run_id,
                    lambda r, p=prog, f=filename: self._apply_standard_progress(r, p, f),
                )

            handler.cleanup()
            storage.update_run(run_id, _finalize_status)

        except Exception as exc:
            _LOGGER.exception("Standart iş %s hata ile durdu", run_id)
            message = str(exc)[:500]

            def mark_failed(r: dict) -> None:
                r["error"] = message
                _mark_pending_items(r, "failed", "İş hata ile durdu.")
                _finalize_status(r)

            storage.update_run(run_id, mark_failed)
        finally:
            with self._lock:
                self._cancel_events.pop(run_id, None)

    @staticmethod
    def _apply_standard_progress(
        run: dict, prog: StandardProgress, filename: str | None
    ) -> None:
        item = next((i for i in run["items"] if i["index"] == prog.index), None)
        if item is None:
            return
        if prog.payload is not None:
            item.update(status="ok", file=filename, error=None)
        elif prog.cancelled:
            item.update(status="cancelled", error=None)
        else:
            item.update(status="failed", error=prog.error)

    # -----------------------------------------------------------------------
    # BATCH: GÖNDERİM
    # -----------------------------------------------------------------------
    def _submit_batch(self, run_id: str, api_key: str) -> None:
        jsonl_path: Path | None = None
        handler: GeminiBatchHandler | None = None
        job_created = False
        try:
            def mark_submitting(r: dict) -> None:
                r.update(
                    status="submitting",
                    started_at=time.time(),
                    process_token=PROCESS_TOKEN,
                    host=HOSTNAME,
                )

            run = storage.update_run(run_id, mark_submitting)
            if run is None:
                return
            if run.get("cancel_requested"):
                def cancel_before_submit(r: dict) -> None:
                    _mark_pending_items(r, "cancelled", None)
                    _finalize_status(r)

                storage.update_run(run_id, cancel_before_submit)
                return

            handler = GeminiBatchHandler(api_key=api_key)
            handler.upload_master_image(Path(run["output_dir"]) / run["master_file"])

            # JSONL her iş için ayrı geçici dosyada (eskiden proje kökündeki tek
            # batch_requests.jsonl'ı iki kullanıcı birbirinin üstüne yazabiliyordu).
            fd, tmp_name = tempfile.mkstemp(prefix=f"gig-{run_id}-", suffix=".jsonl")
            os.close(fd)
            jsonl_path = Path(tmp_name)
            handler.build_jsonl(
                master_prompt=run["master_prompt"],
                variations=[item["variation"] for item in run["items"]],
                output_path=jsonl_path,
                use_auto_prefix=run["use_auto_prefix"],
            )
            job_name = handler.start_batch_job(
                jsonl_path=jsonl_path, display_name=f"gig-{run_id}"
            )
            job_created = True

            # Job adı HEMEN diske: bundan sonra sayfa kapansa, uygulama çökse
            # bile sonuç takip edilip indirilebilir.
            def mark_waiting(r: dict) -> None:
                r["status"] = "waiting"
                r["batch"] = {
                    "job_name": job_name,
                    "state": "JOB_STATE_PENDING",
                    "submitted_at": time.time(),
                    "last_checked_at": None,
                    "last_error": None,
                    "src_file": handler.jsonl_file_name,
                    "master_file_name": handler.master_file_name,
                }

            run = storage.update_run(run_id, mark_waiting)
            if run is not None and run.get("cancel_requested"):
                # Gönderim sürerken iptal istendi.
                handler.cancel()
            self._wake_poller.set()

        except Exception as exc:
            _LOGGER.exception("Batch iş %s gönderilemedi", run_id)
            message = str(exc)[:500]
            if handler is not None and not job_created:
                handler.cleanup()

            def mark_failed(r: dict) -> None:
                if job_created:
                    # Job oluştu ama sonrasında hata: takip sürsün, sadece not düş.
                    (r.get("batch") or {})["last_error"] = message
                    return
                r["error"] = message
                _mark_pending_items(r, "failed", "Batch gönderilemedi.")
                _finalize_status(r)

            storage.update_run(run_id, mark_failed)
        finally:
            if jsonl_path is not None:
                jsonl_path.unlink(missing_ok=True)

    # -----------------------------------------------------------------------
    # BATCH: TAKİP + OTOMATİK İNDİRME
    # -----------------------------------------------------------------------
    def _ensure_poller(self) -> None:
        # Modül yeniden yüklenirse (kod değişikliği) ikinci bir takipçi başlamasın.
        for thread in threading.enumerate():
            if thread.name == POLLER_THREAD_NAME and thread.is_alive():
                return
        threading.Thread(
            target=self._poll_loop, name=POLLER_THREAD_NAME, daemon=True
        ).start()

    def _poll_loop(self) -> None:
        while True:
            try:
                self.poll_batches_once()
            except Exception:
                _LOGGER.exception("Batch takibi sırasında beklenmeyen hata")
            self._wake_poller.wait(BATCH_POLL_INTERVAL_SECONDS)
            self._wake_poller.clear()

    def poll_batches_once(self) -> None:
        """Bekleyen tüm batch işlerini (tüm kullanıcılar) bir kez kontrol eder."""
        api_key = get_api_key()
        current_fp = key_fingerprint(api_key) if api_key else None

        for run in storage.list_runs():
            if run.get("mode") != "batch" or run.get("status") not in ("waiting", "downloading"):
                continue
            batch = run.get("batch") or {}
            if not batch.get("job_name"):
                continue

            if current_fp is None or run.get("key_fp") != current_fp:
                note = (
                    "Bu iş farklı bir API anahtarıyla başlatılmış; o anahtar kaydedilince indirilecek."
                    if api_key
                    else "Kayıtlı API anahtarı yok; anahtar kaydedilince takip devam edecek."
                )
                if batch.get("last_error") != note:
                    storage.update_run(run["id"], lambda r, n=note: r["batch"].update(last_error=n))
                continue

            try:
                self._check_batch(run["id"], api_key)
            except Exception as exc:
                # Geçici ağ hatası vb.: iş bozulmaz, bir sonraki turda tekrar denenir.
                _LOGGER.warning("Batch %s kontrol edilemedi: %s", run["id"], exc)
                note = f"Durum alınamadı, tekrar denenecek: {str(exc)[:200]}"

                def record_error(r: dict, n: str = note) -> None:
                    r["batch"].update(last_error=n, last_checked_at=time.time())

                storage.update_run(run["id"], record_error)

    def _check_batch(self, run_id: str, api_key: str) -> None:
        run = storage.load_run(run_id)
        if run is None or run["status"] not in ("waiting", "downloading"):
            return
        handler = _batch_handler_for(run, api_key)

        try:
            job = handler.client.batches.get(name=handler.batch_job_name)
        except Exception as exc:
            if not _is_not_found(exc):
                raise

            def mark_missing(r: dict) -> None:
                r["error"] = "Batch job Google'da bulunamadı (silinmiş olabilir)."
                _mark_pending_items(r, "failed", None)
                _finalize_status(r)

            storage.update_run(run_id, mark_missing)
            return

        state = job.state.name if job.state else "JOB_STATE_UNSPECIFIED"

        def record_state(r: dict) -> None:
            r["batch"].update(state=state, last_checked_at=time.time(), last_error=None)

        storage.update_run(run_id, record_state)

        if state == "JOB_STATE_SUCCEEDED":
            storage.update_run(run_id, lambda r: r.update(status="downloading"))
            # Hata verirse (ağ vb.) durum "downloading" kalır, sonraki turda tekrar denenir.
            self._store_batch_results(run_id, handler.fetch_results())
            handler.cleanup()
        elif state in _BATCH_END_STATES:
            job_error = getattr(job.error, "message", None) if job.error else None
            cancelled = state == "JOB_STATE_CANCELLED"
            message = {
                "JOB_STATE_CANCELLED": "Batch job iptal edildi.",
                "JOB_STATE_EXPIRED": "Batch job süresi doldu (Google zamanında işleyemedi).",
            }.get(state, f"Batch job başarısız oldu: {job_error or 'detay yok'}")

            def mark_ended(r: dict) -> None:
                r["error"] = None if cancelled else message
                _mark_pending_items(r, "cancelled" if cancelled else "failed", None if cancelled else message)
                _finalize_status(r)

            storage.update_run(run_id, mark_ended)
            handler.cleanup()

    def _store_batch_results(self, run_id: str, results: dict[str, BatchItemResult]) -> None:
        run = storage.load_run(run_id)
        if run is None:
            return

        if run.get("imported") and not run["items"]:
            # Hesaptan içe aktarılan eski job: varyasyon metinleri bilinmiyor,
            # istek anahtarlarından (req-001...) liste oluştur.
            items = []
            for position, key in enumerate(sorted(results), start=1):
                match = _KEY_INDEX_RE.search(key)
                items.append({
                    "index": int(match.group(1)) if match else position,
                    "key": key,
                    "variation": "",
                    "status": "pending",
                    "file": None,
                    "error": None,
                })
            run["items"] = items

        saved: dict[str, str] = {}
        for item in run["items"]:
            result = results.get(item["key"])
            if result is not None and result.payload is not None:
                saved[item["key"]] = _save_payload(run, item, result.payload)

        items_snapshot = run["items"]

        def apply_results(r: dict) -> None:
            if not r["items"]:
                r["items"] = items_snapshot
            for item in r["items"]:
                result = results.get(item["key"])
                if item["key"] in saved:
                    item.update(status="ok", file=saved[item["key"]], error=None)
                elif result is None:
                    item.update(status="failed", error="Sonuç dosyasında bu istek yok.")
                else:
                    item.update(status="failed", error=result.error)
            _finalize_status(r)

        storage.update_run(run_id, apply_results)

    # -----------------------------------------------------------------------
    # ESKİ JOB'LARI HESAPTAN İÇE AKTARMA
    # -----------------------------------------------------------------------
    def import_account_batches(self, owner: str, output_base: str) -> dict[str, int]:
        """
        Google hesabındaki, bu uygulamanın takip etmediği görsel batch job'larını
        listeye ekler; bitmiş olanlar takipçi tarafından otomatik indirilir.
        Sayfa kapandığı için sonucu hiç indirilmemiş (ama ücreti ödenmiş) job'ları
        kurtarmak için.
        """
        api_key = get_api_key()
        if not api_key:
            raise RuntimeError("Önce API anahtarını kaydet.")
        fp = key_fingerprint(api_key)
        client = genai.Client(
            api_key=api_key, http_options=types.HttpOptions(timeout=HTTP_TIMEOUT_MS)
        )

        runs = storage.list_runs()
        known_jobs = {(r.get("batch") or {}).get("job_name") for r in runs}
        runs_by_id = {r["id"]: r for r in runs}
        counts = {"imported": 0, "relinked": 0, "skipped": 0}

        for job in client.batches.list(config={"page_size": 100}):
            if not job.name or job.name in known_jobs:
                continue
            if "image" not in (job.model or ""):
                counts["skipped"] += 1
                continue
            state = job.state.name if job.state else "JOB_STATE_UNSPECIFIED"

            # Gönderim sırasında uygulama kapandıysa job bizim kaydımıza bağlanabilir.
            display_name = job.display_name or ""
            linked = runs_by_id.get(display_name[4:]) if display_name.startswith("gig-") else None
            if linked is not None and not (linked.get("batch") or {}).get("job_name"):
                storage.update_run(
                    linked["id"],
                    lambda r, j=job.name, s=state: self._relink(r, j, s, fp),
                )
                counts["relinked"] += 1
                continue

            if state not in _BATCH_IMPORTABLE_STATES:
                counts["skipped"] += 1
                continue

            storage.create_imported_run(
                owner=owner,
                output_base=output_base,
                job_name=job.name,
                state=state,
                job_created_at=job.create_time.timestamp() if job.create_time else None,
                key_fp=fp,
            )
            counts["imported"] += 1

        if counts["imported"] or counts["relinked"]:
            self._wake_poller.set()
        return counts

    @staticmethod
    def _relink(run: dict, job_name: str, state: str, fp: str) -> None:
        run.update(status="waiting", error=None, finished_at=None, key_fp=fp)
        run["batch"] = {
            "job_name": job_name,
            "state": state,
            "submitted_at": run.get("started_at"),
            "last_checked_at": None,
            "last_error": None,
            "src_file": None,
            "master_file_name": None,
        }
        for item in run["items"]:
            if item["status"] in ("cancelled", "failed"):
                item.update(status="pending", error=None)

    # -----------------------------------------------------------------------
    # Uygulama yeniden başlatıldığında
    # -----------------------------------------------------------------------
    def _recover_after_restart(self) -> None:
        for run in storage.list_runs():
            if run.get("status") not in storage.ACTIVE_STATUSES:
                continue
            if run.get("host") not in (None, HOSTNAME):
                continue  # başka bir bilgisayarın işi
            if run.get("process_token") == PROCESS_TOKEN:
                continue  # bu süreçte hâlâ çalışıyor

            batch = run.get("batch") or {}
            if run.get("mode") == "batch" and batch.get("job_name"):
                if run["status"] == "downloading":
                    storage.update_run(run["id"], lambda r: r.update(status="waiting"))
                continue  # takipçi kaldığı yerden devam eder

            if run.get("mode") == "standard":
                message = (
                    "Uygulama kapandığı için iş yarıda kaldı. "
                    "Tamamlanan görseller kaydedildi."
                )
            else:
                message = (
                    "Batch gönderilirken uygulama kapandı. Google'da oluşup "
                    "oluşmadığını görmek için 'Hesaptaki batch işlerini tara'yı kullan."
                )

            def mark_interrupted(r: dict, m: str = message) -> None:
                _mark_pending_items(r, "cancelled", None)
                r.update(status="interrupted", error=m, finished_at=time.time())

            storage.update_run(run["id"], mark_interrupted)


# ---------------------------------------------------------------------------
# Süreç başına tek örnek
# ---------------------------------------------------------------------------
_MANAGER: JobManager | None = None
_MANAGER_LOCK = threading.Lock()


def get_manager() -> JobManager:
    global _MANAGER
    with _MANAGER_LOCK:
        if _MANAGER is None:
            _MANAGER = JobManager()
        return _MANAGER
