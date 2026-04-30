"""
main.py
=======
Gemini 2.5 Flash Image Batcher - Streamlit Arayüzü.

YENİ ÖZELLİKLER (v2):
    - Standart modda üretim sırasında ilerleme çubuğu + log; bitince tek seferde sonuç grid'i
      (Streamlit widget key/rerun sorunlarını en aza indirir).
    - BİREYSEL İNDİR + TOPLU ZIP: Her görsel ve paket indirme.
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
        # Üretilmiş görsellerin disk yolları — STRING listesi (rerun uyumu).
        "saved_paths": [],
        "is_running": False,        # Şu an iş çalışıyor mu (çift tıklama engeli)
        "last_error": None,         # Son hata mesajı
        "last_job_name": None,      # Son batch job ismi (debug için)
        "failed_keys": [],          # Başarısız istekler
        "run_started_at": None,     # Üretim başlangıç zamanı (timestamp)
        # file_uploader indirme/rerun sonrası None olabiliyor; master baytı sakla.
        "master_upload_bytes": None,
        "master_upload_name": None,
        # Widget key'leri ile bağlı — indirme tetiklenince prompt'lar silinmesin.
        "widget_master_prompt": "",
        "widget_variations": "",
        # Her yeni üretimde +1; indirme/ZIP buton key çakışmasını kesin olarak önler.
        "_results_gen": 0,
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
            "Standart: Anlık üretim, bitince aşağıda indirme. "
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
            "- Bittiğinde aşağıda önizleme + indirme\n"
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
    "Master görsel + Master prompt + Varyasyonlar ile toplu üretim. "
    "Dosyalar `outputs/` klasörüne yazılır; arayüzde işlem bitince indirebilirsin."
)


# ---------------------------------------------------------------------------
# Form alanları - 2 sütunlu düzen
# ---------------------------------------------------------------------------
col_left, col_right = st.columns([1, 1])

with col_left:
    st.subheader("📤 1. Master Görsel")
    # key= ile widget değeri session_state'te kalır; indirme/rerun sonrası
    # uploader boş dönerse aşağıdaki önbellek devreye girer.
    uploaded_image = st.file_uploader(
        "Referans görseli yükle",
        type=["png", "jpg", "jpeg", "webp"],
        help="Tüm varyasyonlar bu görseli temel alacak.",
        key="widget_master_file",
    )
    if uploaded_image is not None:
        st.session_state.master_upload_bytes = uploaded_image.getvalue()
        st.session_state.master_upload_name = uploaded_image.name

    _preview_bytes = (
        uploaded_image.getvalue()
        if uploaded_image is not None
        else st.session_state.get("master_upload_bytes")
    )
    if _preview_bytes:
        st.image(
            _preview_bytes,
            caption="Master Görsel Önizleme",
            width="stretch",
        )
        if uploaded_image is None and st.session_state.get("master_upload_bytes"):
            st.caption("📌 Önbellekteki görsel (yeniden yüklemeden üretebilirsin).")

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
        key="widget_master_prompt",
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
        key="widget_variations",
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
    has_master_image: bool,
    prompt: str,
    variations_raw: str,
) -> list[str]:
    """Form girdilerini doğrular, hata listesi döner."""
    errors: list[str] = []

    if not api_key or "YOUR_API_KEY" in api_key.upper():
        errors.append("Geçerli bir API anahtarı gerekli (sidebar veya .env).")
    if not has_master_image:
        errors.append("Master görsel yüklenmemiş (veya oturumda önbellek yok).")
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


def _save_bytes_to_temp(data: bytes, suffix: str) -> Path:
    """Ham baytları geçici dosyaya yazar (önbellekten master görsel için)."""
    if not suffix.startswith("."):
        suffix = f".{suffix}"
    tmp = tempfile.NamedTemporaryFile(delete=False, suffix=suffix)
    tmp.write(data)
    tmp.close()
    return Path(tmp.name)


def _paths_from_session_saved() -> list[Path]:
    """saved_paths oturum değerini Path listesine çevirir (str veya Path kabul)."""
    raw = st.session_state.get("saved_paths") or []
    return [Path(str(p)) for p in raw]


def _invalidate_zip_cache() -> None:
    """Yeni üretim başlarken ZIP önbelleğini temizle."""
    st.session_state.pop("_zip_cache_sig", None)
    st.session_state.pop("_zip_cache_bytes", None)


def _zip_bytes_cached(paths: list[Path]) -> bytes:
    """
    Aynı dosya seti için ZIP'i tekrar tekrar üretmeyi önler.
    Canlı grid her görselde yeniden çizildiğinde 20 görseli ZIP'lemek
    UI'ı kilitler ve hatalara yol açar.
    """
    try:
        sig = tuple(
            (str(p.resolve()), p.stat().st_size, int(p.stat().st_mtime_ns))
            for p in paths
            if p.exists()
        )
    except OSError:
        sig = tuple(str(p) for p in paths)

    if (
        st.session_state.get("_zip_cache_sig") == sig
        and st.session_state.get("_zip_cache_bytes") is not None
    ):
        return st.session_state["_zip_cache_bytes"]

    data = _make_zip_bytes(paths)
    st.session_state["_zip_cache_sig"] = sig
    st.session_state["_zip_cache_bytes"] = data
    return data


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


def _render_results_grid(
    placeholder: "st.delta_generator.DeltaGenerator",
    paths: list[Path],
) -> None:
    """
    Sonuç görsellerini TEK blokta çizer (önizleme + ZIP + tekil indir).

    TASARIM: Standart akışta döngü içinde BURAYI ÇAĞIRMA — her tamamlanan
    görselde tüm grid'i yeniden kurmak Streamlit'te key/rerun sorunlarını
    çoğaltır. Üretim bitince saved_paths dolu → script sonunda bir kez çağrılır.

    Widget key'leri _results_gen ile benzersiz: yeni üretimde eski butonlarla
    asla çakışmaz (StreamlitDuplicateElementKey önlemi).
    """
    if not paths:
        placeholder.empty()
        return

    placeholder.empty()
    gen = int(st.session_state.get("_results_gen", 0))

    with placeholder.container():
        st.markdown(f"### 🖼️ Üretilen Görseller ({len(paths)})")

        zip_bytes = _zip_bytes_cached(paths)
        st.download_button(
            label=f"📦 Hepsini ZIP olarak indir ({len(paths)} görsel)",
            data=zip_bytes,
            file_name="gemini_images.zip",
            mime="application/zip",
            width="stretch",
            type="secondary",
            key=f"results_zip_g{gen}",
        )

        st.markdown("")

        cols_per_row = 4
        global_idx = 0

        for row_start in range(0, len(paths), cols_per_row):
            row_paths = paths[row_start : row_start + cols_per_row]
            cols = st.columns(cols_per_row)

            for col, path in zip(cols, row_paths):
                with col:
                    try:
                        # Dosya yeni yazıldıysa (özellikle Windows) kısa gecikmeyle
                        # tekrar dene; aksi halde grid'de boş kutu görülebilir.
                        img = None
                        last_err: Exception | None = None
                        for _attempt in range(5):
                            try:
                                img = Image.open(path)
                                img.load()
                                break
                            except Exception as err:
                                last_err = err
                                time.sleep(0.06)
                        if img is None:
                            raise last_err or RuntimeError(path.name)

                        st.image(
                            img,
                            caption=path.name,
                            width="stretch",
                        )

                        with open(path, "rb") as f:
                            image_bytes = f.read()

                        suffix = path.suffix.lstrip(".").lower()
                        mime = f"image/{'jpeg' if suffix == 'jpg' else suffix}"

                        st.download_button(
                            label="💾 İndir",
                            data=image_bytes,
                            file_name=path.name,
                            mime=mime,
                            key=f"results_dl_g{gen}_{global_idx}",
                            width="stretch",
                        )
                        global_idx += 1
                    except Exception as e:
                        st.error(f"{path.name}: {e}")
                        global_idx += 1


# ===========================================================================
# SONUÇ ALANI — üretim bittikten sonra tek seferde doldurulur (st.empty).
# ===========================================================================
st.divider()
st.caption(
    "📥 **Sonuçlar:** Üretim sürerken ilerleme yukarıda; bittiğinde görseller "
    "burada önizlenir ve indirilebilir."
)
live_grid_placeholder = st.empty()


def _draw_results_section() -> None:
    """
    Script sonunda: üretim yoksa alanı temizle; varsa grid'i bir kez çiz.

    Önceki üretim sidebar'dan silindiğinde saved_paths=[] olur — placeholder
    boşaltılmazsa eski görseller ekranda kalır; bu yüzden boşta da empty() şart.
    """
    if st.session_state.is_running:
        return
    paths = _paths_from_session_saved()
    if not paths:
        live_grid_placeholder.empty()
        return
    _render_results_grid(live_grid_placeholder, paths)


# Script sonunda çağrılır: önce start_button bloğu state'i yazar, sonra grid çizilir.


# ===========================================================================
#                          BATCH MOD AKIŞI
# ===========================================================================
def _run_batch_flow(
    api_key: str,
    master_temp_path: Path,
    master_prompt: str,
    variations_list: list[str],
    output_dir: str,
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

    # Grid, script sonunda _draw_results_section ile çizilir (tek yol).

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
    use_auto_prefix: bool = True,
) -> tuple[list[Path], list[str]]:
    """
    Standart API: Paralel üretim + her görsel anında diske yazılır.

    UI: Döngüde sadece ilerleme + log — grid/indirme yok (Streamlit stabilitesi).
    Bittiğinde saved_paths dolar; script sonunda tek seferde grid çizilir.
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

    # Bu koşu için sıfırdan biriken disk yolları (Path; session'a string olarak yazılır).
    saved_paths_so_far: list[Path] = []

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

            # Ara kayıt: çökme olursa kısmi sonuç diskte kalır; grid yine sonda çizilir.
            st.session_state.saved_paths = [str(p) for p in saved_paths_so_far]

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
    # Uploader rerun/indirme sonrası boş dönebilir; bayt önbelleği varsa yine geçerli.
    has_master_image = uploaded_image is not None or bool(
        st.session_state.get("master_upload_bytes")
    )
    validation_errors = _validate_inputs(
        api_key_input,
        has_master_image,
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
        # Önceki üretimin ZIP önbelleği yeni koşuda yanlışlıkla kullanılmasın.
        _invalidate_zip_cache()
        # Yeni widget nesli — indirme butonları önceki koşu ile asla aynı key'i paylaşmaz.
        st.session_state["_results_gen"] = int(
            st.session_state.get("_results_gen", 0)
        ) + 1

        # Sonuç alanını boşalt; üretim bitince tek parça grid basılacak.
        live_grid_placeholder.empty()

        variations_list = [
            v.strip() for v in variations_text.splitlines() if v.strip()
        ]
        # Master dosya: yüklü dosya varsa ondan; yoksa oturumdaki baytlardan temp üret.
        if uploaded_image is not None:
            master_temp_path = _save_uploaded_to_temp(uploaded_image)
        else:
            _mb = st.session_state.get("master_upload_bytes")
            _mn = st.session_state.get("master_upload_name") or "master.png"
            master_temp_path = _save_bytes_to_temp(
                _mb, Path(_mn).suffix or ".png"
            )

        try:
            # Standart mod: st.status İÇİNDE grid güncellenirse kutu kapanınca içerik
            # kayboluyor gibi davranır; indirme de tam rerun tetikler — grid ana akışta kalsın.
            if api_mode == "batch":
                with st.status(
                    f"{mode_label} işlemi başlatılıyor...",
                    expanded=True,
                ) as status:
                    saved_paths, failed_keys = _run_batch_flow(
                        api_key=api_key_input,
                        master_temp_path=master_temp_path,
                        master_prompt=master_prompt,
                        variations_list=variations_list,
                        output_dir=output_dir,
                        use_auto_prefix=use_auto_prefix,
                    )
                    st.session_state.saved_paths = [str(p) for p in saved_paths]
                    st.session_state.failed_keys = failed_keys
                    status.update(
                        label=f"✅ {len(saved_paths)} görsel başarıyla üretildi!",
                        state="complete",
                        expanded=False,
                    )
            else:
                saved_paths, failed_keys = _run_standard_flow(
                    api_key=api_key_input,
                    master_temp_path=master_temp_path,
                    master_prompt=master_prompt,
                    variations_list=variations_list,
                    workers=max_workers or 2,
                    output_dir=output_dir,
                    use_auto_prefix=use_auto_prefix,
                )
                st.session_state.saved_paths = [str(p) for p in saved_paths]
                st.session_state.failed_keys = failed_keys
                st.success(
                    f"✅ {len(saved_paths)} görsel hazır; aşağıdan indirebilirsin.",
                    icon="✅",
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
#                         SONUÇ GRİDİ (tek çizim noktası)
# ===========================================================================
_draw_results_section()


# ===========================================================================
#                          KALICI HATA GÖSTERİMİ
# ===========================================================================
if st.session_state.last_error:
    st.divider()
    with st.expander("❌ Son hata detayı", expanded=False):
        st.error(st.session_state.last_error)
