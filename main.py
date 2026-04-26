"""
main.py
=======
Gemini 2.5 Flash Image Batcher - Streamlit Arayüzü.

YENİ ÖZELLİKLER (v2):
    - CANLI GRID: Görseller üretildikçe anında ekrana eklenir.
    - BİREYSEL İNDİR BUTONU: Her görselin altında "İndir" butonu.
    - TOPLU ZIP İNDİR: Üst kısımda tüm görselleri tek paket halinde indir.
    - DAHA NET HATA MESAJLARI: SAFETY/RECITATION/MAX_TOKENS ayrımı.

İKİ MOD DESTEĞİ:
    1. Standart API (Paid Tier - anında, canlı grid)
    2. Batch API (%50 indirimli, yavaş, toplu görüntüleme)

ÇALIŞTIRMA:
    streamlit run main.py
"""

from __future__ import annotations

import io
import os
import tempfile
import time
import zipfile
from pathlib import Path
from typing import TYPE_CHECKING

import streamlit as st
from dotenv import load_dotenv
from PIL import Image

from async_saver import save_all_images_sync, save_single_image_sync
from batch_handler import GeminiBatchHandler
from standard_handler import GeminiStandardHandler

if TYPE_CHECKING:
    from async_saver import ImagePayload


# ---------------------------------------------------------------------------
# Sayfa konfigürasyonu - tüm st.* çağrılarından ÖNCE gelmeli (Streamlit kuralı)
# ---------------------------------------------------------------------------
st.set_page_config(
    page_title="Gemini 2.5 Flash Image Batcher",
    page_icon="🎨",
    layout="wide",
    initial_sidebar_state="expanded",
)

load_dotenv()
DEFAULT_API_KEY = os.getenv("GEMINI_API_KEY", "")


# ---------------------------------------------------------------------------
# Session state başlatma - rerun'larda state kaybolmasın diye
# ---------------------------------------------------------------------------
def _init_session_state() -> None:
    """Streamlit session state'ini varsayılan değerlerle başlatır."""
    defaults = {
        "saved_paths": [],          # Üretilmiş görsellerin yolları (kalıcı)
        "is_running": False,        # Şu an iş çalışıyor mu (çift tıklama engeli)
        "last_error": None,         # Son hata mesajı
        "last_job_name": None,      # Son batch job ismi (debug için)
        "failed_keys": [],          # Başarısız istekler
        "run_started_at": None,     # Üretim başlangıç zamanı (timestamp)
    }
    for key, value in defaults.items():
        if key not in st.session_state:
            st.session_state[key] = value


# ---------------------------------------------------------------------------
# OTOMATİK KILITLENME ÇÖZÜCÜ
# Streamlit bir widget'a tıklanınca scripti baştan çalıştırır.
# Eğer o sırada is_running=True iken script yarım kaldıysa (finally çalışmadı),
# buton kalıcı disabled kalır. Çözüm: belirli süre geçtiyse otomatik sıfırla.
# ---------------------------------------------------------------------------
_RUN_TIMEOUT_SECONDS = 600  # 10 dakika - Batch'in bile bu kadar uzun sürmesi şüpheli

def _auto_reset_if_stuck() -> None:
    """
    is_running=True takılı kaldıysa otomatik sıfırlar.

    Streamlit'in rerun döngüsünde her script çalıştığında bu fonksiyon
    kontrol yapar. run_started_at'tan bu yana 10 dakika geçtiyse ve
    hâlâ is_running=True ise → kimse gözetmeden bir şeyler kilitlenmiş →
    sıfırla, kullanıcı yeniden başlatabilsin.
    """
    if not st.session_state.is_running:
        return  # Sorun yok, çalışmıyor zaten

    started = st.session_state.get("run_started_at")
    if started is None:
        # Timestamp yok ama is_running=True → eski state kalıntısı → sıfırla
        st.session_state.is_running = False
        return

    elapsed = time.time() - started
    if elapsed > _RUN_TIMEOUT_SECONDS:
        # 10 dakika geçmiş, hâlâ "çalışıyor" → kilitlenmiş
        st.session_state.is_running = False
        st.session_state.run_started_at = None


_init_session_state()
_auto_reset_if_stuck()  # Her script run'ında kilitlenme kontrolü yap


# ===========================================================================
#                              SIDEBAR
# ===========================================================================
with st.sidebar:
    st.title("⚙️ Ayarlar")

    api_key_input = st.text_input(
        "Gemini API Key",
        value=DEFAULT_API_KEY,
        type="password",
        help=(
            "Anahtarını https://aistudio.google.com/apikey adresinden al. "
            ".env dosyasından otomatik yükleniyor."
        ),
    )

    output_dir = st.text_input(
        "Çıktı klasörü",
        value="outputs",
        help="Üretilen görsellerin kaydedileceği yerel klasör.",
    )

    st.divider()
    st.subheader("🎯 Üretim Modu")

    api_mode = st.radio(
        label="Hangi API ile üretim yapılacak?",
        options=["standart", "batch"],
        format_func=lambda x: {
            "standart": "Standart API (anında)",
            "batch": "Batch API (%50 ucuz, yavaş)",
        }[x],
        index=0,
        help=(
            "Standart: Anlık üretim, canlı grid. "
            "Batch: %50 ucuz ama 24 saate kadar sürebilir."
        ),
    )

    # --- Standart-mod'a özel ayar: paralel worker sayısı ---
    # Batch'te geçerli değil çünkü Google sunucusu zaten paralelize ediyor.
    # NOT: Retry mekanizması bilinçli olarak KALDIRILDI - bug durumunda istek
    # sayısının patlamaması için. Auto-prefix tek savunma hattı; başarısız
    # varyasyonu kullanıcı manuel olarak tekrar denemekle yükümlü.
    if api_mode == "standart":
        max_workers = st.slider(
            "Eş zamanlı istek sayısı",
            min_value=1,
            max_value=5,
            value=3,
            help=(
                "Daha yüksek = daha hızlı ama 429 (rate limit) riski. "
                "Paid tier için 3 dengeli."
            ),
        )
    else:
        max_workers = None

    # --- Her İKİ MOD için ortak ayar: auto-prefix ---
    # Hem Standart hem Batch'te aynı modeli (gemini-2.5-flash-image) çağırıyoruz,
    # dolayısıyla "STOP-without-image" sorunu her ikisinde de var. Çözüm de aynı:
    # imperatif prompt prefix'i. UI'da tek checkbox - kullanıcı her iki modda da
    # aynı kontrolü görsün diye if dışında.
    use_auto_prefix = st.checkbox(
        "🎯 Otomatik 'görsel üret' öneki ekle",
        value=True,
        help=(
            "Master prompt'unun başına şu cümle eklenir:\n\n"
            "\"Based on the provided reference image, generate a new "
            "image that matches the description below.\"\n\n"
            "Modelin sadece-text döndürme eğilimini ~%80 azaltır. "
            "Maliyeti yok denecek kadar az (~25 token = ~$0.000003). "
            "Hem Standart hem Batch modunda çalışır. "
            "Master prompt'una zaten benzer bir komut yazdıysan kapatabilirsin."
        ),
    )

    st.divider()

    # FİYAT NOTU: Tüm rakamlar Nisan 2026 itibariyle resmi Google fiyat sayfasından.
    # Her görsel = 1290 output token × $30/1M = tam $0.039 (Standard).
    # Batch %50 indirimle $0.0195/görsel.
    if api_mode == "standart":
        st.success(
            "💡 **Standart API**\n\n"
            "- Anında sonuç (saniyeler)\n"
            "- Canlı grid: üretildikçe görüntüle\n"
            "- **~$0.039/görsel** (tam fiyat)"
        )
    else:
        st.warning(
            "⚠️ **Batch API**\n\n"
            "- **~$0.0195/görsel** (%50 indirim)\n"
            "- Dakikalar - 24 saat arası sürebilir\n"
            "- Sonuç toplu görüntülenir"
        )

    # --- CANLI MALİYET TAHMİNİ ---
    # Kullanıcı varyasyon yazdıkça anında "ne kadar para harcayacağım?" görsün.
    # st.session_state üzerinden değil, doğrudan widget değerinden okuyamayız
    # çünkü main akıştaki text_area henüz çalışmadı; bunu form altında
    # ayrıca render edeceğiz (variations_text doluyken).
    st.divider()
    st.caption(
        "💰 **Maliyet ipucu**: Varyasyon listeni yazdıktan sonra "
        "form altında canlı tahmin göreceksin."
    )

    st.divider()

    # --- "Başlat" butonu takılı kaldıysa manuel sıfırlama ---
    # is_running=True iken script yarım kalırsa buton kalıcı disabled olur.
    # Otomatik sıfırlama 10 dk bekler; bu buton ANINDA kurtarır.
    if st.session_state.is_running:
        st.warning("⏳ Üretim çalışıyor veya askıda kaldı.")
        if st.button(
            "🔓 Butonu Kilidden Çıkar",
            help=(
                "Üretim butonunu takılı kaldıysa serbest bırakır. "
                "Çalışan bir işlem varsa o iptal OLMAZ, sadece buton aktifleşir."
            ),
            width="stretch",
            type="secondary",
        ):
            st.session_state.is_running = False
            st.session_state.run_started_at = None
            st.rerun()

    # Önceki üretimi temizleme butonu
    if st.session_state.saved_paths:
        if st.button(
            "🗑️ Önceki üretimi temizle",
            help="Sayfadaki görselleri kaldırır (dosyalar diskte kalır).",
            width="stretch",
        ):
            st.session_state.saved_paths = []
            st.session_state.failed_keys = []
            st.session_state.last_error = None
            st.rerun()

    st.caption("Model: `gemini-2.5-flash-image`")
    st.caption("SDK: `google-genai`")


# ===========================================================================
#                              ANA SAYFA
# ===========================================================================
st.title("🎨 Gemini 2.5 Flash Image Batcher")
st.markdown(
    "Master görsel + Master prompt + Varyasyonlar girerek toplu görsel üret. "
    "Sonuçlar canlı olarak aşağıda görünür ve `outputs/` klasörüne kaydedilir."
)


# ---------------------------------------------------------------------------
# Form alanları - 2 sütunlu düzen
# ---------------------------------------------------------------------------
col_left, col_right = st.columns([1, 1])

with col_left:
    st.subheader("📤 1. Master Görsel")
    uploaded_image = st.file_uploader(
        "Referans görseli yükle",
        type=["png", "jpg", "jpeg", "webp"],
        help="Tüm varyasyonlar bu görseli temel alacak.",
    )

    if uploaded_image is not None:
        st.image(
            uploaded_image,
            caption="Master Görsel Önizleme",
            width="stretch",
        )

with col_right:
    st.subheader("✍️ 2. Master Prompt")
    master_prompt = st.text_area(
        "Sabit metin (her varyasyona uygulanacak temel komut)",
        height=140,
        placeholder=(
            "Örnek: Bu görselin stilini ve karakterlerini koruyarak "
            "aşağıdaki varyasyona uygun yeni bir versiyon üret."
        ),
        help=(
            "💡 Otomatik önek varsayılan olarak AÇIK (sidebar'dan görebilirsin). "
            "Yani senin yazdığın prompt'un başına model'i 'görsel üret' "
            "demeye zorlayan İngilizce kısa bir cümle ekleniyor. "
            "Sen sadece sahnenin/varyasyonun ne olacağını anlat - "
            "model'e komut vermeyi bize bırakabilirsin."
        ),
    )

    # Auto-prefix kapalıysa ve kullanıcı imperatif yazmadıysa uyar.
    # Auto-prefix açıksa zaten model komut alıyor, uyarı gerekmez.
    if not use_auto_prefix and master_prompt.strip():
        _prompt_hint_keywords = [
            "üret", "generate", "create", "draw", "produce", "make"
        ]
        _has_imperative = any(
            kw in master_prompt.lower() for kw in _prompt_hint_keywords
        )
        if not _has_imperative:
            st.caption(
                "⚠️ Otomatik önek kapalı ve prompt'unda 'görsel üret' "
                "benzeri bir komut göremedim. Başarı oranı düşebilir - "
                "ya sidebar'dan oneki aç ya da prompt'una imperatif komut ekle."
            )

    st.subheader("🔀 3. Varyasyonlar")
    variations_text = st.text_area(
        "Her satıra bir varyasyon yaz",
        height=200,
        placeholder=(
            "Kırmızı arka plan\n"
            "Mavi arka plan\n"
            "Gece sahnesi, neon ışıklar\n"
            "Ormanlık alan, yağmurlu hava"
        ),
    )

    # --- CANLI MALİYET TAHMİNİ ---
    # Kullanıcı varyasyon yazdıkça anında "ne kadar para harcayacağım?" görsün.
    # Boş satırları sayma (kullanıcı genelde bırakır).
    _variation_count = sum(
        1 for line in variations_text.splitlines() if line.strip()
    )
    if _variation_count > 0:
        # Resmi fiyatlar - Nisan 2026 itibariyle Google'ın açıkladığı:
        # Standard: $0.039/görsel, Batch: $0.0195/görsel
        # Input token maliyeti negligible (~$0.0002 per görsel) - eklemiyoruz
        # çünkü kullanıcıyı yanıltmasın, ana maliyet output görsel.
        _cost_standard = _variation_count * 0.039
        _cost_batch = _variation_count * 0.0195
        # USD/TL kuru yaklaşık - kullanıcı net rakam görsün diye gösteriyoruz
        # ama "yaklaşık" olduğunu vurguluyoruz
        _try_rate = 36  # Yaklaşık USD/TRY - değişebilir
        _try_standard = _cost_standard * _try_rate
        _try_batch = _cost_batch * _try_rate

        # İki sütunlu metrik gösterimi - karşılaştırma için
        cost_col1, cost_col2 = st.columns(2)
        with cost_col1:
            st.metric(
                f"💸 Standart ({_variation_count} görsel)",
                f"${_cost_standard:.2f}",
                f"~{_try_standard:.0f} TL",
                delta_color="off",
            )
        with cost_col2:
            st.metric(
                f"🟢 Batch ({_variation_count} görsel)",
                f"${_cost_batch:.2f}",
                f"~{_try_batch:.0f} TL (-50%)",
                delta_color="normal",
            )
        st.caption(
            f"📊 Hesap: {_variation_count} görsel × $0.039 (Standard) "
            f"veya × $0.0195 (Batch). Kur ~{_try_rate} TL/USD varsayımı. "
            "Input token maliyeti dahil değil (~$0.0002/görsel - negligible)."
        )

st.divider()


# ---------------------------------------------------------------------------
# Üretimi Başlat butonu
# ---------------------------------------------------------------------------
mode_label = "Standart" if api_mode == "standart" else "Batch"
start_button = st.button(
    f"🚀 Üretimi Başlat ({mode_label} Mod)",
    type="primary",
    width="stretch",
    disabled=st.session_state.is_running,
)


# ===========================================================================
#                              YARDIMCI FONKSİYONLAR
# ===========================================================================
def _validate_inputs(
    api_key: str,
    image_file,
    prompt: str,
    variations_raw: str,
) -> list[str]:
    """Form girdilerini doğrular, hata listesi döner."""
    errors: list[str] = []

    if not api_key or "YOUR_API_KEY" in api_key.upper():
        errors.append("Geçerli bir API anahtarı gerekli (sidebar veya .env).")
    if image_file is None:
        errors.append("Master görsel yüklenmemiş.")
    if not prompt.strip():
        errors.append("Master prompt boş olamaz.")
    if not variations_raw.strip():
        errors.append("En az bir varyasyon satırı gerekli.")

    return errors


def _save_uploaded_to_temp(uploaded_file) -> Path:
    """Streamlit UploadedFile'ı geçici diske yazar (Files API yol bekler)."""
    suffix = Path(uploaded_file.name).suffix or ".png"
    tmp = tempfile.NamedTemporaryFile(delete=False, suffix=suffix)
    tmp.write(uploaded_file.getbuffer())
    tmp.close()
    return Path(tmp.name)


def _make_zip_bytes(paths: list[Path]) -> bytes:
    """
    Verilen path listesindeki tüm dosyaları ZIP olarak paketler.

    Streamlit download_button'a verilebilir bytes döner.
    Bellekte ZIP oluşturuyoruz (BytesIO) - diske yazmıyoruz.
    50 görsel için ~100ms, kabul edilebilir.
    """
    buffer = io.BytesIO()
    # ZIP_DEFLATED: dosyaları sıkıştır (görseller zaten sıkıştırılmış olduğu için
    # az fayda var ama yine de bir miktar kazanç). ZIP_STORED da olur.
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as zf:
        for path in paths:
            # arcname: ZIP içindeki dosya adı (klasör yapısını dahil etme)
            zf.write(path, arcname=path.name)

    # Buffer pozisyonunu başa al ve bytes oku
    buffer.seek(0)
    return buffer.getvalue()


def _render_live_grid(
    placeholder: "st.delta_generator.DeltaGenerator",
    paths: list[Path],
) -> None:
    """
    Canlı grid'i çizer veya günceller.

    YENIDEN ÇİZİM STRATEJİSİ:
        Her yeni görsel geldiğinde tüm grid baştan çiziliyor.
        Streamlit'in delta diff'leme mantığı sayesinde görsel olarak
        sadece değişen kısımlar update olur (performans dostu).

    Args:
        placeholder: st.empty() ile oluşturulmuş placeholder.
        paths: Şu ana kadar üretilmiş görsel yolları.
    """
    if not paths:
        # Boş ise placeholder'ı temizle
        placeholder.empty()
        return

    with placeholder.container():
        # ----- ÜST KISIMDA TOPLU ZIP İNDİR BUTONU -----
        st.markdown(f"### 🖼️ Üretilen Görseller ({len(paths)})")

        zip_bytes = _make_zip_bytes(paths)
        # KEY NEDEN DİNAMİK?
        # _render_live_grid streaming sırasında defalarca çağrılır.
        # Streamlit key'leri session bazlı takip eder. Aynı key iki kez
        # görünürse "duplicate key" hatası fırlar. Görsel sayısını key'e
        # ekleyince her çağrıda FARKLI bir key üretiliyor → çakışma yok.
        zip_key = f"zip_dl_{len(paths)}"
        st.download_button(
            label=f"📦 Hepsini ZIP olarak indir ({len(paths)} görsel)",
            data=zip_bytes,
            file_name="gemini_images.zip",
            mime="application/zip",
            width="stretch",
            type="secondary",
            key=zip_key,
        )

        st.markdown("")  # küçük boşluk

        # ----- 4 SÜTUNLU GÖRSEL GRİDİ -----
        cols_per_row = 4

        for row_start in range(0, len(paths), cols_per_row):
            row_paths = paths[row_start : row_start + cols_per_row]
            cols = st.columns(cols_per_row)

            for col, path in zip(cols, row_paths):
                with col:
                    try:
                        img = Image.open(path)
                        st.image(
                            img,
                            caption=path.name,
                            width="stretch",
                        )

                        # ----- BİREYSEL İNDİR BUTONU -----
                        with open(path, "rb") as f:
                            image_bytes = f.read()

                        suffix = path.suffix.lstrip(".").lower()
                        mime = f"image/{'jpeg' if suffix == 'jpg' else suffix}"

                        # len(paths) + row_start + path.stem kombinasyonu:
                        # Her _render_live_grid çağrısında paths sayısı farklı
                        # olduğu için key her seferinde unique oluyor.
                        dl_key = f"dl_{len(paths)}_{row_start}_{path.stem}"
                        st.download_button(
                            label="💾 İndir",
                            data=image_bytes,
                            file_name=path.name,
                            mime=mime,
                            key=dl_key,
                            width="stretch",
                        )
                    except Exception as e:
                        st.error(f"{path.name}: {e}")


# ===========================================================================
# CANLI GRID PLACEHOLDER - üretim başlamadan ÖNCE oluşturulmalı.
# Streamlit script'i yukarıdan aşağı çalıştırır - bu yüzden grid'in
# görüneceği konumu burada rezerve ediyoruz.
# ===========================================================================
st.divider()
live_grid_placeholder = st.empty()

# Eğer önceki çalıştırmadan görseller varsa onları göster
if st.session_state.saved_paths and not st.session_state.is_running:
    _render_live_grid(live_grid_placeholder, st.session_state.saved_paths)


# ===========================================================================
#                          BATCH MOD AKIŞI
# ===========================================================================
def _run_batch_flow(
    api_key: str,
    master_temp_path: Path,
    master_prompt: str,
    variations_list: list[str],
    output_dir: str,
    grid_placeholder,
    use_auto_prefix: bool = True,
) -> tuple[list[Path], list[str]]:
    """
    Batch API: JSONL üret, job başlat, polling, sonuç indir, kaydet.
    Batch'te sonuçlar TOPLU geliyor; canlı grid stream yapılamıyor.
    Sadece bittiğinde grid'i bir kerede çiziyoruz.

    Args:
        use_auto_prefix: True ise her JSONL satırının prompt'unun başına
            görsel-üret prefix'i eklenir (Standard mode ile simetrik).
    """
    st.write("🔧 Gemini Batch istemcisi hazırlanıyor...")
    handler = GeminiBatchHandler(api_key=api_key)

    st.write("📤 Master görsel Files API'ye yükleniyor (ACTIVE bekleniyor)...")
    handler.upload_master_image(master_temp_path)

    st.write(f"📝 {len(variations_list)} varyasyon için JSONL üretiliyor...")
    jsonl_path = handler.build_jsonl(
        master_prompt=master_prompt,
        variations=variations_list,
        output_path="batch_requests.jsonl",
        use_auto_prefix=use_auto_prefix,
    )

    st.write("🚀 Batch Job başlatılıyor...")
    job_name = handler.start_batch_job(jsonl_path=jsonl_path)
    st.session_state.last_job_name = job_name
    st.write(f"✅ Job oluşturuldu: `{job_name}`")

    st.write("⏳ Job durumu takip ediliyor (uzun sürebilir)...")
    progress_bar = st.progress(0, text="Başlatılıyor...")

    final_progress = None
    for progress in handler.poll_until_complete():
        final_progress = progress
        pct = {
            "JOB_STATE_PENDING": 0.1,
            "JOB_STATE_RUNNING": 0.5,
        }.get(progress.state, 1.0)
        progress_bar.progress(pct, text=progress.message)

    if final_progress and final_progress.state != "JOB_STATE_SUCCEEDED":
        raise RuntimeError(f"Batch job başarısız: {final_progress.message}")

    st.write("📥 Sonuç dosyası indiriliyor...")
    payloads = handler.fetch_results()

    if not payloads:
        raise RuntimeError("Sonuç dosyasında görsel bulunamadı.")

    st.write(f"💾 {len(payloads)} görsel paralel olarak diske yazılıyor...")
    saved_paths = save_all_images_sync(payloads=payloads, output_dir=output_dir)

    # Grid'i bir kerede çiz
    st.session_state.saved_paths = saved_paths
    _render_live_grid(grid_placeholder, saved_paths)

    handler.cleanup()
    return saved_paths, []


# ===========================================================================
#                         STANDART MOD AKIŞI (CANLI GRID)
# ===========================================================================
def _run_standard_flow(
    api_key: str,
    master_temp_path: Path,
    master_prompt: str,
    variations_list: list[str],
    workers: int,
    output_dir: str,
    grid_placeholder,
    use_auto_prefix: bool = True,
) -> tuple[list[Path], list[str]]:
    """
    Standart API: Paralel üretim + her görsel anında diske + canlı grid update.

    AKIŞ:
        1. Handler hazırla, master upload (ACTIVE bekle).
        2. Generator'u tüket - her tamamlanan istek:
           a. Progress bar update.
           b. Log alanına satır ekle.
           c. Yeni payload ANINDA diske yaz.
           d. Grid'i yeniden çiz (yeni görsel dahil).
    """
    st.write("🔧 Gemini Standart istemcisi hazırlanıyor...")
    handler = GeminiStandardHandler(api_key=api_key)

    st.write("📤 Master görsel Files API'ye yükleniyor (ACTIVE bekleniyor)...")
    handler.upload_master_image(master_temp_path)

    total = len(variations_list)
    st.write(f"⚡ {total} varyasyon, {workers} paralel worker ile üretiliyor...")

    # İlerleme barı + log alanı
    progress_bar = st.progress(0, text="Başlatılıyor...")
    log_area = st.empty()
    log_lines: list[str] = []

    # Stream süresince biriken yollar
    saved_paths_so_far: list[Path] = list(st.session_state.saved_paths)
    # Önceki üretimi sıfırla - yeni session başlıyor
    saved_paths_so_far = []

    # Hangi payload'lar zaten kaydedildi (index bazlı takip)
    last_saved_count = 0

    for prog in handler.generate_all_streaming(
        master_prompt=master_prompt,
        variations=variations_list,
        max_workers=workers,
        use_auto_prefix=use_auto_prefix,
    ):
        # ----- 1) Progress bar -----
        pct = prog.completed / prog.total
        progress_bar.progress(
            pct,
            text=f"{prog.completed}/{prog.total} - {prog.last_message}",
        )

        # ----- 2) Log alanı (son 8 satır kaydırarak) -----
        log_lines.append(prog.last_message)
        log_area.code("\n".join(log_lines[-8:]), language=None)

        # ----- 3) Yeni payload var mı? Diske yaz + grid yenile -----
        # handler.payloads stream sırasında dolup gidiyor; biz bir adım gerideyiz.
        current_payload_count = len(handler.payloads)
        if current_payload_count > last_saved_count:
            # Yeni gelen tüm payload'ları kaydet (genelde 1 tane ama
            # birkaç worker aynı anda bitirdiyse birden fazla olabilir)
            new_payloads = handler.payloads[last_saved_count:current_payload_count]
            for new_payload in new_payloads:
                # Tek görseli senkron kaydet (hızlı, blocking değil pratikte)
                saved_path = save_single_image_sync(new_payload, output_dir)
                saved_paths_so_far.append(saved_path)

            last_saved_count = current_payload_count

            # Session state'i güncelle (rerun durumunda kayıp önle)
            st.session_state.saved_paths = saved_paths_so_far.copy()

            # ----- 4) Grid'i yenile - YENİ GÖRSEL DAHIL -----
            _render_live_grid(grid_placeholder, saved_paths_so_far)

    # Stream bitti - genel özet
    failed = handler.failed_keys
    if not saved_paths_so_far:
        raise RuntimeError(
            "Hiçbir varyasyon başarılı olamadı. Log'a bak."
        )

    st.write(f"🎉 {len(saved_paths_so_far)}/{total} görsel başarıyla üretildi.")
    if failed:
        st.warning(f"⚠️ {len(failed)} istek başarısız: {', '.join(failed)}")

    handler.cleanup()
    return saved_paths_so_far, failed


# ===========================================================================
#                              ANA İŞ AKIŞI
# ===========================================================================
if start_button:
    validation_errors = _validate_inputs(
        api_key_input,
        uploaded_image,
        master_prompt,
        variations_text,
    )

    if validation_errors:
        for err in validation_errors:
            st.error(f"❌ {err}")
    else:
        # State sıfırla
        st.session_state.is_running = True
        st.session_state.run_started_at = time.time()  # Kilitlenme tespiti için
        st.session_state.last_error = None
        st.session_state.saved_paths = []
        st.session_state.failed_keys = []

        # Live grid placeholder'ı temizle
        live_grid_placeholder.empty()

        variations_list = [
            v.strip() for v in variations_text.splitlines() if v.strip()
        ]
        master_temp_path = _save_uploaded_to_temp(uploaded_image)

        try:
            with st.status(
                f"{mode_label} işlemi başlatılıyor...",
                expanded=True,
            ) as status:

                if api_mode == "batch":
                    saved_paths, failed_keys = _run_batch_flow(
                        api_key=api_key_input,
                        master_temp_path=master_temp_path,
                        master_prompt=master_prompt,
                        variations_list=variations_list,
                        output_dir=output_dir,
                        grid_placeholder=live_grid_placeholder,
                        use_auto_prefix=use_auto_prefix,
                    )
                else:
                    saved_paths, failed_keys = _run_standard_flow(
                        api_key=api_key_input,
                        master_temp_path=master_temp_path,
                        master_prompt=master_prompt,
                        variations_list=variations_list,
                        workers=max_workers or 2,
                        output_dir=output_dir,
                        grid_placeholder=live_grid_placeholder,
                        use_auto_prefix=use_auto_prefix,
                    )

                st.session_state.saved_paths = saved_paths
                st.session_state.failed_keys = failed_keys

                status.update(
                    label=f"✅ {len(saved_paths)} görsel başarıyla üretildi!",
                    state="complete",
                    expanded=False,
                )

        except Exception as exc:
            st.session_state.last_error = str(exc)

            # 429 için özel kullanıcı dostu mesaj
            if "429" in str(exc) or "RESOURCE_EXHAUSTED" in str(exc).upper():
                st.error(
                    "❌ **Rate Limit / Quota Hatası (429)**\n\n"
                    "- Çok hızlı istek attın → Worker sayısını düşür ve bekle\n"
                    "- Günlük kotan doldu → Yarın tekrar dene\n"
                    "- Free tier'da `gemini-2.5-flash-image` çalışmaz → Billing aç"
                )
            st.exception(exc)

        finally:
            st.session_state.is_running = False
            try:
                master_temp_path.unlink(missing_ok=True)
            except Exception:
                pass


# ===========================================================================
#                          KALICI HATA GÖSTERİMİ
# ===========================================================================
if st.session_state.last_error:
    st.divider()
    with st.expander("❌ Son hata detayı", expanded=False):
        st.error(st.session_state.last_error)
