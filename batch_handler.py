"""
batch_handler.py
================
Gemini 2.5 Flash Image - Batch API iş akışını yöneten modül.

İŞ AKIŞI (Context.md kurallarına göre):
    1. Master görseli Files API'ye upload et    -> file_uri
    2. Master Prompt + her varyasyonu birleştir -> JSONL satırları
    3. JSONL dosyasını Files API'ye upload et   -> jsonl_file_name
    4. client.batches.create(...)               -> BatchJob
    5. Job state SUCCEEDED olana kadar poll et
    6. Sonuç dosyasını indir, base64 görselleri çıkar
    7. ImagePayload listesi döndür

NEDEN MODÜLER?
    Streamlit UI'dan ayrık tutuyoruz ki:
    - Test edilebilir olsun (CLI'dan da çalışır)
    - UI değişirse iş mantığı bozulmasın
    - Sorumluluklar net ayrılmış olsun
"""

from __future__ import annotations

import base64
import json
import mimetypes
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterator

# google-genai: YENİ resmi Google Gen AI SDK
# (eski google-generativeai deprecated, kullanmıyoruz)
from google import genai
from google.genai import types

from async_saver import ImagePayload


# ---------------------------------------------------------------------------
# Sabitler / Konfigürasyon
# ---------------------------------------------------------------------------
# Kullanacağımız model: image generation destekleyen 2.5 flash varyantı.
MODEL_NAME = "gemini-2.5-flash-image"

# Polling sırasında her sorgu arasındaki bekleme süresi (saniye).
# Çok kısa olursa kotamızı boş yere yeriz, çok uzun olursa UI yavaş güncellenir.
POLL_INTERVAL_SECONDS = 15

# Batch job'ın "bitti" sayıldığı durum isimleri (Google enum'u string olarak gelir)
TERMINAL_STATES = {
    "JOB_STATE_SUCCEEDED",   # Başarıyla tamamlandı
    "JOB_STATE_FAILED",      # Hata ile sonuçlandı
    "JOB_STATE_CANCELLED",   # Manuel iptal
    "JOB_STATE_EXPIRED",     # 24 saat içinde bitmedi
}

# OTOMATİK GÖRSEL-ÜRET PREFIX'İ:
# Modelin "STOP-without-image" davranışını ~%80 azaltan imperatif komut cümlesi.
# Standard mode'daki ile AYNI olmalı - tutarlılık için.
# DİKKAT: standard_handler.py'daki IMAGE_GENERATION_PREFIX ile senkron tut!
# (DRY ihlali değil; iki modül mimari olarak eşit seviyede - import etmek
#  yerine kopya tutuyoruz. Refactor gerekirse ortak `prompts.py` açılabilir.)
IMAGE_GENERATION_PREFIX = (
    "Based on the provided reference image, generate a new image "
    "that matches the description below.\n\n"
)


# ---------------------------------------------------------------------------
# Polling sırasında UI'a "şu anda hangi durumdayız" bilgisini iletmek için
# bir veri sınıfı. Generator pattern ile UI'a yield edeceğiz.
# ---------------------------------------------------------------------------
@dataclass
class BatchProgress:
    """Streamlit UI'ında ilerleme barını güncellemek için durum paketi."""

    state: str           # Anlık durum (ör: "JOB_STATE_RUNNING")
    elapsed_seconds: int  # Job başlatıldığından bu yana geçen saniye
    is_terminal: bool    # True ise iş bitti (başarılı veya başarısız)
    message: str         # Kullanıcıya gösterilecek anlaşılır mesaj


# ===========================================================================
#                              ANA SINIF
# ===========================================================================
class GeminiBatchHandler:
    """
    Gemini Batch API iş akışını orkestre eden ana sınıf.

    Tek bir Client örneği üzerinden tüm iş akışını yürütür.
    Stateful olduğu için (job_name, file_uri saklar) her batch için
    yeni bir GeminiBatchHandler instance'ı oluşturmak en temiz yol.
    """

    def __init__(self, api_key: str) -> None:
        """
        Args:
            api_key: Gemini API anahtarı (.env'den okunur).
                     "YOUR_API_KEY_HERE" gibi placeholder ise hata fırlatır.
        """
        # Placeholder kontrolü: kullanıcı .env'i doldurmadıysa erkenden uyaralım
        if not api_key or "YOUR_API_KEY" in api_key.upper():
            raise ValueError(
                "API anahtarı geçerli değil. .env dosyasındaki "
                "GEMINI_API_KEY değerini kendi anahtarınla değiştir."
            )

        # google-genai Client'ı: tüm API çağrıları bunun üzerinden gider.
        self.client = genai.Client(api_key=api_key)

        # State değişkenleri (sınıf bazlı; her instance kendi job'unu takip eder)
        self.master_file_uri: str | None = None      # Yüklenen master görselin URI'si
        self.master_mime_type: str | None = None     # Master görselin MIME tipi
        self.jsonl_file_name: str | None = None      # Yüklenen JSONL'nin Files API ismi
        self.batch_job_name: str | None = None       # Aktif batch job'ın ismi

    # -----------------------------------------------------------------------
    # ADIM 1: Master Görseli Files API'ye Yükle
    # -----------------------------------------------------------------------
    def upload_master_image(self, image_path: str | Path) -> str:
        """
        Master (referans) görseli Gemini Files API'ye yükler.

        NEDEN BU GEREKLI?
            Batch API JSONL'inde her satıra ham görselin base64'ünü gömersek
            dosya devasa olur. Files API'ye bir kez yükleyip URI ile referans
            vermek hem hızlı hem ölçeklenebilir.

        Args:
            image_path: Diskteki master görselin yolu.

        Returns:
            file_data.file_uri'ye yazılacak URI string'i.
        """
        path = Path(image_path)
        if not path.exists():
            raise FileNotFoundError(f"Master görsel bulunamadı: {path}")

        # MIME türünü dosya uzantısından otomatik tahmin et
        # (Gemini API doğru MIME bekler)
        mime_type, _ = mimetypes.guess_type(str(path))
        if not mime_type:
            mime_type = "image/png"  # Bilinmeyen tip için güvenli varsayılan

        # Files API'ye yükle. config parametresi MIME'ı manuel set eder.
        uploaded = self.client.files.upload(
            file=str(path),
            config=types.UploadFileConfig(
                display_name=path.name,
                mime_type=mime_type,
            ),
        )

        # ACTIVE state polling - dosya hazır olana kadar bekle.
        # NEDEN? Files API upload sonrası dosya PROCESSING'de başlar.
        # Eğer batch JSONL'i ACTIVE olmadan referanslarsa ilk istekler
        # boş response döner. (standard_handler ile aynı race condition fix.)
        deadline = time.time() + 30  # 30 saniye timeout - genelde 1-3 sn yeter
        while True:
            current_state = uploaded.state.name if uploaded.state else "UNKNOWN"
            if current_state == "ACTIVE":
                break
            if current_state == "FAILED":
                raise RuntimeError(
                    f"Master görsel Files API'de işlenemedi: {path.name}"
                )
            if time.time() > deadline:
                raise TimeoutError(
                    f"Master görsel 30 saniyede ACTIVE olmadı. "
                    f"Son durum: {current_state}"
                )
            time.sleep(0.5)
            uploaded = self.client.files.get(name=uploaded.name)

        # State'i kaydet ki sonraki adımlar (JSONL üretimi) kullansın
        self.master_file_uri = uploaded.uri
        self.master_mime_type = mime_type

        return uploaded.uri

    # -----------------------------------------------------------------------
    # ADIM 2: JSONL Dosyasını Üret
    # -----------------------------------------------------------------------
    def build_jsonl(
        self,
        master_prompt: str,
        variations: list[str],
        output_path: str | Path = "batch_requests.jsonl",
        use_auto_prefix: bool = True,
    ) -> Path:
        """
        Master Prompt + her varyasyonu birleştirip Batch API formatında
        JSONL dosyası üretir.

        Args:
            use_auto_prefix: True ise IMAGE_GENERATION_PREFIX prompt'un başına
                eklenir. Modelin "STOP-without-image" davranışını ~%80 azaltır.
                Standard mode'daki davranışla simetrik (UI checkbox aynı).

        JSONL formatı (her satır bir JSON nesnesi):
            {
              "key": "req-1",
              "request": {
                "contents": [{
                  "parts": [
                    {"text": "Birleşmiş prompt"},
                    {"file_data": {"file_uri": "...", "mime_type": "..."}}
                  ]
                }],
                "generation_config": {
                  "response_modalities": ["TEXT", "IMAGE"]
                }
              }
            }
        """
        # Master görsel henüz yüklenmediyse, JSONL anlamsız olur
        if not self.master_file_uri or not self.master_mime_type:
            raise RuntimeError(
                "Önce upload_master_image() çağırmalısın."
            )

        # Boş varyasyon kontrolü - kullanıcı boş satır bırakmış olabilir
        cleaned_variations = [v.strip() for v in variations if v.strip()]
        if not cleaned_variations:
            raise ValueError("En az bir varyasyon gerekli.")

        output_path = Path(output_path)

        # Auto-prefix ile prompt'un başına imperatif komut ekleyelim mi?
        # Standard mode'daki ile AYNI mantık - sidebar checkbox bu değeri kontrol eder.
        prefix = IMAGE_GENERATION_PREFIX if use_auto_prefix else ""

        # JSONL'i satır satır yazıyoruz (utf-8 emoji/Türkçe karakterler için şart)
        with output_path.open("w", encoding="utf-8") as f:
            for idx, variation in enumerate(cleaned_variations, start=1):
                # [opsiyonel auto-prefix] + master_prompt + varyasyon
                combined_prompt = (
                    f"{prefix}{master_prompt.strip()}\n\nVaryasyon: {variation}"
                )

                # Batch isteği için tek bir JSON nesnesi
                request_obj = {
                    "key": f"req-{idx:03d}",  # 001, 002... formatında zero-padded
                    "request": {
                        "contents": [
                            {
                                "parts": [
                                    {"text": combined_prompt},
                                    {
                                        "file_data": {
                                            "file_uri": self.master_file_uri,
                                            "mime_type": self.master_mime_type,
                                        }
                                    },
                                ]
                            }
                        ],
                        # Image generation için response_modalities ŞART
                        # Sadece TEXT olursa görsel üretmez!
                        "generation_config": {
                            "response_modalities": ["TEXT", "IMAGE"]
                        },
                    },
                }

                # ensure_ascii=False: Türkçe karakter doğru yazılsın
                f.write(json.dumps(request_obj, ensure_ascii=False) + "\n")

        return output_path

    # -----------------------------------------------------------------------
    # ADIM 3: JSONL'i Files API'ye Yükle ve Batch Job Başlat
    # -----------------------------------------------------------------------
    def start_batch_job(
        self,
        jsonl_path: str | Path,
        display_name: str = "image-batch-job",
    ) -> str:
        """
        Hazırlanan JSONL'i Files API'ye yükler ve batch job başlatır.

        Returns:
            batch job ismi (polling için kullanılacak).
        """
        jsonl_path = Path(jsonl_path)

        # Files API'ye JSONL upload (mime_type "jsonl" olmalı)
        uploaded = self.client.files.upload(
            file=str(jsonl_path),
            config=types.UploadFileConfig(
                display_name=jsonl_path.name,
                mime_type="application/jsonl",
            ),
        )
        self.jsonl_file_name = uploaded.name

        # Batch job oluştur. src parametresi yüklenen JSONL'in adı.
        batch_job = self.client.batches.create(
            model=MODEL_NAME,
            src=uploaded.name,
            config={"display_name": display_name},
        )
        self.batch_job_name = batch_job.name

        return batch_job.name

    # -----------------------------------------------------------------------
    # ADIM 4: Polling - Job durumunu sürekli sorgula
    # -----------------------------------------------------------------------
    def poll_until_complete(
        self,
        progress_callback: Callable[[BatchProgress], None] | None = None,
    ) -> Iterator[BatchProgress]:
        """
        Batch job'ı bitene kadar belirli aralıklarla durumunu sorgular.

        GENERATOR PATTERN:
            Bu fonksiyon yield ile durum güncellemelerini akıtır.
            Streamlit UI bunu for döngüsünde tüketip ilerleme barını günceller.

        Args:
            progress_callback: Opsiyonel - her güncellemede çağırılacak fonksiyon.

        Yields:
            Her polling adımında bir BatchProgress nesnesi.
        """
        if not self.batch_job_name:
            raise RuntimeError("Önce start_batch_job() çağırmalısın.")

        start_time = time.time()

        # Sonsuz döngü; terminal state'e ulaşınca break ediyoruz
        while True:
            # Job'ın güncel durumunu Google'dan çek
            job = self.client.batches.get(name=self.batch_job_name)
            state_name = job.state.name  # ör: "JOB_STATE_RUNNING"

            elapsed = int(time.time() - start_time)

            # Kullanıcı dostu mesaj üret
            message = self._humanize_state(state_name, elapsed)

            # Durumu paketle
            progress = BatchProgress(
                state=state_name,
                elapsed_seconds=elapsed,
                is_terminal=state_name in TERMINAL_STATES,
                message=message,
            )

            # İsteğe bağlı callback (UI dışı kullanım için)
            if progress_callback:
                progress_callback(progress)

            # UI'a yield ediyoruz ki Streamlit ilerleme barını güncelleyebilsin
            yield progress

            # Terminal state'e ulaşıldıysa polling biter
            if progress.is_terminal:
                return

            # Bir sonraki sorguya kadar bekle
            time.sleep(POLL_INTERVAL_SECONDS)

    @staticmethod
    def _humanize_state(state: str, elapsed: int) -> str:
        """Google'ın enum string'lerini kullanıcıya gösterilebilir mesaja çevirir."""
        # Geçen süreyi MM:SS formatına çevir
        minutes, seconds = divmod(elapsed, 60)
        time_str = f"{minutes:02d}:{seconds:02d}"

        # State -> insan dostu mesaj eşleşmesi
        mapping = {
            "JOB_STATE_PENDING": f"⏳ Job kuyrukta bekliyor... ({time_str})",
            "JOB_STATE_RUNNING": f"🔄 Görseller üretiliyor... ({time_str})",
            "JOB_STATE_SUCCEEDED": f"✅ Tamamlandı! ({time_str})",
            "JOB_STATE_FAILED": f"❌ Job başarısız oldu. ({time_str})",
            "JOB_STATE_CANCELLED": f"⚠️ Job iptal edildi. ({time_str})",
            "JOB_STATE_EXPIRED": f"⏰ 24 saat doldu, job süresi geçti. ({time_str})",
        }
        return mapping.get(state, f"Bilinmeyen durum: {state} ({time_str})")

    # -----------------------------------------------------------------------
    # ADIM 5: Sonuçları İndir ve Parse Et
    # -----------------------------------------------------------------------
    def fetch_results(self) -> list[ImagePayload]:
        """
        Batch tamamlandıktan sonra sonuç JSONL'ini indirir ve içindeki
        base64 görselleri ImagePayload listesine çevirir.

        Returns:
            Diske yazılmaya hazır ImagePayload nesnelerinin listesi.
        """
        if not self.batch_job_name:
            raise RuntimeError("Aktif bir batch job yok.")

        # Job objesini güncel haliyle tekrar çek (dest field'ını öğrenmek için)
        job = self.client.batches.get(name=self.batch_job_name)

        # Yalnızca başarılı job'tan sonuç indirilebilir
        if job.state.name != "JOB_STATE_SUCCEEDED":
            raise RuntimeError(
                f"Job başarılı değil. Durum: {job.state.name}. "
                f"Hata varsa: {getattr(job, 'error', 'detay yok')}"
            )

        # Sonuç dosyasının ismi job.dest.file_name içinde
        result_file_name = job.dest.file_name

        # Files API'den sonuç JSONL'ini binary olarak indir
        result_bytes = self.client.files.download(file=result_file_name)
        # bytes -> str -> satır satır JSON parse
        result_text = result_bytes.decode("utf-8")

        payloads: list[ImagePayload] = []

        # JSONL: her satır ayrı bir JSON. Boş satırları es geçiyoruz.
        for line in result_text.splitlines():
            if not line.strip():
                continue

            # Bir satırı JSON'a çevir
            entry = json.loads(line)
            key = entry.get("key", "unknown")

            # Hatalı entry varsa atla (kısmi başarı tolerans)
            response = entry.get("response")
            if not response:
                continue

            # Yanıt yapısı: response.candidates[0].content.parts[]
            # Bazı parts text, bazıları inline_data (görsel) olabilir.
            # Sadece görsel olanları topluyoruz.
            try:
                candidates = response["candidates"]
                parts = candidates[0]["content"]["parts"]
            except (KeyError, IndexError):
                # Beklenmedik bir yapı: bu entry'yi atla
                continue

            for part in parts:
                inline = part.get("inline_data") or part.get("inlineData")
                if not inline:
                    continue  # text part - bizi ilgilendirmiyor

                # Base64 görsel verisi ve MIME bilgisi
                # API hem snake_case hem camelCase kullanabiliyor (defansif okuma)
                b64_data = inline.get("data")
                mime = inline.get("mime_type") or inline.get("mimeType") or "image/png"

                if b64_data:
                    payloads.append(
                        ImagePayload(
                            key=key,
                            base64_data=b64_data,
                            mime_type=mime,
                        )
                    )

        return payloads

    # -----------------------------------------------------------------------
    # YARDIMCI: Temizlik (opsiyonel - Files API'deki dosyaları sil)
    # -----------------------------------------------------------------------
    def cleanup(self) -> None:
        """
        Files API'ye yüklenen geçici dosyaları siler.
        Files API kotası dolmasın diye çağırmak iyi pratik.
        """
        # try/except ile hatalar yutuluyor; cleanup başarısız olsa bile
        # ana akış zaten bitti, kullanıcıya hata göstermek anlamsız.
        for file_ref in (self.jsonl_file_name,):
            if file_ref:
                try:
                    self.client.files.delete(name=file_ref)
                except Exception:
                    pass
